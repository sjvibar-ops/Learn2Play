"""Mutual changes to an existing appointment, preserving its payment."""

from datetime import date, datetime, time, timedelta, timezone

from flask import Blueprint, abort, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import inspect

if __package__:
    from .extensions import db
    from .models import Appointment, MentorAvailability, MentorUnavailableDate, RescheduleProposal, utcnow
    from .routes import provider_approved_for_role
    from .scheduling import lock_schedules, overlaps, participant_bookings, reservation_errors
else:
    from extensions import db
    from models import Appointment, MentorAvailability, MentorUnavailableDate, RescheduleProposal, utcnow
    from routes import provider_approved_for_role
    from scheduling import lock_schedules, overlaps, participant_bookings, reservation_errors


reschedules = Blueprint("reschedules", __name__, url_prefix="/api/bookings")


@reschedules.before_request
def require_schema():
    if current_user.is_authenticated and not inspect(db.engine).has_table("reschedule_proposals"):
        return failure("Rescheduling requires a database upgrade.", 503)
    return None


@reschedules.after_request
def prevent_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def failure(message, status=400):
    return jsonify(ok=False, message=message), status


def participant(appointment):
    return appointment and current_user.id in (appointment.student_id, appointment.provider_id)


def pending(appointment_id):
    return RescheduleProposal.query.filter_by(appointment_id=appointment_id, status="pending").first()


def snapshot(proposal):
    return {
        "id": proposal.id,
        "requested_by": proposal.requested_by,
        "decided_by": proposal.decided_by,
        "previous_start": proposal.previous_start.replace(tzinfo=timezone.utc).isoformat(),
        "proposed_start": proposal.proposed_start.replace(tzinfo=timezone.utc).isoformat(),
        "status": proposal.status,
    }


def can_move(appointment):
    return (
        appointment.status in ("accepted", "scheduled")
        and appointment.start_time > utcnow()
        and not appointment.attendance_disputed
        and appointment.attendance_disputed_at is None
        and appointment.attendance_closed_at is None
        and appointment.provider_confirmed_at is None
        and appointment.student_confirmed_at is None
        and provider_approved_for_role(appointment.provider, appointment.role)
        and appointment.student.is_active
    )


def available(appointment, start):
    if start <= utcnow() or start > utcnow() + timedelta(days=60) or start == appointment.start_time:
        return False
    local_start = start + timedelta(hours=8)
    local_end = local_start + timedelta(minutes=appointment.duration_minutes or 60)
    day = local_start.date()
    if MentorUnavailableDate.query.filter_by(mentor_id=appointment.provider_id, unavailable_date=day).first():
        return False
    windows = MentorAvailability.query.filter_by(mentor_id=appointment.provider_id, weekday=day.weekday()).all()
    if not any(datetime.combine(day, time.fromisoformat(w.start_time)) <= local_start
               and datetime.combine(day, time.fromisoformat(w.end_time)) >= local_end for w in windows):
        return False
    others = participant_bookings(appointment.student_id, appointment.provider_id)
    return not overlaps(others.filter(Appointment.id != appointment.id).all(), start, appointment.duration_minutes)


@reschedules.get("/reschedule-proposals")
@login_required
def list_proposals():
    results = RescheduleProposal.query.join(Appointment).filter(
        RescheduleProposal.status == "pending",
        (Appointment.student_id == current_user.id) | (Appointment.provider_id == current_user.id),
    ).all()
    return jsonify(ok=True, proposals={str(row.appointment_id): snapshot(row) for row in results})


@reschedules.get("/<int:booking_id>/reschedule")
@login_required
def history(booking_id):
    if not participant(db.session.get(Appointment, booking_id)):
        abort(404)
    rows = RescheduleProposal.query.filter_by(appointment_id=booking_id).order_by(RescheduleProposal.id).all()
    return jsonify(ok=True, history=[snapshot(row) for row in rows])


@reschedules.post("/<int:booking_id>/reschedule")
@login_required
@reservation_errors
def change(booking_id):
    actor_id = current_user.id
    appointment = db.session.get(Appointment, booking_id)
    if not participant(appointment):
        abort(404)
    lock_schedules(appointment.student_id, appointment.provider_id)
    appointment = db.session.get(Appointment, booking_id, populate_existing=True, with_for_update=True)
    if not participant(appointment):
        abort(404)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return failure("Send a rescheduling request.")
    action = data.get("action")
    proposal = pending(booking_id)
    if action == "propose":
        if proposal is not None:
            return failure("Resolve the existing proposal first.", 409)
        if not can_move(appointment):
            return failure("Only an upcoming, confirmed appointment without an attendance dispute can move.", 409)
        try:
            day = date.fromisoformat(data.get("date", ""))
            slot = time.fromisoformat(data.get("time", ""))
            if slot.tzinfo is not None or slot.minute or slot.second or slot.microsecond:
                raise ValueError
            start = datetime.combine(day, slot) - timedelta(hours=8)
            valid_slot = available(appointment, start)
        except (TypeError, ValueError, OverflowError):
            return failure("Choose a valid whole-hour Philippine date and time.")
        if not valid_slot:
            return failure("That time is unavailable.", 409)
        proposal = RescheduleProposal(appointment_id=booking_id, requested_by=actor_id,
                                      previous_start=appointment.start_time, proposed_start=start)
        db.session.add(proposal)
        db.session.commit()
        return jsonify(ok=True, proposal=snapshot(proposal)), 201
    if action not in ("accept", "reject", "withdraw"):
        return failure("Choose a valid reschedule action.")
    if proposal is None or type(data.get("proposal_id")) is not int or data["proposal_id"] != proposal.id:
        return failure("That proposal is no longer pending.", 409)
    if (proposal.requested_by == actor_id) != (action == "withdraw"):
        return failure("Only the other participant can decide a proposal.", 403)
    if action == "accept":
        if (not can_move(appointment) or appointment.start_time != proposal.previous_start
                or not available(appointment, proposal.proposed_start)):
            return failure("The appointment or proposed time is no longer available.", 409)
        appointment.start_time = proposal.proposed_start
    proposal.status = {"accept": "accepted", "reject": "rejected", "withdraw": "withdrawn"}[action]
    proposal.decided_by = actor_id
    proposal.decided_at = utcnow()
    db.session.commit()
    return jsonify(ok=True, proposal=snapshot(proposal))
