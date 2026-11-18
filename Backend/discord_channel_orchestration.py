"""Internal-only Discord channel orchestration (Batch 3.2A-2B-2B).

Not an HTTP endpoint. Caller MUST first authenticate/authorize the booked user,
verify both guild memberships, provider eligibility and the session time window.
A result indicating reconciliation is required MUST NOT be automatically retried.
No automatic compensating deletion: a SELECT non-adoption check cannot protect
against another worker adopting the channel after that check.
"""
from dataclasses import dataclass
from enum import Enum

import sqlalchemy as sa
from sqlalchemy.orm import aliased
from flask import current_app

if __package__:
    from .extensions import db
    from .models import Appointment, User
    from . import discord_oauth as discord
    from . import discord_provisioning as domain
    from . import discord_lifecycle as lifecycle
else:
    from extensions import db
    from models import Appointment, User
    import discord_oauth as discord
    import discord_provisioning as domain
    import discord_lifecycle as lifecycle


class OrchestrationStatus(str, Enum):
    CREATED = "CREATED"
    ALREADY_PROVISIONED = "ALREADY_PROVISIONED"
    CLAIM_NOT_ACQUIRED = "CLAIM_NOT_ACQUIRED"
    PREFLIGHT_REJECTED = "PREFLIGHT_REJECTED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    DISCORD_REJECTED = "DISCORD_REJECTED"
    DISCORD_RATE_LIMITED = "DISCORD_RATE_LIMITED"
    UNCERTAIN = "UNCERTAIN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    DATABASE_ERROR = "DATABASE_ERROR"


@dataclass(frozen=True)
class OrchestrationResult:
    status: OrchestrationStatus
    channel_id: str | None = None
    claim_status: str | None = None
    retry_after: float | None = None
    # Only for trusted operational reconciliation, not public API output.
    orphan_candidate_channel_id: str | None = None


def _rollback():
    try:
        db.session.rollback()
    except Exception:
        pass


def _preflight(appointment_id):
    """Read scalar identity/booking data, then close the read transaction."""
    student = aliased(User)
    provider = aliased(User)
    try:
        row = db.session.execute(
            sa.select(
                Appointment.status,
                student.discord_user_id,
                provider.discord_user_id,
            )
            .join(student, student.id == Appointment.student_id)
            .join(provider, provider.id == Appointment.provider_id)
            .where(Appointment.id == appointment_id)
        ).first()
        db.session.rollback()
    except Exception:
        _rollback()
        return None, OrchestrationStatus.DATABASE_ERROR
    if row is None or row[0] not in ("accepted", "scheduled"):
        return None, OrchestrationStatus.PREFLIGHT_REJECTED
    return (row[1], row[2]), None


def _finalize_failure(appointment_id, token, outcome, *, rate_limited=False, retry_after=None):
    finalized = domain.finalize_provisioning_failure(appointment_id, token, outcome)
    reason = ("UNCERTAIN_CREATION" if outcome is domain.FailureOutcome.OUTCOME_UNKNOWN
              else "NOT_CREATED")
    recorded = lifecycle.update_attempt(appointment_id, token, reason)
    if (finalized.status is not domain.FinalizeStatus.FINALIZED
            or recorded.status is not lifecycle.RecoveryStatus.RECORDED):
        return OrchestrationResult(OrchestrationStatus.RECONCILIATION_REQUIRED)
    if outcome is domain.FailureOutcome.OUTCOME_UNKNOWN:
        return OrchestrationResult(OrchestrationStatus.UNCERTAIN)
    if rate_limited:
        return OrchestrationResult(OrchestrationStatus.DISCORD_RATE_LIMITED, retry_after=retry_after)
    return OrchestrationResult(OrchestrationStatus.DISCORD_REJECTED)


def provision_voice_channel(appointment_id, settings=None):
    """Provision an eligible appointment's channel using existing DB fencing.

    Trusted domain call ONLY, never call from an unauthenticated controller.
    The optional settings parameter is intended for trusted injection in tests.
    Successful creation without a confirmed finalization is deliberately left
    for reconciliation; this function NEVER issues a Discord DELETE.
    """
    if type(appointment_id) is not int or appointment_id <= 0:
        return OrchestrationResult(OrchestrationStatus.PREFLIGHT_REJECTED)

    participants, error = _preflight(appointment_id)
    if error:
        return OrchestrationResult(error)

    try:
        bot_settings = discord.get_bot_settings(current_app.config) if settings is None else settings
        # Validate settings and both identities BEFORE obtaining a DB claim.
        guild_id, _ = discord._validate_bot_settings(bot_settings)
        discord.build_private_voice_overwrites(guild_id, participants[0], participants[1])
        channel_name = discord.validate_channel_name(f"session-{appointment_id}")
    except (discord.DiscordConfigurationError, discord.DiscordChannelValidationError, ValueError, TypeError):
        return OrchestrationResult(OrchestrationStatus.CONFIGURATION_ERROR)

    claim = domain.claim_provisioning(appointment_id)
    if claim.status is domain.ClaimStatus.ALREADY_PROVISIONED:
        return OrchestrationResult(
            OrchestrationStatus.ALREADY_PROVISIONED,
            channel_id=claim.existing_channel_id,
        )
    if claim.status is not domain.ClaimStatus.CLAIMED or not claim.token:
        status = (OrchestrationStatus.DATABASE_ERROR if claim.status is domain.ClaimStatus.DATABASE_ERROR
                  else OrchestrationStatus.CLAIM_NOT_ACQUIRED)
        return OrchestrationResult(status, claim_status=claim.status.value)

    # A crash after the HTTP request needs a durable trace that creation MAY
    # have happened. Failure to persist it forbids calling Discord at all.
    ledger = lifecycle.begin_attempt(appointment_id, claim.token)
    if ledger.status is not lifecycle.RecoveryStatus.RECORDED:
        return OrchestrationResult(OrchestrationStatus.RECONCILIATION_REQUIRED)

    # The claim and ledger record were COMMITTED before the external call.
    try:
        created = discord.create_private_voice_channel(
            bot_settings, channel_name, participants[0], participants[1]
        )
    except Exception:
        # May have sent request: conservatively classify as unknown.
        return _finalize_failure(appointment_id, claim.token, domain.FailureOutcome.OUTCOME_UNKNOWN)

    if not isinstance(created, dict):
        return _finalize_failure(appointment_id, claim.token, domain.FailureOutcome.OUTCOME_UNKNOWN)

    if created.get("ok") is True and created.get("status") == discord.VOICE_CHANNEL_CREATED:
        channel_id = created.get("channel_id")
        # Do not trust a partial or malformed success object.
        if not domain._valid_channel_id(channel_id):
            return _finalize_failure(appointment_id, claim.token, domain.FailureOutcome.OUTCOME_UNKNOWN)
        finalized = domain.finalize_provisioning_success(appointment_id, claim.token, channel_id)
        if finalized.status is domain.FinalizeStatus.FINALIZED:
            stored = lifecycle.update_attempt(appointment_id, claim.token, "RESOLVED", channel_id=channel_id)
            # The channel itself is already bound in the appointment. A ledger
            # write failure leaves a PENDING review record; never delete it.
            return OrchestrationResult(OrchestrationStatus.CREATED, channel_id=channel_id)
        # Fencing rejection, uniqueness conflict OR commit ambiguity. Never
        # issue an automatic delete: non-adoption SELECT is not race-free.
        lifecycle.update_attempt(appointment_id, claim.token, "ORPHAN_CANDIDATE", channel_id=channel_id)
        return OrchestrationResult(
            OrchestrationStatus.RECONCILIATION_REQUIRED,
            orphan_candidate_channel_id=channel_id,
        )

    if created.get("outcome_unknown") is True:
        return _finalize_failure(appointment_id, claim.token, domain.FailureOutcome.OUTCOME_UNKNOWN)

    definitive = {
        discord.VOICE_REQUEST_REJECTED,
        discord.VOICE_BOT_FORBIDDEN,
        discord.VOICE_GUILD_NOT_FOUND,
        discord.VOICE_RATE_LIMITED,
    }
    if created.get("ok") is False and created.get("status") in definitive:
        is_rate = created["status"] == discord.VOICE_RATE_LIMITED
        return _finalize_failure(
            appointment_id, claim.token, domain.FailureOutcome.NOT_CREATED,
            rate_limited=is_rate,
            retry_after=created.get("retry_after") if is_rate else None,
        )
    return _finalize_failure(appointment_id, claim.token, domain.FailureOutcome.OUTCOME_UNKNOWN)
