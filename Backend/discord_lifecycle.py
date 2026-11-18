"""Durable Discord channel recovery ledger (Feature 3).

Safety properties:
- Writes only to the database, never calls Discord or Supabase APIs.
- Uncertain provisioning is never automatically unlocked or retried.
- This module NEVER deletes a Discord channel: checking non-adoption is not
  sufficient to prove a channel cannot be concurrently adopted.
- The review function is an operator aid, not an automated cleanup worker.
"""
import hashlib
from dataclasses import dataclass
from enum import Enum
import sqlalchemy as sa

if __package__:
    from .extensions import db
    from .models import Appointment, DiscordChannelRecovery
else:
    from extensions import db
    from models import Appointment, DiscordChannelRecovery


class RecoveryStatus(str, Enum):
    RECORDED = "RECORDED"
    ALREADY_RECORDED = "ALREADY_RECORDED"
    DATABASE_ERROR = "DATABASE_ERROR"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True)
class RecoveryResult:
    status: RecoveryStatus


def _rollback():
    try:
        db.session.rollback()
    except Exception:
        pass


def attempt_key(appointment_id, token):
    """Idempotent ledger key without exposing attempt tokens to records/logs."""
    if type(appointment_id) is not int or appointment_id <= 0:
        raise ValueError("Invalid appointment ID")
    if not isinstance(token, str) or len(token) != 64 or any(ch not in '0123456789abcdef' for ch in token):
        raise ValueError("Invalid attempt token")
    return 'attempt:' + hashlib.sha256(f'{appointment_id}:{token}'.encode()).hexdigest()


def _record_in_transaction(appointment_id, event_key, reason, *, channel_id=None):
    """Insert once; calling code owns commit/rollback and transaction."""
    exists = db.session.execute(sa.select(DiscordChannelRecovery.id).where(
        DiscordChannelRecovery.event_key == event_key)).scalar_one_or_none()
    if exists is not None:
        return False
    db.session.add(DiscordChannelRecovery(
        event_key=event_key, appointment_id=appointment_id,
        channel_id=channel_id, recovery_reason=reason,
        recovery_status='PENDING'))
    return True


def queue_terminal_cleanup(appointment, reason):
    """Add to the caller's booking transaction; does NOT commit independently.

    Caller must commit the appointment state and recovery row together.
    On a database error the entire booking transaction must roll back.
    """
    if reason not in ('CANCELLED', 'COMPLETED'):
        raise ValueError('Invalid terminal recovery reason')
    if appointment.status != reason.lower():
        raise ValueError('Booking must already be terminal')
    if not appointment.discord_channel_id:
        return False
    event_key = f'terminal:{appointment.id}:{reason}'
    return _record_in_transaction(appointment.id, event_key, reason,
                                  channel_id=appointment.discord_channel_id)


def begin_attempt(appointment_id, token):
    """Persist the claim in a recovery ledger BEFORE the Discord POST.

    Even if the process crashes later, the row documents an external-call
    window. A failed or ambiguous DB commit forbids external creation.
    """
    try:
        key = attempt_key(appointment_id, token)
    except ValueError:
        return RecoveryResult(RecoveryStatus.INVALID_INPUT)
    try:
        new = _record_in_transaction(appointment_id, key, 'ATTEMPT_STARTED')
        db.session.commit()
        return RecoveryResult(RecoveryStatus.RECORDED if new else RecoveryStatus.ALREADY_RECORDED)
    except Exception:
        _rollback()
        return RecoveryResult(RecoveryStatus.DATABASE_ERROR)


def update_attempt(appointment_id, token, reason, *, channel_id=None):
    """Persist the result. Failure leaves the existing PENDING record in place.

    On a confirmed finalized appointment, RESOLVED suppresses review. On any
    ambiguous result, retain review state. No automatic channel cleanup.
    """
    allowed = ('RESOLVED', 'NOT_CREATED', 'UNCERTAIN_CREATION', 'ORPHAN_CANDIDATE')
    if reason not in allowed:
        return RecoveryResult(RecoveryStatus.INVALID_INPUT)
    if channel_id is not None and (not isinstance(channel_id, str) or not channel_id.isdecimal() or not 15 <= len(channel_id) <= 25):
        return RecoveryResult(RecoveryStatus.INVALID_INPUT)
    try:
        key = attempt_key(appointment_id, token)
    except ValueError:
        return RecoveryResult(RecoveryStatus.INVALID_INPUT)
    try:
        row = db.session.execute(sa.select(DiscordChannelRecovery).where(
            DiscordChannelRecovery.event_key == key)).scalar_one_or_none()
        if row is None:
            _rollback()
            return RecoveryResult(RecoveryStatus.DATABASE_ERROR)
        # Never erase evidence of known Discord channel creation.
        if row.channel_id and channel_id and row.channel_id != channel_id:
            _rollback()
            return RecoveryResult(RecoveryStatus.INVALID_INPUT)
        if row.recovery_reason == 'ORPHAN_CANDIDATE' and reason != 'ORPHAN_CANDIDATE':
            _rollback()
            return RecoveryResult(RecoveryStatus.INVALID_INPUT)
        row.recovery_reason = reason
        row.channel_id = channel_id or row.channel_id
        row.recovery_status = 'RESOLVED' if reason in ('RESOLVED', 'NOT_CREATED') else 'PENDING'
        db.session.commit()
        return RecoveryResult(RecoveryStatus.RECORDED)
    except Exception:
        _rollback()
        return RecoveryResult(RecoveryStatus.DATABASE_ERROR)


def review_pending_recoveries(limit=50):
    """READ-ONLY operator inspection. Never auto-finalizes or deletes channels.

    For a known channel, report if any appointment references it. Treat every
    unknown/missing channel as requiring manual Discord-side verification.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('Limit must be 1..500')
    try:
        rows = db.session.execute(sa.select(DiscordChannelRecovery).where(
            DiscordChannelRecovery.recovery_status == 'PENDING'
        ).order_by(DiscordChannelRecovery.id).limit(limit)).scalars().all()
        result = []
        for item in rows:
            refs = []
            if item.channel_id:
                refs = db.session.execute(sa.select(Appointment.id, Appointment.status).where(
                    Appointment.discord_channel_id == item.channel_id)).all()
            result.append({
                'recovery_id': item.id,
                'appointment_id': item.appointment_id,
                'reason': item.recovery_reason,
                'channel_id': item.channel_id,
                'referenced_by_active_booking': any(status in ('accepted', 'scheduled') for _, status in refs),
                'requires_manual_review': True,
            })
        db.session.rollback()
        return result
    except Exception:
        _rollback()
        raise RuntimeError('Recovery inspection unavailable') from None
