"""Manual Discord recovery triage; NO Discord REST calls or automated deletion.

This domain module only maintains a durable operator audit and an optimistic
version fence.  Resolving a ledger item does not release an appointment's
provisioning attempt or imply the Discord channel has been deleted.
"""
from enum import Enum
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlalchemy as sa

if __package__:
    from .extensions import db
    from .models import Appointment, DiscordChannelRecovery
else:
    from extensions import db
    from models import Appointment, DiscordChannelRecovery


class Decision(str, Enum):
    FLAGGED = "FLAGGED"
    RESOLVED = "RESOLVED"
    STALE = "STALE"
    INVALID = "INVALID"
    DATABASE_ERROR = "DATABASE_ERROR"


@dataclass(frozen=True)
class DecisionResult:
    status: Decision


def _rollback():
    try:
        db.session.rollback()
    except Exception:
        pass


def list_recoveries(*, limit=50, offset=0):
    """Read-only paged inspection with no Discord calls or sensitive tokens."""
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError("Invalid pagination")
    try:
        rows = db.session.execute(sa.select(DiscordChannelRecovery)
            .order_by(DiscordChannelRecovery.id.desc()).offset(offset).limit(limit)).scalars().all()
        items = []
        for rec in rows:
            active = False
            if rec.channel_id:
                active = db.session.execute(sa.select(Appointment.id).where(
                    Appointment.discord_channel_id == rec.channel_id,
                    Appointment.status.in_(("accepted", "scheduled"))
                ).limit(1)).first() is not None
            items.append({"id": rec.id, "appointment_id": rec.appointment_id,
                "channel_id": rec.channel_id, "reason": rec.recovery_reason,
                "status": rec.recovery_status, "review_version": rec.review_version,
                "referenced_by_active_booking": active,
                "requires_manual_review": rec.recovery_status != "RESOLVED",
                "operator_id": rec.operator_id,
                "operator_notes": rec.operator_notes,
                "resolved_at": rec.resolved_at.isoformat() if rec.resolved_at else None})
        db.session.rollback()
        return items
    except Exception:
        _rollback()
        raise RuntimeError("Recovery inspection unavailable") from None



def get_recovery(recovery_id):
    """Full-id lookup without assuming a recovery is in the first page."""
    if type(recovery_id) is not int or recovery_id <= 0:
        raise ValueError("Invalid recovery ID")
    try:
        row = db.session.get(DiscordChannelRecovery, recovery_id)
        if row is None:
            db.session.rollback()
            return None
        active = False
        if row.channel_id:
            active = db.session.execute(sa.select(Appointment.id).where(
                Appointment.discord_channel_id == row.channel_id,
                Appointment.status.in_(("accepted", "scheduled"))
            ).limit(1)).first() is not None
        item = {"id": row.id, "appointment_id": row.appointment_id,
            "channel_id": row.channel_id, "reason": row.recovery_reason,
            "status": row.recovery_status, "review_version": row.review_version,
            "referenced_by_active_booking": active,
            "requires_manual_review": row.recovery_status != "RESOLVED",
            "operator_id": row.operator_id,
            "operator_notes": row.operator_notes,
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None}
        db.session.rollback()
        return item
    except Exception:
        _rollback()
        raise RuntimeError("Recovery inspection unavailable") from None


def decide(recovery_id, operator_id, expected_version, action, notes):
    """Version-fenced manual triage; no channel mutation, no Discord requests.

    An operator's *external verification* is required for RESOLVED.  This
    function does not itself assert channel absence or authorize a retry.
    """
    if (type(recovery_id) is not int or recovery_id <= 0 or
        type(operator_id) is not int or operator_id <= 0 or
        type(expected_version) is not int or expected_version < 0 or
        action not in ("FLAG", "RESOLVE") or
        not isinstance(notes, str) or not 12 <= len(notes.strip()) <= 2000):
        return DecisionResult(Decision.INVALID)
    target = "NEEDS_MANUAL_REVIEW" if action == "FLAG" else "RESOLVED"
    allowed = ("PENDING", "NEEDS_MANUAL_REVIEW")
    try:
        stmt = sa.update(DiscordChannelRecovery).where(
            DiscordChannelRecovery.id == recovery_id,
            DiscordChannelRecovery.review_version == expected_version,
            DiscordChannelRecovery.recovery_status.in_(allowed)
        ).values(recovery_status=target, operator_id=operator_id,
                 operator_notes=notes.strip(),
                 review_version=DiscordChannelRecovery.review_version + 1,
                 resolved_at=datetime.now(timezone.utc).replace(tzinfo=None) if action == "RESOLVE" else None,
                 updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
        changed = db.session.execute(stmt).rowcount
        if changed != 1:
            _rollback()
            return DecisionResult(Decision.STALE)
        db.session.commit()
        return DecisionResult(Decision.RESOLVED if action == "RESOLVE" else Decision.FLAGGED)
    except Exception:
        _rollback()
        return DecisionResult(Decision.DATABASE_ERROR)
