"""Record typed, redacted administrator audit snapshots in the caller's transaction."""

from enum import StrEnum
from uuid import UUID

if __package__:
    from .extensions import db
    from .models import AdminAuditEvent, User
else:
    from extensions import db
    from models import AdminAuditEvent, User


class AuditAction(StrEnum):
    PAYMENT_APPROVED = "payment_approved"
    PAYMENT_REJECTED = "payment_rejected"
    PAYMENT_CORRECTED = "payment_corrected"
    REFUND_APPROVED = "refund_approved"
    REFUND_CONFIRMED = "refund_confirmed"
    ADJUSTMENT_APPROVED = "adjustment_approved"
    ADMIN_PRIVILEGE_CHANGED = "admin_privilege_changed"
    ACCOUNT_ACTIVE_CHANGED = "account_active_changed"


class AuditTarget(StrEnum):
    PAYMENT = "payment"
    APPOINTMENT = "appointment"
    USER = "user"
    LEDGER_TRANSACTION = "ledger_transaction"


class AuditReason(StrEnum):
    RECEIVING_ACCOUNT_CHECKED = "receiving_account_checked"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DUPLICATE_TRANSFER = "duplicate_transfer"
    DECISION_CORRECTION = "decision_correction"
    EXTERNAL_CONFIRMATION = "external_confirmation"
    PRIVILEGE_REVIEW = "privilege_review"


SNAPSHOT_ENUMS = {
    "payment_status": {"PENDING", "SUBMITTED", "VERIFIED", "REJECTED"},
    "reconciliation_state": {
        "NOT_REQUIRED", "NEEDS_REVIEW", "NO_FUNDS_RECEIVED", "REFUND_PENDING", "REFUNDED", "RETAINED",
    },
    "appointment_status": {"pending", "accepted", "scheduled", "completed", "cancelled", "rejected"},
    "currency": {"PHP"},
}
SNAPSHOT_FLAGS = {"is_admin", "is_active"}
SNAPSHOT_AMOUNTS = {"amount_centavos", "refund_centavos", "adjustment_centavos"}


def _snapshot(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Audit snapshots must be dictionaries of allowed fields")
    result = {}
    for key, field in value.items():
        allowed = (
            (key in SNAPSHOT_ENUMS and type(field) is str and field in SNAPSHOT_ENUMS[key])
            or (key in SNAPSHOT_FLAGS and type(field) is bool)
            or (key in SNAPSHOT_AMOUNTS and type(field) is int and -(2**63) <= field < 2**63)
        )
        if not allowed:
            raise ValueError("Audit snapshot contains an unsupported field or value")
        result[key] = field
    return result


def record_admin_event(
    *, actor, action, target_type, target_id, reason=None,
    before=None, after=None, correlation_id=None, related_event_id=None, verification_event_id=None,
):
    """Stage an event using the trusted, authenticated server-side User.

    Pass the authenticated principal from server context, never a user looked
    up using a request-supplied ID. Callers authorize the action and own commit.
    Database triggers enforce same-target related events on insert.
    """
    if not isinstance(actor, User) or not actor.is_authenticated or type(actor.id) is not int or actor.id <= 0:
        raise ValueError("An authenticated server-side audit actor is required")
    if type(target_id) is not int or target_id <= 0:
        raise ValueError("Audit target must be a positive record ID")
    if correlation_id is not None and not isinstance(correlation_id, UUID):
        raise ValueError("Audit correlation ID must be a UUID")
    try:
        action = AuditAction(action).value
        target_type = AuditTarget(target_type).value
        reason = AuditReason(reason).value if reason is not None else None
    except ValueError:
        raise ValueError("Unsupported audit action, target type, or reason") from None
    if verification_event_id is not None and (
        type(verification_event_id) is not int or verification_event_id <= 0
        or action != AuditAction.PAYMENT_APPROVED or target_type != AuditTarget.PAYMENT
        or reason != AuditReason.RECEIVING_ACCOUNT_CHECKED
    ):
        raise ValueError("Invalid payment verification audit link")
    record = AdminAuditEvent(
        actor_id=actor.id, action=action,
        target_type=target_type, target_id=target_id,
        reason=reason,
        before=_snapshot(before), after=_snapshot(after),
        correlation_id=correlation_id, related_event_id=related_event_id,
        verification_event_id=verification_event_id,
    )
    db.session.add(record)
    return record
