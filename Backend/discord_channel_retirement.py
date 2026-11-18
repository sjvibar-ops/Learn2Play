"""Commit-only Discord channel retirement preparation; NO DELETE permission.

The current cleanup review claim coordinates recovery workers but is not a
remote deletion lock. This module makes a terminal appointment's channel
unjoinable and unadoptable in a single DB transaction, token-fenced to its
recovery review claim. No caller may construe success as permission to call
Discord DELETE: external ownership proof and crash protocol are outstanding.

A prepared record remains pinned until a future, separately authorized
reconciliation process handles it. There is no automatic expiration, retry,
or network call here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re

import sqlalchemy as sa

if __package__:
    from .extensions import db
    from .models import Appointment, DiscordChannelRecovery
else:
    from extensions import db
    from models import Appointment, DiscordChannelRecovery


class Retirement(str, Enum):
    PREPARED = 'PREPARED'
    REJECTED = 'REJECTED'
    INVALID = 'INVALID'
    DATABASE_ERROR = 'DATABASE_ERROR'


@dataclass(frozen=True)
class RetirementResult:
    status: Retirement
    deletion_permitted: bool = False


def _rollback():
    try:
        db.session.rollback()
    except Exception:
        pass


def prepare_retirement(recovery_id, review_token):
    """Atomically pin ONE terminal booking and its own review claim.

    The DB write checks the recovery/appointment relationship with correlated
    EXISTS predicates, then moves both records within one commit. It rejects
    unknown/orphan channels, conflicting appointment identities, active or
    provisioning bookings, ambiguous recovery reasons, and stale tokens.

    This operation is internal-only; review tokens must not reach HTTP clients.
    No external HTTP and no authorization to perform it are supplied here.
    """
    if (type(recovery_id) is not int or recovery_id <= 0
            or not isinstance(review_token, str)
            or re.fullmatch(r'[0-9a-f]{64}', review_token) is None):
        return RetirementResult(Retirement.INVALID)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    a = Appointment
    r = DiscordChannelRecovery
    try:
        # The recovery row and its reference must match: a missing channel ID,
        # mismatched appointment or a stale review token rejects the change.
        matching_claim = sa.exists(sa.select(r.id).where(
            r.id == recovery_id,
            r.appointment_id == a.id,
            r.channel_id == a.discord_channel_id,
            r.cleanup_state == 'REVIEW_CLAIMED',
            r.cleanup_fencing_token == review_token,
            r.recovery_status.in_(('PENDING', 'NEEDS_MANUAL_REVIEW')),
            r.recovery_reason.in_(('CANCELLED', 'COMPLETED')),
        ))
        changed_appointment = db.session.execute(sa.update(a).where(
            a.discord_channel_id.is_not(None),
            a.status.in_(('cancelled', 'completed')),
            a.discord_channel_status == 'provisioned',
            matching_claim,
        ).values(discord_channel_status='deprovisioning', updated_at=now)).rowcount
        if changed_appointment != 1:
            _rollback()
            return RetirementResult(Retirement.REJECTED)

        # CAS the recovery in the SAME transaction. If another reviewer
        # changed or resolved it, rollback the appointment transition too.
        matching_appointment = sa.exists(sa.select(a.id).where(
            a.id == r.appointment_id,
            a.discord_channel_id == r.channel_id,
            a.status.in_(('cancelled', 'completed')),
            a.discord_channel_status == 'deprovisioning',
        ))
        changed_recovery = db.session.execute(sa.update(r).where(
            r.id == recovery_id,
            r.cleanup_state == 'REVIEW_CLAIMED',
            r.cleanup_fencing_token == review_token,
            r.recovery_reason.in_(('CANCELLED', 'COMPLETED')),
            r.recovery_status.in_(('PENDING', 'NEEDS_MANUAL_REVIEW')),
            matching_appointment,
        ).values(cleanup_state='RETIREMENT_PREPARED', updated_at=now)).rowcount
        if changed_recovery != 1:
            _rollback()
            return RetirementResult(Retirement.REJECTED)
        db.session.commit()
        return RetirementResult(Retirement.PREPARED)
    except Exception:
        _rollback()
        return RetirementResult(Retirement.DATABASE_ERROR)
