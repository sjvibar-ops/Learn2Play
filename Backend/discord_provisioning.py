"""Database-only Discord channel provisioning primitives (Batch 3.2A-2B-2A).

Not an HTTP endpoint. A trusted controller must authorize the appointment and
verify the Discord users before calling these functions in a later batch.
No Discord API calls, retries, stale-claim recovery or compensating deletes occur.
A database commit error has an *unknown* commit outcome; callers must reconcile
rather than assume that Discord/SQL state can be rolled back safely.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
import secrets

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

if __package__:
    from .extensions import db
    from .models import Appointment
else:
    from extensions import db
    from models import Appointment


_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
_CHANNEL_ID_RE = re.compile(r"[0-9]{15,25}")
_ELIGIBLE_BOOKINGS = ("accepted", "scheduled")
_CLAIMABLE_CHANNEL_STATES = ("unprovisioned", "failed")


class ClaimStatus(str, Enum):
    CLAIMED = "CLAIMED"
    ALREADY_PROVISIONED = "ALREADY_PROVISIONED"
    IN_PROGRESS = "IN_PROGRESS"
    INELIGIBLE_BOOKING = "INELIGIBLE_BOOKING"
    UNCERTAIN_LOCKED = "UNCERTAIN_LOCKED"
    CONCURRENT_CONFLICT = "CONCURRENT_CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    DATABASE_ERROR = "DATABASE_ERROR"
    INVALID_INPUT = "INVALID_INPUT"


class FinalizeStatus(str, Enum):
    FINALIZED = "FINALIZED"
    FENCING_REJECTED = "FENCING_REJECTED"
    INTEGRITY_CONFLICT = "INTEGRITY_CONFLICT"
    DATABASE_ERROR = "DATABASE_ERROR"
    INVALID_INPUT = "INVALID_INPUT"


class FailureOutcome(str, Enum):
    """Only definite non-creation may become FAILED; unknown remains UNCERTAIN."""
    NOT_CREATED = "NOT_CREATED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    token: str | None = None  # Internal-only; never serialize to HTTP response.
    existing_channel_id: str | None = None


@dataclass(frozen=True)
class FinalizeResult:
    status: FinalizeStatus
    channel_id: str | None = None


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _valid_appointment_id(value):
    return type(value) is int and value > 0


def _valid_token(value):
    return isinstance(value, str) and _TOKEN_RE.fullmatch(value) is not None


def _valid_channel_id(value):
    return isinstance(value, str) and _CHANNEL_ID_RE.fullmatch(value) is not None


def _safe_rollback():
    try:
        db.session.rollback()
    except Exception:
        # Never replace an original DB failure with a rollback failure.
        pass


def _classify_unclaimed(appointment_id):
    """Read status only after the losing UPDATE has been rolled back.

    This is informational; it does not confer any right to provision. The
    inspection transaction is closed before the result is returned.
    """
    try:
        row = db.session.execute(
            sa.select(
                Appointment.status,
                Appointment.discord_channel_status,
                Appointment.discord_channel_id,
            ).where(Appointment.id == appointment_id)
        ).first()
        db.session.rollback()
    except Exception:
        _safe_rollback()
        return ClaimResult(ClaimStatus.DATABASE_ERROR)
    if row is None:
        return ClaimResult(ClaimStatus.NOT_FOUND)
    booking, state, channel_id = row
    if booking not in _ELIGIBLE_BOOKINGS:
        return ClaimResult(ClaimStatus.INELIGIBLE_BOOKING)
    if state == "uncertain":
        return ClaimResult(ClaimStatus.UNCERTAIN_LOCKED)
    if state == "provisioning":
        return ClaimResult(ClaimStatus.IN_PROGRESS)
    if state == "provisioned" and channel_id:
        return ClaimResult(ClaimStatus.ALREADY_PROVISIONED, existing_channel_id=channel_id)
    return ClaimResult(ClaimStatus.CONCURRENT_CONFLICT)


def claim_provisioning(appointment_id):
    """Atomically claim one eligible booking; return CLAIMED only after commit."""
    if not _valid_appointment_id(appointment_id):
        return ClaimResult(ClaimStatus.INVALID_INPUT)
    token = secrets.token_hex(32)
    now = _utcnow()
    try:
        result = db.session.execute(
            sa.update(Appointment)
            .where(
                Appointment.id == appointment_id,
                Appointment.status.in_(_ELIGIBLE_BOOKINGS),
                Appointment.discord_channel_id.is_(None),
                Appointment.discord_channel_status.in_(_CLAIMABLE_CHANNEL_STATES),
            )
            .values(
                discord_channel_status="provisioning",
                discord_channel_attempt_token=token,
                discord_channel_last_attempt_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            _safe_rollback()
            return _classify_unclaimed(appointment_id)
        db.session.commit()
        return ClaimResult(ClaimStatus.CLAIMED, token=token)
    except Exception:
        _safe_rollback()
        return ClaimResult(ClaimStatus.DATABASE_ERROR)
    except Exception:
        _safe_rollback()
        return ClaimResult(ClaimStatus.DATABASE_ERROR)


def finalize_provisioning_success(appointment_id, attempt_token, channel_id):
    """Record a validated channel ID iff the original claim is still owned."""
    if not (_valid_appointment_id(appointment_id) and _valid_token(attempt_token)
            and _valid_channel_id(channel_id)):
        return FinalizeResult(FinalizeStatus.INVALID_INPUT)
    now = _utcnow()
    try:
        result = db.session.execute(
            sa.update(Appointment)
            .where(
                Appointment.id == appointment_id,
                Appointment.discord_channel_attempt_token == attempt_token,
                Appointment.discord_channel_status == "provisioning",
                Appointment.status.in_(_ELIGIBLE_BOOKINGS),
                Appointment.discord_channel_id.is_(None),
            )
            .values(
                discord_channel_id=channel_id,
                discord_channel_status="provisioned",
                discord_channel_created_at=now,
                discord_channel_attempt_token=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            _safe_rollback()
            return FinalizeResult(FinalizeStatus.FENCING_REJECTED)
        db.session.commit()
        return FinalizeResult(FinalizeStatus.FINALIZED, channel_id=channel_id)
    except IntegrityError:
        _safe_rollback()
        return FinalizeResult(FinalizeStatus.INTEGRITY_CONFLICT)
    except Exception:
        _safe_rollback()
        return FinalizeResult(FinalizeStatus.DATABASE_ERROR)


def finalize_provisioning_failure(appointment_id, attempt_token, outcome):
    """Persist definite failure or uncertain creation without attempting recovery.

    Eligibility is intentionally *not* a guard here: a booking could be
    cancelled while an attempt is in flight. The original token owner may still
    record an unknown outcome, which must not be silently discarded. No channel
    is created or deleted. Finalization on a terminal booking never makes that
    booking eligible for a new claim.
    """
    if not (_valid_appointment_id(appointment_id) and _valid_token(attempt_token)):
        return FinalizeResult(FinalizeStatus.INVALID_INPUT)
    if not isinstance(outcome, FailureOutcome):
        return FinalizeResult(FinalizeStatus.INVALID_INPUT)
    new_state = "failed" if outcome is FailureOutcome.NOT_CREATED else "uncertain"
    try:
        result = db.session.execute(
            sa.update(Appointment)
            .where(
                Appointment.id == appointment_id,
                Appointment.discord_channel_attempt_token == attempt_token,
                Appointment.discord_channel_status == "provisioning",
                Appointment.discord_channel_id.is_(None),
            )
            .values(
                discord_channel_status=new_state,
                discord_channel_attempt_token=None,
                updated_at=_utcnow(),
            )
        )
        if result.rowcount != 1:
            _safe_rollback()
            return FinalizeResult(FinalizeStatus.FENCING_REJECTED)
        db.session.commit()
        return FinalizeResult(FinalizeStatus.FINALIZED)
    except Exception:
        _safe_rollback()
        return FinalizeResult(FinalizeStatus.DATABASE_ERROR)
