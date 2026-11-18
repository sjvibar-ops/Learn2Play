import json
from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
from sqlalchemy import event
from werkzeug.security import check_password_hash, generate_password_hash

if __package__:
    from .extensions import db
else:
    from extensions import db


def utcnow():
    """Return timezone-naive UTC timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    # Account information
    display_name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)

    # Roles: student, mentor (Teacher), and/or coach
    roles_json = db.Column(db.Text, nullable=False, default='["student"]')

    # Profile information
    bio = db.Column(db.Text, nullable=True)
    profile_picture = db.Column(db.String(255), nullable=True)
    topics_json = db.Column(
        db.Text, nullable=True, default='[]', server_default='[]'
    )

    # Legacy rate column retained for backward compatibility
    hourly_rate = db.Column(
        db.Integer, nullable=True, default=300, server_default=db.text("300")
    )

    # Independent decimal hourly rates for providers
    teaching_rate = db.Column(db.Numeric(10, 2), nullable=True)
    coaching_rate = db.Column(db.Numeric(10, 2), nullable=True)

    # Administrative access is separate from user-selected roles
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Discord identity (Batch 3.1B-1). All three are NULL until the user links
    # Discord. The Discord ID is a snowflake kept as a STRING (never a float or
    # JS number). Uniqueness is enforced by the database (unique index).
    # OAuth access/refresh tokens are never stored.
    discord_user_id = db.Column(db.String(32), unique=True, nullable=True, index=True)
    discord_username = db.Column(db.String(100), nullable=True)
    discord_linked_at = db.Column(db.DateTime, nullable=True)

    @property
    def roles(self):
        try:
            return json.loads(self.roles_json)
        except Exception:
            return ["student"]

    @roles.setter
    def roles(self, value):
        allowed_roles = {"student", "mentor", "coach"}
        if not isinstance(value, list):
            raise ValueError("Roles must be a list.")
        if any(not isinstance(role, str) or role not in allowed_roles for role in value):
            raise ValueError("Please select valid roles.")
        self.roles_json = json.dumps(list(dict.fromkeys(value)))

    @property
    def topics(self):
        try:
            return json.loads(self.topics_json) if self.topics_json else []
        except Exception:
            return []

    @topics.setter
    def topics(self, value):
        if isinstance(value, list):
            self.topics_json = json.dumps(list(dict.fromkeys([t.strip() for t in value if t.strip()])))
        elif isinstance(value, str):
            items = [t.strip() for t in value.split(",") if t.strip()]
            self.topics_json = json.dumps(list(dict.fromkeys(items)))
        else:
            self.topics_json = '[]'

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.email}>"


class RoleApplication(db.Model):
    """A request for a role that needs admin approval (mentor/teacher or coach)."""
    __tablename__ = "role_applications"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False)                       # mentor | coach
    bio = db.Column(db.Text, nullable=False)
    links = db.Column(db.Text, default="")                                # one URL per line
    # pending | approved | rejected | revision_required. "not_submitted" is never
    # stored: it is the absence of a row for (user_id, role).
    status = db.Column(db.String(20), nullable=False, default="pending")
    review_note = db.Column(db.String(500))
    reviewed_by = db.Column(db.Integer)
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # Time of the latest (re)submission. NULL on legacy rows; readers fall back
    # to created_at. created_at keeps the time of the first submission.
    submitted_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (db.UniqueConstraint("user_id", "role"),)


class Credential(db.Model):
    """An uploaded proof file (certificate, license, diploma, award)."""
    __tablename__ = "credentials"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # The application this file was uploaded for. NULL means "application
    # unknown" (every row that existed before Batch 2.1B); such rows are never
    # assigned to an application by guesswork.
    application_id = db.Column(
        db.Integer,
        db.ForeignKey("role_applications.id", name="fk_credentials_application_id"),
        nullable=True,
        index=True,
    )


class RoleApplicationHistory(db.Model):
    """Append-only log of (re)submissions of a role application.

    RoleApplication holds only the latest state. Each submission through
    POST /api/me/applications/<role> appends one row here that records the
    transition and a snapshot of the state it replaced (including the previous
    review decision), so a resubmission never destroys an earlier decision.
    Rows are never updated or deleted by the application.
    """
    __tablename__ = "role_application_history"

    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(
        db.Integer,
        db.ForeignKey("role_applications.id", name="fk_role_application_history_application_id"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", name="fk_role_application_history_user_id"),
        nullable=False,
        index=True,
    )
    role = db.Column(db.String(20), nullable=False)          # mentor | coach
    event_type = db.Column(db.String(20), nullable=False)    # submitted | resubmitted
    from_status = db.Column(db.String(20), nullable=False)   # not_submitted | rejected | revision_required
    to_status = db.Column(db.String(20), nullable=False)     # pending

    # Snapshot of the state replaced by this transition (all NULL for the first
    # submission, where nothing was replaced).
    prior_bio = db.Column(db.Text, nullable=True)
    prior_links = db.Column(db.Text, nullable=True)
    prior_review_note = db.Column(db.String(500), nullable=True)
    prior_reviewed_by = db.Column(db.Integer, nullable=True)
    prior_reviewed_at = db.Column(db.DateTime, nullable=True)
    prior_submitted_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


@event.listens_for(RoleApplicationHistory, "before_update")
def _history_rows_are_immutable(mapper, connection, target):
    raise ValueError("RoleApplicationHistory rows are append-only.")


@event.listens_for(RoleApplicationHistory, "before_delete")
def _history_rows_are_permanent(mapper, connection, target):
    raise ValueError("RoleApplicationHistory rows are append-only.")


# Lifecycle values for Appointment.discord_channel_status (Batch 3.2A-2A).
# Schema only: nothing in this batch transitions between them.
#   unprovisioned - no Discord channel has been requested (default; every
#                   appointment that existed before this batch)
#   provisioning  - a creation attempt is in flight / was claimed
#   provisioned   - a channel exists and discord_channel_id is set
#   failed        - creation definitively failed; no channel is known to exist
#   uncertain     - outcome unknown (e.g. timeout); a channel MAY exist and
#                   needs explicit reconciliation, never a blind retry
#   deprovisioned - the channel was deliberately removed
DISCORD_CHANNEL_STATUSES = (
    "unprovisioned",
    "provisioning",
    "provisioned",
    "failed",
    "uncertain",
    "deprovisioning",  # Terminal channel retired locally; no new join or adoption.
    "deprovisioned",
)

# FUTURE CONCURRENCY REQUIREMENTS (document only; implemented in Batch 3.2A-2B)
# for whichever code later claims/finalizes a channel for an Appointment:
#   * An in-process Python lock alone is insufficient (multiple workers).
#   * Read-check-write is not atomic across concurrent requests. Claim with a
#     database-side conditional UPDATE (compare-and-set) and check the affected
#     row count.
#   * Persist a unique attempt identifier (or equivalent fencing token) before
#     the external HTTP creation call where necessary.
#   * Never hold a database transaction open across Discord HTTP calls.
#   * Never blindly retry a stale "provisioning" attempt: the channel might
#     already exist.
#   * "uncertain" requires explicit reconciliation.
#   * Re-check booking status and attempt ownership before finalizing.
#   * An "outcome unknown" response is not a definitive failure.


class Appointment(db.Model):
    """A scheduled 1-on-1 session between a student and a provider (teacher or coach)."""
    __tablename__ = "appointments"

    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    role = db.Column(db.String(20), nullable=False, default="mentor")     # mentor | coach
    topic = db.Column(db.String(120), nullable=False)                     # e.g. Calculus, Valorant
    start_time = db.Column(db.DateTime, nullable=False, index=True)       # UTC naive
    duration_minutes = db.Column(db.Integer, nullable=False, default=60)

    # Status: scheduled | completed | cancelled
    status = db.Column(db.String(20), nullable=False, default="scheduled")

    # Financial snapshot locked at booking time
    price = db.Column(db.Numeric(10, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default="PHP")

    # Logistics
    location_type = db.Column(db.String(50), default="Online")            # Online | In-person
    meeting_link = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    meeting_room = db.Column(db.String(80), nullable=True, unique=True)

    # Discord private voice channel tracking (Batch 3.2A-2A, schema only).
    # Backend-assigned; never accepted from client input. The channel ID is a
    # Discord snowflake kept as a STRING. Uniqueness is enforced by a unique
    # index (many NULLs allowed). Timestamps are naive UTC. Valid status values
    # are listed in DISCORD_CHANNEL_STATUSES. Existing rows read as
    # "unprovisioned" through the server default.
    discord_channel_id = db.Column(db.String(32), nullable=True, unique=True, index=True)
    discord_channel_created_at = db.Column(db.DateTime, nullable=True)
    discord_channel_status = db.Column(
        db.String(20),
        nullable=False,
        default="unprovisioned",
        server_default="unprovisioned",
    )
    discord_channel_last_attempt_at = db.Column(db.DateTime, nullable=True)

    # Batch 3.2A-2B-1 (schema only): the attempt token identifies the one
    # provisioning attempt that is currently authorized to write the outcome
    # for this appointment's Discord channel. NULL means no attempt is
    # authorized (every existing row and every new row). Backend-assigned
    # only: never accepted from, or returned to, client input. Nothing in this
    # batch generates, assigns, claims or checks it. A later batch
    # (3.2A-2B-2) will generate tokens with secrets.token_hex(32) (64 hex
    # characters) and finalize an attempt only while the stored token still
    # matches, so a stale worker cannot overwrite a newer outcome. No unique
    # index: the token is only ever compared within a single row.
    discord_channel_attempt_token = db.Column(db.String(64), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    # Relationships
    provider = db.relationship("User", foreign_keys=[provider_id], backref="provider_appointments")
    student = db.relationship("User", foreign_keys=[student_id], backref="student_appointments")

    def __repr__(self):
        return f"<Appointment #{self.id} {self.topic} ({self.status})>"


class DiscordChannelRecovery(db.Model):
    """Durable safety ledger; never grants permission to DELETE a channel.

    An ATTEMPT_STARTED record is committed before attempting external creation.
    A reconciliation worker may classify records for operator inspection but is
    expressly forbidden from issuing Discord DELETE or retrying provisioning.
    """
    __tablename__ = "discord_channel_recoveries"

    id = db.Column(db.Integer, primary_key=True)
    event_key = db.Column(db.String(100), nullable=False, unique=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False, index=True)
    channel_id = db.Column(db.String(32), nullable=True, index=True)
    recovery_reason = db.Column(db.String(32), nullable=False)
    recovery_status = db.Column(db.String(32), nullable=False, default="PENDING", server_default="PENDING")
    last_error_code = db.Column(db.String(48), nullable=True)
    # Operator decisions are durable and auditable; they DO NOT authorize
    # automatic Discord deletion or unlock uncertain creation attempts.
    # Audit-only claim for coordination. It NEVER authorizes Discord DELETE.
    cleanup_fencing_token = db.Column(db.String(64), nullable=True)
    cleanup_state = db.Column(db.String(32), nullable=False, default="UNCLAIMED", server_default="UNCLAIMED")
    cleanup_claimed_at = db.Column(db.DateTime, nullable=True)
    cleanup_worker_id = db.Column(db.String(64), nullable=True)
    deletion_dispatched_at = db.Column(db.DateTime, nullable=True)
    deletion_http_status = db.Column(db.Integer, nullable=True)
    operator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    operator_notes = db.Column(db.Text, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)
    review_version = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    appointment = db.relationship("Appointment", foreign_keys=[appointment_id])


class Report(db.Model):
    """A report filed by any user against any other user."""
    __tablename__ = "reports"

    id = db.Column(db.Integer, primary_key=True)
    reporter_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    reported_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    # Optional: link to the appointment/session during which this happened
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=True, index=True)

    reason = db.Column(db.String(100), nullable=False)   # short category: Harassment, Cheating, etc.
    details = db.Column(db.Text, nullable=True)          # free-text description

    # Admin review: open | reviewed | dismissed
    status = db.Column(db.String(20), nullable=False, default="open")
    admin_note = db.Column(db.Text, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    # Relationships
    reporter = db.relationship("User", foreign_keys=[reporter_id], backref="filed_reports")
    reported = db.relationship("User", foreign_keys=[reported_id], backref="received_reports")
    appointment = db.relationship("Appointment", foreign_keys=[appointment_id])

    def __repr__(self):
        return f"<Report #{self.id} [{self.status}]>"


class Message(db.Model):
    """A direct message between two users.

    If appointment_id is set  -> belongs to a booked session thread.
    If appointment_id is None -> peer-to-peer chat.
    """
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    # None = peer-to-peer; set = session thread
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=True, index=True)

    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    # Relationships
    sender = db.relationship("User", foreign_keys=[sender_id], backref="sent_messages")
    recipient = db.relationship("User", foreign_keys=[recipient_id], backref="received_messages")
    appointment = db.relationship("Appointment", foreign_keys=[appointment_id])

    def __repr__(self):
        return f"<Message #{self.id} from={self.sender_id} to={self.recipient_id}>"


class MentorAvailability(db.Model):
    """A recurring weekly availability window, stored as local PHT wall time."""
    __tablename__ = "mentor_availability"

    id = db.Column(db.Integer, primary_key=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    weekday = db.Column(db.Integer, nullable=False)  # Monday=0
    start_time = db.Column(db.String(5), nullable=False)  # HH:MM
    end_time = db.Column(db.String(5), nullable=False)
    mentor = db.relationship("User", backref="availability_windows")
    __table_args__ = (db.UniqueConstraint("mentor_id", "weekday", "start_time", "end_time"),)


class MentorUnavailableDate(db.Model):
    __tablename__ = "mentor_unavailable_dates"

    id = db.Column(db.Integer, primary_key=True)
    mentor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    unavailable_date = db.Column(db.Date, nullable=False)
    __table_args__ = (db.UniqueConstraint("mentor_id", "unavailable_date"),)
