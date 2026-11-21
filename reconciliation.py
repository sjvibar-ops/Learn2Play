"""Booking-end financial recordkeeping. Never moves money or changes payment status.

Callers acquire Payment -> Appointment and commit once for the complete transition.
"""
if __package__:
    from .extensions import db
    from .models import Appointment, Payment, PaymentEvent, utcnow
else:
    from extensions import db
    from models import Appointment, Payment, PaymentEvent, utcnow

ENDED = ("cancelled", "rejected")
TRANSITIONS = {
    "NOT_REQUIRED": ("NEEDS_REVIEW",),
    "NEEDS_REVIEW": ("NO_FUNDS_RECEIVED", "REFUND_PENDING", "RETAINED"),
    "REFUND_PENDING": ("REFUNDED",),
}
STATES = ("NOT_REQUIRED", "NEEDS_REVIEW", "NO_FUNDS_RECEIVED", "REFUND_PENDING", "REFUNDED", "RETAINED")


class ReconciliationError(Exception):
    def __init__(self, message, status=409):
        self.message, self.status = message, status


def audit_functions():
    # Lazy import: payments imports the existing routes' admin guard.
    if __package__:
        from .payments import preserve_legacy_snapshot, record_event
    else:
        from payments import preserve_legacy_snapshot, record_event
    return preserve_legacy_snapshot, record_event


def locked_payment(booking_id):
    return Payment.query.filter_by(appointment_id=booking_id).populate_existing().with_for_update().first()


def lock_booking_end(booking_id):
    payment = locked_payment(booking_id)
    appointment = Appointment.query.filter_by(id=booking_id).populate_existing().with_for_update().first()
    # A booking/payment creation may commit between these READ COMMITTED reads.
    # Never acquire a newly discovered Payment lock after Appointment: retry
    # from the correct lock order rather than ending without financial tracking.
    if payment is None and appointment is not None:
        if db.session.query(Payment.id).filter_by(appointment_id=booking_id).first() is not None:
            raise ReconciliationError("This booking/payment changed. Reload and retry.")
    return payment, appointment


def text_field(value, label, maximum=500, minimum=5):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum or any(ord(c) < 32 and c not in "\r\n\t" for c in value):
        raise ReconciliationError(f"Enter {label} of {minimum}-{maximum} characters.", 400)
    return value.strip()


def append_reconciliation(payment, appointment, actor_id, event_type, previous, **fields):
    _, record_event = audit_functions()
    payment.reconciliation_version += 1
    payment.reconciliation_updated_at = utcnow()
    payment.reconciliation_updated_by_id = actor_id
    record_event(payment, event_type, payment.status, actor_id,
                 reconciliation_previous_state=previous,
                 reconciliation_state=payment.reconciliation_state,
                 reconciliation_version=payment.reconciliation_version,
                 reconciliation_explanation=payment.reconciliation_explanation,
                 booking_status=appointment.status, booking_ended_at=appointment.ended_at,
                 booking_ending_reason=appointment.ending_reason, **fields)


def end_booking(appointment, payment, actor_id, outcome, reason):
    """Record a real observed booking end and flag evidence conservatively."""
    if outcome not in ENDED or appointment.status not in ("pending", "accepted", "scheduled"):
        raise ReconciliationError("This booking cannot be ended in its current state.")
    if outcome == "rejected" and appointment.status != "pending":
        raise ReconciliationError("Only pending bookings can be rejected.")
    appointment.status = outcome
    appointment.ended_at = utcnow()
    appointment.ended_by_id = actor_id
    appointment.ending_reason = text_field(reason, "a booking ending reason")
    if payment is None:
        return
    preserve_legacy_snapshot, _ = audit_functions()
    preserve_legacy_snapshot(payment, actor_id)
    previous = payment.reconciliation_state
    evidence = (payment.status != "PENDING" or payment.receipt_path or payment.submitted_at
                or payment.reviewed_at or payment.submission_revision > 0
                or PaymentEvent.query.filter_by(payment_id=payment.id).first() is not None)
    payment.reconciliation_state = "NEEDS_REVIEW" if evidence else "NOT_REQUIRED"
    payment.reconciliation_explanation = (
        "This booking ended. Its payment requires independent financial review; no refund is recorded."
        if evidence else "No payment submission or receipt of funds is recorded. Contact an admin if you sent funds outside the application."
    )
    append_reconciliation(payment, appointment, actor_id, "BOOKING_ENDED", previous)


def decide(payment, appointment, actor_id, data):
    """Record an admin's external-confirmation attestation, not financial authority."""
    if appointment.status not in ENDED:
        raise ReconciliationError("Only cancelled or rejected bookings can be reconciled.")
    version = data.get("expected_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != payment.reconciliation_version:
        raise ReconciliationError("This reconciliation changed. Reload before recording a decision.")
    outcome = data.get("state")
    if outcome not in TRANSITIONS.get(payment.reconciliation_state, ()):
        raise ReconciliationError("This reconciliation transition is not available.")
    reason = text_field(data.get("reason"), "an internal decision reason")
    explanation = text_field(data.get("student_explanation"), "a student-visible explanation")
    notes = text_field(data.get("confirmation_notes"), "documented independent-check or external-confirmation notes", maximum=1000)
    evidence = data.get("evidence_reference", "")
    if not isinstance(evidence, str) or len(evidence.strip()) > 200 or any(ord(c) < 32 for c in evidence):
        raise ReconciliationError("Evidence reference must be text up to 200 characters.", 400)
    flags = {name: data.get(name) is True for name in (
        "funds_received_confirmed", "independent_check_confirmed", "authorized_refund_confirmed",
        "account_holder_refund_confirmed", "authorized_retention_confirmed")}
    authority_name = authority_capacity = None
    if outcome == "NEEDS_REVIEW":
        pass  # Document newly reported evidence without asserting money was received.
    elif outcome == "NO_FUNDS_RECEIVED":
        if not flags["independent_check_confirmed"]:
            raise ReconciliationError("Document an independent check of the authorized account/transaction records confirming no funds were received.", 400)
    else:
        authority_name = text_field(data.get("authority_name"), "the external confirmer's identity/reference", maximum=200, minimum=2)
        authority_capacity = data.get("authority_capacity")
        if authority_capacity not in ("account_holder", "authorized_financial_decision_maker"):
            raise ReconciliationError("Record the external confirmer's authority/capacity.", 400)
        if outcome == "REFUNDED":
            if authority_capacity != "account_holder" or not flags["account_holder_refund_confirmed"]:
                raise ReconciliationError("Explicit confirmation from the authorized account holder that the external refund was completed is required.", 400)
        elif outcome == "REFUND_PENDING":
            if not flags["funds_received_confirmed"] or not flags["authorized_refund_confirmed"]:
                raise ReconciliationError("Confirm funds were received and an authorized decision-maker approved the refund externally.", 400)
        elif outcome == "RETAINED":
            if not flags["funds_received_confirmed"] or not flags["authorized_retention_confirmed"]:
                raise ReconciliationError("Confirm funds were received and the responsible authorized decision-maker approved retention externally.", 400)
    relevant = {
        "NEEDS_REVIEW": (), "NO_FUNDS_RECEIVED": ("independent_check_confirmed",),
        "REFUND_PENDING": ("funds_received_confirmed", "authorized_refund_confirmed"),
        "REFUNDED": ("account_holder_refund_confirmed",),
        "RETAINED": ("funds_received_confirmed", "authorized_retention_confirmed"),
    }[outcome]
    if outcome == "NO_FUNDS_RECEIVED" and flags["funds_received_confirmed"]:
        raise ReconciliationError("A no-funds finding cannot also assert that funds were received.", 400)
    flags = {name: value if name in relevant else None for name, value in flags.items()}
    preserve_legacy_snapshot, _ = audit_functions()
    preserve_legacy_snapshot(payment, actor_id)
    previous = payment.reconciliation_state
    payment.reconciliation_state = outcome
    payment.reconciliation_explanation = explanation
    append_reconciliation(payment, appointment, actor_id, outcome, previous,
                          decision_reason=reason, confirmation_notes=notes,
                          evidence_reference=evidence.strip() or None,
                          authority_name=authority_name, authority_capacity=authority_capacity, **flags)
