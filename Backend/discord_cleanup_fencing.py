"""Durable, fail-closed cleanup coordination (NO Discord deletion).

The claim is only a marker preventing duplicate *review* work. It is NOT a
license to delete: other booking writers have not yet been fenced against
re-adoption, and the Discord side cannot enforce our database token.

In particular, expiry of an operator claim never re-enables deletion.
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


class Claim(str, Enum):
    CLAIMED = 'CLAIMED'
    REJECTED = 'REJECTED'
    INVALID = 'INVALID'
    DATABASE_ERROR = 'DATABASE_ERROR'


@dataclass(frozen=True)
class ClaimResult:
    status: Claim
    # Service-side only. Do not serialize the token to user-facing endpoints.
    token: str | None = None
    deletion_permitted: bool = False


def _rollback():
    try:
        db.session.rollback()
    except Exception:
        pass


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def claim_for_review(recovery_id, worker_id):
    """CAS claim a known terminal recovery; does not start deletion.

    No automatic takeover of a stale claim, including after a worker crash.
    Only a manual audited operator resolution may eventually supersede it.
    """
    if (type(recovery_id) is not int or recovery_id <= 0
            or not isinstance(worker_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', worker_id)):
        return ClaimResult(Claim.INVALID)
    token = secrets.token_hex(32)
    try:
        # The EXISTS predicates are part of the UPDATE. This is still NOT a
        # global write fence against future adoption; no deletion is allowed.
        rec = DiscordChannelRecovery
        active = sa.exists(sa.select(Appointment.id).where(
            Appointment.discord_channel_id == rec.channel_id,
            sa.or_(Appointment.status.notin_(('cancelled', 'completed')),
                   Appointment.discord_channel_status.in_(('provisioning', 'uncertain')))))
        conflicting = sa.exists(sa.select(sa.literal(1)).select_from(
            sa.orm.aliased(DiscordChannelRecovery, name='other')).where(
                sa.text('other.channel_id = discord_channel_recoveries.channel_id'),
                sa.text('other.id != discord_channel_recoveries.id'),
                sa.text("other.recovery_status != 'RESOLVED'")))
        changed = db.session.execute(sa.update(rec).where(
            rec.id == recovery_id,
            rec.cleanup_state == 'UNCLAIMED',
            rec.cleanup_fencing_token.is_(None),
            rec.channel_id.is_not(None),
            rec.recovery_status.in_(('PENDING', 'NEEDS_MANUAL_REVIEW')),
            rec.recovery_reason.in_(('CANCELLED', 'COMPLETED', 'ORPHAN_CANDIDATE')),
            ~active, ~conflicting,
        ).values(cleanup_state='REVIEW_CLAIMED', cleanup_fencing_token=token,
                 cleanup_worker_id=worker_id, cleanup_claimed_at=_now(),
                 updated_at=_now())).rowcount
        if changed != 1:
            _rollback()
            return ClaimResult(Claim.REJECTED)
        db.session.commit()
        return ClaimResult(Claim.CLAIMED, token)
    except Exception:
        _rollback()
        return ClaimResult(Claim.DATABASE_ERROR)


def mark_uncertain(recovery_id, token):
    """Token-fenced fail-closed transition after worker interruption.

    Never automatically clears or replaces token. Explicit operator review
    is still needed and no Discord operation is performed.
    """
    if (type(recovery_id) is not int or recovery_id <= 0 or
        not isinstance(token, str) or not re.fullmatch(r'[0-9a-f]{64}', token)):
        return False
    try:
        changed = db.session.execute(sa.update(DiscordChannelRecovery).where(
            DiscordChannelRecovery.id == recovery_id,
            DiscordChannelRecovery.cleanup_fencing_token == token,
            DiscordChannelRecovery.cleanup_state == 'REVIEW_CLAIMED',
        ).values(cleanup_state='NEEDS_MANUAL_REVIEW',
                 recovery_status='NEEDS_MANUAL_REVIEW',
                 updated_at=_now())).rowcount
        if changed != 1:
            _rollback()
            return False
        db.session.commit()
        return True
    except Exception:
        _rollback()
        return False
