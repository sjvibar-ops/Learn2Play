"""Exclusive, durable Discord deletion dispatch and outcome finalization.

Safety Properties:
- Never holds a database transaction across external Discord HTTP requests.
- Compare-And-Swap (CAS) token-fenced claim prevents duplicate or concurrent dispatch.
- Verifies single-owner channel provenance, terminal booking, and absence of conflicting recoveries.
- Interrupted or timed-out dispatches transition to UNCERTAIN / NEEDS_MANUAL_REVIEW;
  they are NEVER automatically retried or re-dispatched.
- No live Discord operations are enabled by default (DISCORD_SESSIONS_ENABLED=false).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
import secrets

import sqlalchemy as sa
from flask import current_app

if __package__:
    from .extensions import db
    from .models import Appointment, DiscordChannelRecovery
    from . import discord_oauth
else:
    from extensions import db
    from models import Appointment, DiscordChannelRecovery
    import discord_oauth


class DispatchClaimStatus(str, Enum):
    CLAIMED = "CLAIMED"
    REJECTED = "REJECTED"
    INVALID = "INVALID"
    DATABASE_ERROR = "DATABASE_ERROR"


class DispatchStatus(str, Enum):
    DELETED = "DELETED"
    RATE_LIMITED = "RATE_LIMITED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"
    INVALID = "INVALID"
    DATABASE_ERROR = "DATABASE_ERROR"


@dataclass(frozen=True)
class DispatchClaimResult:
    status: DispatchClaimStatus
    token: str | None = None
    channel_id: str | None = None
    appointment_id: int | None = None


@dataclass(frozen=True)
class DispatchResult:
    status: DispatchStatus
    channel_id: str | None = None
    already_absent: bool = False
    retry_after: float | None = None
    error_code: str | None = None
    http_status: int | None = None


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rollback():
    try:
        db.session.rollback()
    except Exception:
        pass


def claim_deletion_dispatch(recovery_id, intent_token, worker_id="dispatch_worker"):
    """Atomically advance a DELETION_INTENT_CLAIMED record to DISPATCH_IN_FLIGHT.

    Guarantees:
    - Single worker exclusivity via atomic CAS on cleanup_fencing_token.
    - Zero active booking references.
    - Zero conflicting recovery rows (even resolved ones).
    - Transaction is COMMITTED before external network dispatch.
    """
    if (type(recovery_id) is not int or recovery_id <= 0
            or not isinstance(intent_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", intent_token) is None):
        return DispatchClaimResult(DispatchClaimStatus.INVALID)

    dispatch_token = secrets.token_hex(32)
    now = _utcnow()
    r = DiscordChannelRecovery
    a = Appointment
    other = sa.orm.aliased(r)
    ref = sa.orm.aliased(a)

    try:
        # Scalar read of recovery to obtain channel_id and appointment_id within same tx
        rec = db.session.execute(
            sa.select(r.channel_id, r.appointment_id)
            .where(
                r.id == recovery_id,
                r.cleanup_state == "DELETION_INTENT_CLAIMED",
                r.cleanup_fencing_token == intent_token,
                r.recovery_status.in_(("PENDING", "NEEDS_MANUAL_REVIEW")),
                r.recovery_reason.in_(("CANCELLED", "COMPLETED")),
            )
        ).first()

        if rec is None or not rec[0] or not rec[1]:
            _rollback()
            return DispatchClaimResult(DispatchClaimStatus.REJECTED)

        channel_id, appointment_id = rec[0], rec[1]

        owner = sa.exists(sa.select(a.id).where(
            a.id == r.appointment_id,
            a.discord_channel_id == r.channel_id,
            a.status.in_(("cancelled", "completed")),
            a.discord_channel_status == "deprovisioning",
            a.discord_channel_attempt_token.is_(None),
        ))
        conflicting_appointment = sa.exists(sa.select(ref.id).where(
            ref.discord_channel_id == r.channel_id,
            ref.id != r.appointment_id,
        ))
        # Provenance verification: Only a verified legitimate historical provisioning
        # attempt belonging to the exact same appointment and channel is non-conflicting.
        # Any unresolved, uncertain, failed, duplicate terminal, or different-appointment
        # record remains strictly blocking.
        conflicting_recovery = sa.exists(sa.select(other.id).where(
            other.channel_id == r.channel_id,
            other.id != r.id,
            ~sa.and_(
                other.appointment_id == r.appointment_id,
                other.channel_id == r.channel_id,
                other.event_key.like("attempt:%"),
                other.recovery_reason == "RESOLVED",
                other.recovery_status == "RESOLVED",
                other.cleanup_state.in_(("UNCLAIMED", "RESOLVED")),
                other.cleanup_fencing_token.is_(None),
            ),
        ))

        changed = db.session.execute(sa.update(r).where(
            r.id == recovery_id,
            r.cleanup_state == "DELETION_INTENT_CLAIMED",
            r.cleanup_fencing_token == intent_token,
            r.channel_id == channel_id,
            r.recovery_status.in_(("PENDING", "NEEDS_MANUAL_REVIEW")),
            r.recovery_reason.in_(("CANCELLED", "COMPLETED")),
            owner,
            ~conflicting_appointment,
            ~conflicting_recovery,
        ).values(
            cleanup_state="DISPATCH_IN_FLIGHT",
            cleanup_fencing_token=dispatch_token,
            cleanup_claimed_at=now,
            cleanup_worker_id=worker_id,
            deletion_dispatched_at=now,
            updated_at=now,
        )).rowcount

        if changed != 1:
            _rollback()
            return DispatchClaimResult(DispatchClaimStatus.REJECTED)

        db.session.commit()
        return DispatchClaimResult(
            DispatchClaimStatus.CLAIMED,
            token=dispatch_token,
            channel_id=channel_id,
            appointment_id=appointment_id,
        )
    except Exception:
        _rollback()
        return DispatchClaimResult(DispatchClaimStatus.DATABASE_ERROR)


def finalize_deletion_dispatch(recovery_id, dispatch_token, res, appointment_id, channel_id):
    """Commit deletion outcome using matching dispatch_token in a fresh transaction.

    Enforces:
    - Dual atomic update of recovery and appointment on confirmed deletion.
    - Idempotent resolution on 404 (already absent).
    - Rate limit preservation on 429.
    - Fail-closed transition to UNCERTAIN / NEEDS_MANUAL_REVIEW on 5xx or timeout.
    """
    if (type(recovery_id) is not int or recovery_id <= 0
            or not isinstance(dispatch_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", dispatch_token) is None):
        return DispatchResult(DispatchStatus.INVALID)

    now = _utcnow()
    r = DiscordChannelRecovery
    a = Appointment

    try:
        status_name = res.get("status") if isinstance(res, dict) else None
        is_ok = res.get("ok") is True if isinstance(res, dict) else False

        # Case 1: Confirmed deletion or confirmed already absent (200, 204, 404)
        if is_ok and status_name in (
            discord_oauth.VOICE_CHANNEL_DELETED,
            discord_oauth.VOICE_CHANNEL_ALREADY_ABSENT,
        ):
            already_absent = bool(res.get("already_absent", False))
            http_code = 404 if already_absent else 204
            audit_note = (
                "Channel already absent on Discord (idempotent)"
                if already_absent
                else "Channel deleted on Discord"
            )

            changed_recovery = db.session.execute(sa.update(r).where(
                r.id == recovery_id,
                r.cleanup_state == "DISPATCH_IN_FLIGHT",
                r.cleanup_fencing_token == dispatch_token,
            ).values(
                cleanup_state="RESOLVED",
                recovery_status="RESOLVED",
                resolved_at=now,
                deletion_http_status=http_code,
                last_error_code=None,
                operator_notes=audit_note,
                updated_at=now,
            )).rowcount

            changed_appointment = db.session.execute(sa.update(a).where(
                a.id == appointment_id,
                a.discord_channel_id == channel_id,
                a.discord_channel_status == "deprovisioning",
                a.status.in_(("cancelled", "completed")),
            ).values(
                discord_channel_status="deprovisioned",
                updated_at=now,
            )).rowcount

            if changed_recovery != 1 or changed_appointment != 1:
                _rollback()
                return mark_dispatch_interrupted(
                    recovery_id, dispatch_token, "Fencing conflict during deletion finalization"
                )

            db.session.commit()
            return DispatchResult(
                DispatchStatus.DELETED,
                channel_id=channel_id,
                already_absent=already_absent,
                http_status=http_code,
            )

        # Case 2: Rate limited (429)
        if status_name == discord_oauth.VOICE_RATE_LIMITED:
            retry_after = res.get("retry_after") if isinstance(res, dict) else None
            changed = db.session.execute(sa.update(r).where(
                r.id == recovery_id,
                r.cleanup_state == "DISPATCH_IN_FLIGHT",
                r.cleanup_fencing_token == dispatch_token,
            ).values(
                cleanup_state="RATE_LIMITED",
                deletion_http_status=429,
                last_error_code="RATE_LIMITED",
                operator_notes=f"Rate limited by Discord. Retry-After: {retry_after}",
                updated_at=now,
            )).rowcount

            if changed != 1:
                _rollback()
                return DispatchResult(DispatchStatus.REJECTED)

            db.session.commit()
            return DispatchResult(
                DispatchStatus.RATE_LIMITED,
                channel_id=channel_id,
                retry_after=retry_after,
                http_status=429,
            )

        # Case 3: Definite rejection (401, 403, 400)
        if status_name in (
            discord_oauth.VOICE_BOT_FORBIDDEN,
            discord_oauth.VOICE_REQUEST_REJECTED,
        ):
            http_code = 403 if status_name == discord_oauth.VOICE_BOT_FORBIDDEN else 400
            changed = db.session.execute(sa.update(r).where(
                r.id == recovery_id,
                r.cleanup_state == "DISPATCH_IN_FLIGHT",
                r.cleanup_fencing_token == dispatch_token,
            ).values(
                cleanup_state="NEEDS_MANUAL_REVIEW",
                recovery_status="NEEDS_MANUAL_REVIEW",
                deletion_http_status=http_code,
                last_error_code=status_name,
                operator_notes=f"Discord deletion rejected: {status_name}",
                updated_at=now,
            )).rowcount

            if changed != 1:
                _rollback()
                return DispatchResult(DispatchStatus.REJECTED)

            db.session.commit()
            return DispatchResult(
                DispatchStatus.REJECTED,
                channel_id=channel_id,
                error_code=status_name,
                http_status=http_code,
            )

        # Case 4: Ambiguous / 5xx / Timeout / Socket Error
        changed = db.session.execute(sa.update(r).where(
            r.id == recovery_id,
            r.cleanup_state == "DISPATCH_IN_FLIGHT",
            r.cleanup_fencing_token == dispatch_token,
        ).values(
            cleanup_state="UNCERTAIN",
            recovery_status="NEEDS_MANUAL_REVIEW",
            deletion_http_status=500,
            last_error_code="OUTCOME_UNKNOWN",
            operator_notes="Deletion dispatch outcome unknown: timeout or 5xx. Do not auto-retry.",
            updated_at=now,
        )).rowcount

        if changed != 1:
            _rollback()
            return DispatchResult(DispatchStatus.REJECTED)

        db.session.commit()
        return DispatchResult(
            DispatchStatus.UNCERTAIN,
            channel_id=channel_id,
            error_code="OUTCOME_UNKNOWN",
            http_status=500,
        )

    except Exception:
        _rollback()
        return DispatchResult(DispatchStatus.DATABASE_ERROR)


def mark_dispatch_interrupted(recovery_id, dispatch_token, reason="Worker interrupted"):
    """Token-fenced fail-closed recovery for worker crash or interruption."""
    if (type(recovery_id) is not int or recovery_id <= 0
            or not isinstance(dispatch_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", dispatch_token) is None):
        return DispatchResult(DispatchStatus.INVALID)

    now = _utcnow()
    try:
        changed = db.session.execute(sa.update(DiscordChannelRecovery).where(
            DiscordChannelRecovery.id == recovery_id,
            DiscordChannelRecovery.cleanup_state == "DISPATCH_IN_FLIGHT",
            DiscordChannelRecovery.cleanup_fencing_token == dispatch_token,
        ).values(
            cleanup_state="UNCERTAIN",
            recovery_status="NEEDS_MANUAL_REVIEW",
            last_error_code="INTERRUPTED",
            operator_notes=reason,
            updated_at=now,
        )).rowcount

        if changed != 1:
            _rollback()
            return DispatchResult(DispatchStatus.REJECTED)

        db.session.commit()
        return DispatchResult(DispatchStatus.UNCERTAIN, error_code="INTERRUPTED")
    except Exception:
        _rollback()
        return DispatchResult(DispatchStatus.DATABASE_ERROR)


def dispatch_channel_deletion(recovery_id, intent_token, settings=None, worker_id="dispatch_worker"):
    """High-level deletion dispatch executor with strict transaction decoupling.

    Sequence:
    1. DB Tx 1: Atomically claim attempt (DISPATCH_IN_FLIGHT) and commit.
    2. Outside DB Tx: Call Discord REST DELETE /channels/{channel_id}.
    3. DB Tx 2: Atomically finalize outcome and commit.
    """
    claim = claim_deletion_dispatch(recovery_id, intent_token, worker_id=worker_id)
    if claim.status != DispatchClaimStatus.CLAIMED:
        status_map = {
            DispatchClaimStatus.REJECTED: DispatchStatus.REJECTED,
            DispatchClaimStatus.INVALID: DispatchStatus.INVALID,
            DispatchClaimStatus.DATABASE_ERROR: DispatchStatus.DATABASE_ERROR,
        }
        return DispatchResult(status=status_map.get(claim.status, DispatchStatus.REJECTED))

    # CRITICAL: Verify no database transaction remains open across HTTP call
    try:
        db.session.remove()
    except Exception:
        pass

    try:
        bot_settings = discord_oauth.get_bot_settings(current_app.config) if settings is None else settings
        discord_res = discord_oauth.delete_voice_channel(bot_settings, claim.channel_id)
    except AssertionError:
        raise
    except Exception:
        discord_res = {"ok": False, "status": "VOICE_UNAVAILABLE", "outcome_unknown": True}

    return finalize_deletion_dispatch(
        recovery_id,
        claim.token,
        discord_res,
        claim.appointment_id,
        claim.channel_id,
    )
