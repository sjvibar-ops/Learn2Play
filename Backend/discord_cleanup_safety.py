"""Fail-closed, read-only preflight for Discord channel cleanup.

A CLEANUP_CANDIDATE is NEVER permission to call Discord DELETE. A database
snapshot cannot prove exclusive ownership across concurrent transactions or
external Discord state. This module has no worker, lease, retry, or REST calls.
"""
from dataclasses import dataclass
from enum import Enum
import sqlalchemy as sa

if __package__:
    from .extensions import db
    from .models import Appointment, DiscordChannelRecovery
else:
    from extensions import db
    from models import Appointment, DiscordChannelRecovery


class Safety(str, Enum):
    UNKNOWN = 'UNKNOWN'
    ACTIVE_REFERENCE = 'ACTIVE_REFERENCE'
    AMBIGUOUS_OWNERSHIP = 'AMBIGUOUS_OWNERSHIP'
    CLEANUP_CANDIDATE = 'CLEANUP_CANDIDATE'


@dataclass(frozen=True)
class CleanupAssessment:
    recovery_id: int
    status: Safety
    deletion_permitted: bool = False


def assess_recovery(recovery_id):
    """Read-only diagnostic; every outcome forbids deletion.

    This deliberately does not check Discord REST, claim ownership, or mark
    records resolved. Calling code must NOT use this as a delete gate.
    """
    if type(recovery_id) is not int or recovery_id <= 0:
        raise ValueError('Invalid recovery ID')
    try:
        recovery = db.session.get(DiscordChannelRecovery, recovery_id)
        if recovery is None:
            result = CleanupAssessment(recovery_id, Safety.UNKNOWN)
        elif (not recovery.channel_id or recovery.recovery_reason in
              ('ATTEMPT_STARTED', 'UNCERTAIN_CREATION')):
            result = CleanupAssessment(recovery_id, Safety.AMBIGUOUS_OWNERSHIP)
        else:
            references = db.session.execute(sa.select(
                Appointment.id, Appointment.status, Appointment.discord_channel_status
            ).where(Appointment.discord_channel_id == recovery.channel_id)).all()
            unresolved = db.session.execute(sa.select(DiscordChannelRecovery.id).where(
                DiscordChannelRecovery.channel_id == recovery.channel_id,
                DiscordChannelRecovery.id != recovery_id,
                DiscordChannelRecovery.recovery_status != 'RESOLVED'
            ).limit(1)).first()
            if any(status in ('accepted', 'scheduled') for _, status, _ in references):
                result = CleanupAssessment(recovery_id, Safety.ACTIVE_REFERENCE)
            elif (recovery.recovery_status not in ('PENDING', 'NEEDS_MANUAL_REVIEW')
                  or unresolved is not None
                  or any(channel_status == 'provisioning' for _, _, channel_status in references)
                  or any(status not in ('cancelled', 'completed') for _, status, _ in references)
                  or recovery.recovery_reason not in ('CANCELLED', 'COMPLETED', 'ORPHAN_CANDIDATE')):
                result = CleanupAssessment(recovery_id, Safety.AMBIGUOUS_OWNERSHIP)
            else:
                # Even with no active DB references, adoption or a Discord-side
                # external change can race this snapshot.
                result = CleanupAssessment(recovery_id, Safety.CLEANUP_CANDIDATE)
        db.session.rollback()
        return result
    except Exception:
        db.session.rollback()
        raise RuntimeError('Cleanup assessment unavailable') from None
