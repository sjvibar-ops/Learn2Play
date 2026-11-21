import json
from sqlalchemy import event, DDL
from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
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
    leaderboard_opt_in = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

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
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending | approved | rejected | revision_required
    review_note = db.Column(db.String(500))
    reviewed_by = db.Column(db.Integer)
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

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

    application_id = db.Column(db.Integer, db.ForeignKey("role_applications.id", name="fk_credentials_application_id"), nullable=True, index=True)


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
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", name="fk_role_application_history_actor_id", ondelete="RESTRICT"))
    decision_note = db.Column(db.String(500))
    role = db.Column(db.String(20), nullable=False)          # mentor | coach
    event_type = db.Column(db.String(20), nullable=False)    # submitted | resubmitted | approved | rejected | revision_requested
    from_status = db.Column(db.String(20), nullable=False)   # not_submitted | pending | rejected | revision_required
    to_status = db.Column(db.String(20), nullable=False)     # pending | approved | rejected | revision_required

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


ROLE_APPLICATION_HISTORY_GUARDS = ("CREATE FUNCTION prevent_role_application_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Provider application history is append-only'; END; $$", 'CREATE TRIGGER role_application_history_no_mutation BEFORE UPDATE OR DELETE ON role_application_history FOR EACH ROW EXECUTE FUNCTION prevent_role_application_history_mutation()', 'CREATE TRIGGER role_application_history_no_truncate BEFORE TRUNCATE ON role_application_history FOR EACH STATEMENT EXECUTE FUNCTION prevent_role_application_history_mutation()')
for statement in ROLE_APPLICATION_HISTORY_GUARDS:
    event.listen(RoleApplicationHistory.__table__, "after_create", DDL(statement).execute_if(dialect="postgresql"))
event.listen(RoleApplicationHistory.__table__, "after_drop", DDL("DROP FUNCTION IF EXISTS prevent_role_application_history_mutation()").execute_if(dialect="postgresql"))
for operation in ("UPDATE", "DELETE"):
    event.listen(RoleApplicationHistory.__table__, "after_create", DDL(
        f"CREATE TRIGGER role_application_history_no_{operation.lower()} BEFORE {operation} ON role_application_history BEGIN SELECT RAISE(ABORT, 'Provider application history is append-only'); END"
    ).execute_if(dialect="sqlite"))


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
    ended_at = db.Column(db.DateTime)
    ended_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    ending_reason = db.Column(db.String(500))
    provider_confirmed_at = db.Column(db.DateTime)
    student_confirmed_at = db.Column(db.DateTime)
    attendance_disputed_at = db.Column(db.DateTime)
    attendance_disputed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    attendance_disputed_by = db.relationship("User", foreign_keys=[attendance_disputed_by_id])
    attendance_resolved_at = db.Column(db.DateTime)
    attendance_resolved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    attendance_resolved_by = db.relationship("User", foreign_keys=[attendance_resolved_by_id])
    attendance_resolution = db.Column(db.String(30))
    attendance_resolution_reason = db.Column(db.String(1000))
    attendance_closed_at = db.Column(db.DateTime)

    @property
    def attendance_disputed(self):
        return self.attendance_disputed_at is not None and self.attendance_resolved_at is None


    payment_required = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())

    @property
    def payment_satisfied(self):
        return not self.payment_required or (self.payment is not None and self.payment.status == "VERIFIED")

    # Financial snapshot locked at booking time
    price = db.Column(db.Numeric(10, 2), nullable=True)
    currency = db.Column(db.String(10), nullable=False, default="PHP")

    # Logistics
    location_type = db.Column(db.String(50), default="Online")            # Online | In-person
    meeting_link = db.Column(db.String(255), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    meeting_room = db.Column(db.String(80), nullable=True, unique=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    # Relationships
    provider = db.relationship("User", foreign_keys=[provider_id], backref="provider_appointments")
    student = db.relationship("User", foreign_keys=[student_id], backref="student_appointments")

    def __repr__(self):
        return f"<Appointment #{self.id} {self.topic} ({self.status})>"


class XpEvent(db.Model):
    """Immutable XP awarded for a server-confirmed appointment."""
    __tablename__ = "xp_events"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False)
    path = db.Column(db.String(20), nullable=False)  # academic | gaming
    points = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    user = db.relationship("User", backref="xp_events")
    appointment = db.relationship("Appointment")

    __table_args__ = (
        db.UniqueConstraint("appointment_id", name="uq_xp_events_appointment"),
        db.CheckConstraint("points > 0", name="ck_xp_events_positive_points"),
        db.CheckConstraint("path IN ('academic', 'gaming')", name="ck_xp_events_path"),
        db.Index("ix_xp_events_user_created", "user_id", "created_at"),
    )


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


class Payment(db.Model):
    """One immutable price snapshot per appointment; verification is manual."""
    __tablename__ = "payments"
    # The marker is written by PostgreSQL. Keep normal payment reads and
    # inserts compatible with the pre-activation schema during deployment.
    __mapper_args__ = {"eager_defaults": False}
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False, unique=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    reference = db.Column(db.String(40), nullable=False, unique=True)
    method = db.Column(db.String(30))
    receipt_path = db.Column(db.String(255))
    transaction_reference = db.Column(db.String(120))
    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)
    reconciliation_state = db.Column(db.String(30), nullable=False, default="NOT_REQUIRED", server_default="NOT_REQUIRED", index=True)
    reconciliation_version = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    reconciliation_explanation = db.Column(db.String(500))
    reconciliation_updated_at = db.Column(db.DateTime)
    reconciliation_updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    submission_revision = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    ledger_verification_xid = db.deferred(db.Column(db.String(20), server_default=db.FetchedValue()))
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    submitted_at = db.Column(db.DateTime)
    rejection_reason = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    appointment = db.relationship("Appointment", backref=db.backref("payment", uselist=False))
    student = db.relationship("User", foreign_keys=[student_id])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    __table_args__ = (
        db.CheckConstraint("reconciliation_state IN ('NOT_REQUIRED','NEEDS_REVIEW','NO_FUNDS_RECEIVED','REFUND_PENDING','REFUNDED','RETAINED')", name="ck_payment_reconciliation_state"),
        db.CheckConstraint("reconciliation_version >= 0", name="ck_payment_reconciliation_version"),
        db.CheckConstraint("amount >= 0", name="ck_payment_amount"),
        db.CheckConstraint("submission_revision >= 0", name="ck_payment_submission_revision"),
        db.CheckConstraint("status IN ('PENDING','SUBMITTED','VERIFIED','REJECTED')", name="ck_payment_status"),
        db.Index("uq_payments_verified_transfer",
                 db.func.lower(db.func.trim(method)),
                 db.func.lower(db.func.trim(transaction_reference)), unique=True,
                 postgresql_where=db.text("status = 'VERIFIED'"),
                 sqlite_where=db.text("status = 'VERIFIED'")),
        db.Index("ix_payments_transfer_claim",
                 db.func.lower(db.func.trim(method)),
                 db.func.lower(db.func.trim(transaction_reference))),
    )


class PaymentEvent(db.Model):
    """Immutable snapshots; Payment alone controls payment eligibility."""
    __tablename__ = "payment_events"
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, index=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id", ondelete="RESTRICT"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))
    event_type = db.Column(db.String(30), nullable=False)
    previous_status = db.Column(db.String(20), nullable=False)
    new_status = db.Column(db.String(20), nullable=False)
    revision = db.Column(db.Integer, nullable=False)
    receipt_path = db.Column(db.String(255))
    method = db.Column(db.String(30))
    transaction_reference = db.Column(db.String(120))
    rejection_reason = db.Column(db.String(500))
    submitted_at = db.Column(db.DateTime)
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    reconciliation_previous_state = db.Column(db.String(30))
    reconciliation_state = db.Column(db.String(30))
    reconciliation_version = db.Column(db.Integer)
    reconciliation_explanation = db.Column(db.String(500))
    booking_status = db.Column(db.String(20))
    booking_ended_at = db.Column(db.DateTime)
    booking_ending_reason = db.Column(db.String(500))
    decision_reason = db.Column(db.String(500))
    confirmation_notes = db.Column(db.String(1000))
    evidence_reference = db.Column(db.String(200))
    authority_name = db.Column(db.String(200))
    authority_capacity = db.Column(db.String(60))
    funds_received_confirmed = db.Column(db.Boolean)
    independent_check_confirmed = db.Column(db.Boolean)
    authorized_refund_confirmed = db.Column(db.Boolean)
    account_holder_refund_confirmed = db.Column(db.Boolean)
    authorized_retention_confirmed = db.Column(db.Boolean)
    actor = db.relationship("User", foreign_keys=[actor_id])
    payment = db.relationship("Payment", backref=db.backref("events", order_by="PaymentEvent.id", passive_deletes="all"))
    reviewer = db.relationship("User", foreign_keys=[reviewer_id])
    __table_args__ = (
        db.UniqueConstraint("payment_id", "reconciliation_version", name="uq_payment_event_reconciliation_version"),
        db.CheckConstraint("reconciliation_version IS NULL OR reconciliation_version > 0", name="ck_payment_event_reconciliation_version"),
        db.CheckConstraint("reconciliation_state IS NULL OR reconciliation_state IN ('NOT_REQUIRED','NEEDS_REVIEW','NO_FUNDS_RECEIVED','REFUND_PENDING','REFUNDED','RETAINED')", name="ck_payment_event_reconciliation_state"),
        db.UniqueConstraint("payment_id", "revision", "event_type", name="uq_payment_event_revision_type"),
        db.CheckConstraint("revision >= 0", name="ck_payment_event_revision"),
        db.CheckConstraint("event_type IN ('SUBMITTED','RESUBMITTED','REJECTED','VERIFIED','LEGACY_SNAPSHOT','BOOKING_ENDED','NEEDS_REVIEW','NO_FUNDS_RECEIVED','REFUND_PENDING','REFUNDED','RETAINED')", name="ck_payment_event_type"),
        db.CheckConstraint("previous_status IN ('PENDING','SUBMITTED','REJECTED','VERIFIED') AND new_status IN ('PENDING','SUBMITTED','REJECTED','VERIFIED')", name="ck_payment_event_status"),
    )


@event.listens_for(PaymentEvent, "before_update")
@event.listens_for(PaymentEvent, "before_delete")
def immutable_payment_event(mapper, connection, target):
    raise ValueError("Payment audit events are append-only.")


PAYMENT_EVENT_POSTGRES_GUARDS = (
    """CREATE FUNCTION prevent_payment_event_mutation() RETURNS trigger LANGUAGE plpgsql
       AS $$ BEGIN RAISE EXCEPTION 'Payment audit events are append-only'; END; $$""",
    """CREATE TRIGGER payment_events_no_mutation BEFORE UPDATE OR DELETE ON payment_events
       FOR EACH ROW EXECUTE FUNCTION prevent_payment_event_mutation()""",
    """CREATE TRIGGER payment_events_no_truncate BEFORE TRUNCATE ON payment_events
       FOR EACH STATEMENT EXECUTE FUNCTION prevent_payment_event_mutation()""",
)
# Fresh schemas use these guards too. The migration freezes the same SQL.
for statement in PAYMENT_EVENT_POSTGRES_GUARDS:
    event.listen(PaymentEvent.__table__, "after_create", DDL(statement).execute_if(dialect="postgresql"))
event.listen(PaymentEvent.__table__, "after_drop", DDL("DROP FUNCTION IF EXISTS prevent_payment_event_mutation()").execute_if(dialect="postgresql"))
for operation in ("UPDATE", "DELETE"):
    event.listen(PaymentEvent.__table__, "after_create", DDL(
        f"CREATE TRIGGER payment_events_no_{operation.lower()} BEFORE {operation} ON payment_events BEGIN SELECT RAISE(ABORT, 'Payment audit events are append-only'); END"
    ).execute_if(dialect="sqlite"))


class AttendanceEvent(db.Model):
    """Append-only application audit of participant confirmations and disputes."""
    __tablename__ = "attendance_events"
    id = db.Column(db.Integer, primary_key=True)
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(40), nullable=False)
    note = db.Column(db.String(1000), nullable=False, default="")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    appointment = db.relationship("Appointment", backref="attendance_events")
    actor = db.relationship("User", foreign_keys=[actor_id])


PLATFORM_LEDGER_ACCOUNTS = ("payment_clearing", "platform_commission")
PROVIDER_LEDGER_ACCOUNTS = (
    "provider_pending", "provider_available", "provider_held", "provider_settlement",
)
LEDGER_TRANSACTION_TYPES = (
    "allocation", "release", "hold", "refund", "adjustment", "reversal", "settlement",
)


class AdminAuditEvent(db.Model):
    """Structured administrative history. Callers own authorization and commit."""

    __tablename__ = "admin_audit_events"

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    action = db.Column(db.String(40), nullable=False)
    target_type = db.Column(db.String(40), nullable=False)
    target_id = db.Column(db.BigInteger, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    reason = db.Column(db.String(40))
    before = db.Column(db.JSON(none_as_null=True))
    after = db.Column(db.JSON(none_as_null=True))
    correlation_id = db.Column(db.Uuid)
    related_event_id = db.Column(db.BigInteger, db.ForeignKey("admin_audit_events.id", ondelete="RESTRICT"))
    verification_event_id = db.Column(
        db.Integer, db.ForeignKey("payment_events.id", ondelete="RESTRICT"),
    )

    __table_args__ = (
        db.CheckConstraint("length(trim(action)) > 0 AND length(trim(target_type)) > 0 AND target_id > 0",
                           name="ck_admin_audit_target"),
        db.Index("ix_admin_audit_target", "target_type", "target_id", "id"),
        db.Index("ix_admin_audit_actor", "actor_id", "id"),
        db.Index("ix_admin_audit_correlation", "correlation_id"),
        db.UniqueConstraint("verification_event_id", name="uq_admin_audit_verification_event"),
    )


class LedgerAccount(db.Model):
    __tablename__ = "ledger_accounts"

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    kind = db.Column(db.String(30), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    provider_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))

    __table_args__ = (
        db.CheckConstraint("currency = 'PHP'", name="ck_ledger_account_currency"),
        db.CheckConstraint(
            f"(kind IN {PLATFORM_LEDGER_ACCOUNTS!r} AND provider_id IS NULL) OR "
            f"(kind IN {PROVIDER_LEDGER_ACCOUNTS!r} AND provider_id IS NOT NULL)",
            name="ck_ledger_account_owner",
        ),
        db.UniqueConstraint("id", "currency", name="uq_ledger_account_currency"),
        db.UniqueConstraint("provider_id", "kind", "currency", name="uq_ledger_provider_account"),
        db.Index("uq_ledger_platform_account", "kind", "currency", unique=True,
                 postgresql_where=db.text("provider_id IS NULL"), sqlite_where=db.text("provider_id IS NULL")),
    )


class LedgerTransaction(db.Model):
    __tablename__ = "ledger_transactions"

    id = db.Column(db.BigInteger().with_variant(db.Integer, "sqlite"), primary_key=True)
    kind = db.Column(db.String(30), nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id", ondelete="RESTRICT"))
    appointment_id = db.Column(db.Integer, db.ForeignKey("appointments.id", ondelete="RESTRICT"))
    provider_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))
    payment_event_id = db.Column(db.Integer, db.ForeignKey("payment_events.id", ondelete="RESTRICT"))
    admin_audit_id = db.Column(db.BigInteger, db.ForeignKey("admin_audit_events.id", ondelete="RESTRICT"))
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"))
    system_actor = db.Column(db.String(40))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    reason = db.Column(db.String(500), nullable=False)
    original_transaction_id = db.Column(db.BigInteger, db.ForeignKey("ledger_transactions.id", ondelete="RESTRICT"))
    # PostgreSQL only permits this construction state inside the posting transaction.
    sealed = db.Column(db.Boolean, nullable=False, server_default=db.false())

    __table_args__ = (
        db.CheckConstraint("currency = 'PHP'", name="ck_ledger_transaction_currency"),
        db.CheckConstraint(f"kind IN {LEDGER_TRANSACTION_TYPES!r}", name="ck_ledger_transaction_type"),
        db.CheckConstraint("(payment_event_id IS NULL) <> (admin_audit_id IS NULL)", name="ck_ledger_source"),
        db.CheckConstraint("(actor_id IS NOT NULL AND system_actor IS NULL) OR "
                           "(actor_id IS NULL AND system_actor IS NOT NULL AND length(trim(system_actor)) > 0)",
                           name="ck_ledger_actor"),
        db.CheckConstraint("length(trim(reason)) > 0", name="ck_ledger_reason"),
        db.CheckConstraint("kind <> 'allocation' OR "
                           "(payment_id IS NOT NULL AND appointment_id IS NOT NULL AND provider_id IS NOT NULL "
                           "AND payment_event_id IS NOT NULL AND original_transaction_id IS NULL)",
                           name="ck_ledger_allocation_source"),
        db.CheckConstraint("kind NOT IN ('refund', 'adjustment', 'reversal') OR "
                           "(admin_audit_id IS NOT NULL AND original_transaction_id IS NOT NULL)",
                           name="ck_ledger_compensation_source"),
        db.UniqueConstraint("id", "currency", name="uq_ledger_transaction_currency"),
        db.UniqueConstraint("kind", "payment_event_id", name="uq_ledger_payment_event_kind"),
        db.UniqueConstraint("admin_audit_id", name="uq_ledger_admin_decision"),
        db.Index("uq_ledger_initial_allocation", "payment_id", unique=True,
                 postgresql_where=db.text("kind = 'allocation'"), sqlite_where=db.text("kind = 'allocation'")),
        db.Index("ix_ledger_payment", "payment_id", "id"),
        db.Index("ix_ledger_provider", "provider_id", "id"),
        db.Index("ix_ledger_original", "original_transaction_id"),
    )


class LedgerLine(db.Model):
    """Signed centavos. Positive is a debit, negative is a credit."""

    __tablename__ = "ledger_lines"

    transaction_id = db.Column(db.BigInteger, primary_key=True)
    ordinal = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.BigInteger, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    centavos = db.Column(db.BigInteger, nullable=False)

    __table_args__ = (
        db.ForeignKeyConstraint(["transaction_id", "currency"], ["ledger_transactions.id", "ledger_transactions.currency"],
                                ondelete="RESTRICT", name="fk_ledger_line_transaction_currency"),
        db.ForeignKeyConstraint(["account_id", "currency"], ["ledger_accounts.id", "ledger_accounts.currency"],
                                ondelete="RESTRICT", name="fk_ledger_line_account_currency"),
        db.CheckConstraint("ordinal > 0 AND centavos <> 0", name="ck_ledger_line_values"),
        db.Index("ix_ledger_line_account", "account_id", "transaction_id"),
    )


@event.listens_for(AdminAuditEvent, "before_update")
@event.listens_for(AdminAuditEvent, "before_delete")
def immutable_admin_audit_event(mapper, connection, target):
    raise ValueError("Administrator audit events are append-only.")


class DiscordAccount(db.Model):
    __tablename__ = "discord_accounts"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    discord_id = db.Column(db.String(32), nullable=False, unique=True)
    linked_at = db.Column(db.DateTime, nullable=False, default=utcnow)
