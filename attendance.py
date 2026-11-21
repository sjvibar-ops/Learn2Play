"""Attendance transitions; caller holds the appointment row lock until commit.

Payment reviewers lock Payment then Appointment. This service only locks/updates
Appointment and reads Payment without a lock, avoiding a reverse lock order.
"""
from datetime import timedelta

if __package__:
    from .extensions import db
    from .models import AttendanceEvent, Payment, XpEvent, utcnow
else:
    from extensions import db
    from models import AttendanceEvent, Payment, XpEvent, utcnow


class AttendanceError(Exception):
    def __init__(self, message, status=409):
        self.message = message
        self.status = status


def audit(appointment, actor_id, action, note=""):
    db.session.add(AttendanceEvent(appointment_id=appointment.id,
                                  actor_id=actor_id, action=action, note=note))


def apply_attendance(appointment, actor_id, action, note=""):
    """Apply one transition without committing, for a freshly row-locked record.

    A committed dispute wins over later confirmations. A finalization committed
    first rejects a later dispute; no accepted dispute can coexist with an award.
    Legacy completed appointments remain unchanged and receive no retroactive XP.
    """
    provider = action == "complete" and actor_id == appointment.provider_id
    student = action == "confirm-attendance" and actor_id == appointment.student_id
    dispute = action == "dispute-attendance" and actor_id in (appointment.provider_id, appointment.student_id)
    if not (provider or student or dispute):
        raise AttendanceError("Only the assigned participants can perform this attendance action.", 403)
    if appointment.provider_id == appointment.student_id:
        raise AttendanceError("Attendance requires two distinct participants.", 403)
    if appointment.status not in ("accepted", "scheduled", "completed"):
        raise AttendanceError("This session cannot be confirmed.", 403)
    if appointment.start_time + timedelta(minutes=appointment.duration_minutes or 60) > utcnow():
        raise AttendanceError("Attendance can be confirmed after the scheduled session end.", 400)
    if not isinstance(note, str) or len(note.strip()) > 1000:
        raise AttendanceError("Attendance notes must be text up to 1,000 characters.", 400)
    if appointment.status == "completed":
        if dispute:
            raise AttendanceError("Finalized attendance cannot be disputed through this flow.")
        return
    if appointment.attendance_closed_at is not None:
        raise AttendanceError("Attendance was closed by an administrator without XP.")
    if dispute:
        if not 5 <= len(note.strip()) <= 1000 or "\x00" in note:
            raise AttendanceError("Enter an attendance dispute reason of 5–1,000 characters.", 400)
        if not appointment.attendance_disputed:
            appointment.attendance_disputed_at = utcnow()
            appointment.attendance_disputed_by_id = actor_id
            appointment.attendance_resolved_at = None
            appointment.attendance_resolved_by_id = None
            appointment.attendance_resolution = None
            appointment.attendance_resolution_reason = None
            audit(appointment, actor_id, "dispute_reported", note.strip())
        return
    if appointment.attendance_disputed:
        raise AttendanceError("Attendance is disputed. An administrator must review it.")
    if appointment.payment_required:
        # Plain read is intentional: do not acquire Payment after Appointment.
        status = db.session.query(Payment.status).filter_by(appointment_id=appointment.id).scalar()
        if status != "VERIFIED":
            raise AttendanceError("Payment must be verified before confirming attendance.", 403)
    if appointment.role not in ("mentor", "coach"):
        raise AttendanceError("The appointment has an unsupported session track.")
    if provider and appointment.provider_confirmed_at is None:
        appointment.provider_confirmed_at = utcnow()
        audit(appointment, actor_id, "provider_confirmed")
    if student and appointment.student_confirmed_at is None:
        appointment.student_confirmed_at = utcnow()
        audit(appointment, actor_id, "student_confirmed")
    if appointment.provider_confirmed_at and appointment.student_confirmed_at:
        appointment.status = "completed"
        db.session.add(XpEvent(user_id=appointment.student_id, appointment_id=appointment.id,
                               path="academic" if appointment.role == "mentor" else "gaming", points=50))
        audit(appointment, actor_id, "xp_awarded", "50 XP after both confirmations and required payment verification.")


def resolve_dispute(appointment, admin_id, outcome, reason):
    """Resolve a row-locked dispute without confirming attendance or awarding XP."""
    if outcome not in ("reopen", "close_without_xp"):
        raise AttendanceError("Choose reopen or close_without_xp.", 400)
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 1000 or "\x00" in reason:
        raise AttendanceError("Enter an admin decision reason of 5–1,000 characters.", 400)
    if not appointment.attendance_disputed or appointment.status == "completed":
        raise AttendanceError("Only unresolved attendance disputes can be resolved.")
    if XpEvent.query.filter_by(appointment_id=appointment.id).first() is not None:
        raise AttendanceError("This appointment already has XP; administrative investigation is required.")
    if outcome == "reopen" and appointment.status not in ("accepted", "scheduled"):
        raise AttendanceError("Cancelled or rejected bookings cannot be reopened for attendance.")
    appointment.attendance_resolved_at = utcnow()
    appointment.attendance_resolved_by_id = admin_id
    appointment.attendance_resolution = outcome
    appointment.attendance_resolution_reason = reason.strip()
    if outcome == "reopen":
        appointment.provider_confirmed_at = None
        appointment.student_confirmed_at = None
    else:
        appointment.attendance_closed_at = utcnow()
    audit(appointment, admin_id, "dispute_" + outcome, reason.strip())
