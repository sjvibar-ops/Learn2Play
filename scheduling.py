"""Database-backed reservation mutexes shared by all workers.

Lock stable User rows, not a possibly empty set of appointments. PostgreSQL
NO KEY UPDATE conflicts with another reservation lock but permits FK KEY SHARE.
"""
from datetime import timedelta
from functools import wraps
from flask import current_app, jsonify
from sqlalchemy.exc import SQLAlchemyError

if __package__:
    from .extensions import db
    from .models import Appointment, User
else:
    from extensions import db
    from models import Appointment, User

RESERVING_STATUSES = ('pending', 'accepted', 'scheduled')


class ReservationConfigurationError(Exception):
    pass


def reservation_errors(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            response = current_app.make_response(view(*args, **kwargs))
            if response.status_code >= 400:
                db.session.rollback()
            return response
        except (SQLAlchemyError, ReservationConfigurationError):
            db.session.rollback()
            return jsonify(ok=False, message='The schedule could not be reserved. Please retry.', field=None), 503
    return wrapped


def lock_schedules(*participant_ids):
    """Lock each participant in numeric order until the caller commits/rolls back.

    READ COMMITTED ensures the overlap query sees bookings committed by a lock
    holder we waited for. Fail closed on unsupported PostgreSQL isolation rather
    than making decisions from a repeatable-read snapshot taken during login.
    SQLite omits row locking and is supported for sequential functional tests.
    """
    connection = db.session.connection()
    if connection.dialect.name == 'postgresql' and connection.get_isolation_level() != 'READ COMMITTED':
        raise ReservationConfigurationError('Booking reservations require READ COMMITTED isolation.')
    users = {}
    for uid in sorted(set(participant_ids)):
        row = User.query.filter_by(id=uid).populate_existing().with_for_update(key_share=True).first()
        if row is not None:
            users[uid] = row
    return users


def participant_bookings(*participant_ids):
    """One person's teaching and learning appointments share the same schedule."""
    ids = set(participant_ids)
    return Appointment.query.filter(
        Appointment.status.in_(RESERVING_STATUSES),
        (Appointment.provider_id.in_(ids)) | (Appointment.student_id.in_(ids)),
    )


def overlaps(rows, start, duration_minutes=60):
    end = start + timedelta(minutes=duration_minutes or 60)
    return any(row.start_time < end and
               row.start_time + timedelta(minutes=row.duration_minutes or 60) > start
               for row in rows)
