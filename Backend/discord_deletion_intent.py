"""Exclusive, durable deletion-intent claim. NEVER calls or authorizes Discord DELETE.

This is a crash-stop boundary: a claimed record is never automatically reclaimed,
including after a worker crash. External ownership attestation, authorization and
actual HTTP dispatch MUST be provided by a separately reviewed implementation.

Security: deletion_token is internal; never include it in an API response or log.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
import secrets

import sqlalchemy as sa

if __package__:
    from .extensions import db
    from .models import Appointment, DiscordChannelRecovery
else:
    from extensions import db
    from models import Appointment, DiscordChannelRecovery


class DeletionClaim(str, Enum):
    CLAIMED = 'CLAIMED'
    REJECTED = 'REJECTED'
    INVALID = 'INVALID'
    DATABASE_ERROR = 'DATABASE_ERROR'


@dataclass(frozen=True)
class DeletionClaimResult:
    status: DeletionClaim
    token: str | None = None
    deletion_permitted: bool = False


def _rollback():
    try:
        db.session.rollback()
    except Exception:
        pass


def claim_deletion_intent(recovery_id, review_token):
    """Atomically fence a prepared terminal channel against competing claims.

    No HTTP call is made. No automatic retry, lease expiry or takeover exists.
    Deletion is NOT permitted by the returned result, even on CLAIMED.
    """
    if (type(recovery_id) is not int or recovery_id <= 0
            or not isinstance(review_token, str)
            or re.fullmatch(r'[0-9a-f]{64}', review_token) is None):
        return DeletionClaimResult(DeletionClaim.INVALID)

    token = secrets.token_hex(32)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    r = DiscordChannelRecovery
    a = Appointment
    other = sa.orm.aliased(r)
    ref = sa.orm.aliased(a)
    try:
        owner = sa.exists(sa.select(a.id).where(
            a.id == r.appointment_id,
            a.discord_channel_id == r.channel_id,
            a.status.in_(('cancelled', 'completed')),
            a.discord_channel_status == 'deprovisioning',
            a.discord_channel_attempt_token.is_(None),
        ))
        # No active, in-flight or unrelated reference, including bad data.
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
            r.cleanup_state == 'RETIREMENT_PREPARED',
            r.cleanup_fencing_token == review_token,
            r.channel_id.is_not(None),
            r.recovery_status.in_(('PENDING', 'NEEDS_MANUAL_REVIEW')),
            r.recovery_reason.in_(('CANCELLED', 'COMPLETED')),
            owner,
            ~conflicting_appointment,
            ~conflicting_recovery,
        ).values(
            cleanup_state='DELETION_INTENT_CLAIMED',
            cleanup_fencing_token=token,
            cleanup_claimed_at=now,
            updated_at=now,
        )).rowcount
        if changed != 1:
            _rollback()
            return DeletionClaimResult(DeletionClaim.REJECTED)
        db.session.commit()
        return DeletionClaimResult(DeletionClaim.CLAIMED, token)
    except Exception:
        _rollback()
        return DeletionClaimResult(DeletionClaim.DATABASE_ERROR)
