import os
import re
import secrets
import urllib.parse
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import func, or_
from decimal import Decimal, InvalidOperation
from functools import wraps
from io import BytesIO
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import (
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf.csrf import generate_csrf
import jwt
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from supabase import create_client
from werkzeug.utils import secure_filename

if __package__:
    from .extensions import db
    from .reconciliation import ReconciliationError, lock_booking_end, end_booking, text_field
    from .scheduling import lock_schedules, overlaps, participant_bookings, reservation_errors
    from .models import Appointment, Credential, MentorAvailability, MentorUnavailableDate, Message, Report, RoleApplication, RoleApplicationHistory, User, XpEvent, AttendanceEvent, utcnow
else:
    from extensions import db
    from reconciliation import ReconciliationError, lock_booking_end, end_booking, text_field
    from scheduling import lock_schedules, overlaps, participant_bookings, reservation_errors
    from models import Appointment, Credential, MentorAvailability, MentorUnavailableDate, Message, Report, RoleApplication, RoleApplicationHistory, User, XpEvent, AttendanceEvent, utcnow

main = Blueprint("main", __name__)

def _balance_streak_from_events(events, current_week_start, tz):
    """Return the consecutive balanced-week streak, preserving it during this week."""
    paths_by_week = {}
    for event in events:
        event_day = event.created_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
        monday = event_day - timedelta(days=event_day.weekday())
        paths_by_week.setdefault(monday, set()).add(event.path)
    balanced_weeks = {week for week, paths in paths_by_week.items() if {"academic", "gaming"} <= paths}
    current_week_complete = current_week_start in balanced_weeks
    cursor = current_week_start if current_week_complete else current_week_start - timedelta(days=7)
    streak = 0
    while cursor in balanced_weeks:
        streak += 1
        cursor -= timedelta(days=7)
    return streak, current_week_complete



# ---------- Application Configuration & Central Currency ----------
ALLOWED_ROLES = {"student", "mentor", "coach"}   # "mentor" is displayed to users as "Teacher"
VERIFIED_ROLES = {"mentor", "coach"}            # activation requires admin approval

CURRENCY_CODE = "PHP"
CURRENCY_SYMBOL = "₱"

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
LINK_RE = re.compile(r"^https?://\S+\.\S+$", re.I)

MAX_FILES = 5
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_LINKS = 5
EXPERIENCE_MIN_LENGTH = 50
EXPERIENCE_MAX_LENGTH = 1500
MAX_LINK_LENGTH = 300
APPLICATION_ROLE_LABELS = {"mentor": "Academic Mentor", "coach": "Gaming Coach"}
RESUBMITTABLE_STATUSES = {"rejected", "revision_required"}



class JaasConfigurationError(Exception):
    """Raised when server-side JaaS credentials are missing or unusable."""


def create_jaas_participant_token(appointment, participant):
    """Create a short-lived token scoped to one participant and one booking room."""
    app_id = current_app.config.get("JAAS_APP_ID", "").strip()
    configured_key_id = current_app.config.get("JAAS_API_KEY_ID", "").strip()
    private_key = current_app.config.get("JAAS_PRIVATE_KEY", "")
    if not app_id or not configured_key_id or not private_key:
        raise JaasConfigurationError

    # JaaS kid is AppID/key-id. Accept either its complete form or the key-id
    # suffix, but reject a value belonging to a different AppID.
    key_prefix = f"{app_id}/"
    if configured_key_id.startswith(key_prefix):
        kid = configured_key_id
    elif "/" not in configured_key_id:
        kid = key_prefix + configured_key_id
    else:
        raise JaasConfigurationError
    if not appointment.meeting_room or not participant.display_name:
        raise JaasConfigurationError

    now = datetime.now(timezone.utc)
    is_provider = participant.id == appointment.provider_id
    claims = {
        "aud": "jitsi",
        "iss": "chat",
        "sub": app_id,
        "room": appointment.meeting_room,
        "nbf": int(now.timestamp()) - 10,
        "exp": int(now.timestamp()) + 600,
        "context": {
            "user": {
                "id": str(participant.id),
                "name": participant.display_name,
                "moderator": "true" if is_provider else "false",
            },
            "features": {
                "recording": "false",
                "livestreaming": "false",
                "transcription": "false",
                "outbound-call": "false",
            },
            "room": {"regex": False},
        },
    }
    try:
        return jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": kid, "typ": "JWT"},
        )
    except Exception:
        # Avoid propagating crypto-library details, which can include key data.
        raise JaasConfigurationError from None

FILE_SIGNATURES = {
    "pdf": (b"%PDF-",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
}

CREDENTIALS_BUCKET = "credentials"
CREDENTIAL_MIME_TYPES = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}

AVATARS_BUCKET = "avatars"
MAX_AVATAR_BYTES = 2 * 1024 * 1024
AVATAR_ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
AVATAR_MIME_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


# ---------- Rate & Timezone Helpers ----------
def parse_rate(raw_value):
    """
    Parses rate input:
    - None or "" -> None (Rate not set)
    - Valid number -> Decimal rounded to 2 places
    Raises ValueError on negative, non-finite, or excessively large amounts (> 50,000).
    """
    if raw_value is None or str(raw_value).strip() == "":
        return None
    try:
        val = Decimal(str(raw_value).strip())
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Rate must be a valid number.")

    if not val.is_finite():
        raise ValueError("Rate must be a finite number.")
    if val < 0:
        raise ValueError("Rate cannot be negative.")
    if val > Decimal("50000"):
        raise ValueError("Rate cannot exceed ₱50,000 per hour.")

    return val.quantize(Decimal("0.01"))


def format_dt_pht(dt):
    """Formats UTC datetime into Philippine Standard Time (PHT, UTC+8)."""
    if not dt:
        return ""
    pht = dt + timedelta(hours=8)
    return pht.strftime("%b %d, %Y at %I:%M %p")


def format_time_range_pht(dt, duration_minutes):
    """Formats start time to end time range in PHT."""
    if not dt:
        return ""
    start_pht = dt + timedelta(hours=8)
    end_pht = start_pht + timedelta(minutes=duration_minutes or 60)
    return f"{start_pht.strftime('%I:%M %p')} – {end_pht.strftime('%I:%M %p')}"


# ---------- Decorators ----------
def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            return error("Administrator access required.", 403)
        response = current_app.make_response(view(*args, **kwargs))
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    return wrapped


def provider_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not any(r in current_user.roles for r in ("mentor", "coach")):
            if request.is_json or request.path.startswith("/api/"):
                return error("Teacher or Coach role required.", 403)
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def provider_approved_for_role(user, role):
    """True only for an active user whose approved roles include this exact role.

    Mentor and Coach approvals are independent: a mentor approval never covers a
    coach appointment, and an approval that was removed no longer counts.
    """
    return bool(user and user.is_active and role in VERIFIED_ROLES and role in user.roles)

# ---------- User Serializer ----------
def user_data(user):
    """Consistent user response for frontend consumption."""
    # Compute real dynamic statistics from database appointments
    sessions_count = 0
    total_minutes = 0
    mentors_met = set()

    try:
        completed_provided = Appointment.query.filter_by(provider_id=user.id, status="completed").all()
        completed_attended = Appointment.query.filter_by(student_id=user.id, status="completed").all()
        sessions_count = len(completed_provided) + len(completed_attended)
        total_minutes = sum(a.duration_minutes for a in completed_provided + completed_attended)
        mentors_met = {a.provider_id for a in completed_attended}
    except Exception:
        pass

    return {
        "id": user.id,
        "name": user.display_name,
        "email": user.email,
        "roles": user.roles,
        "is_admin": user.is_admin,
        "bio": user.bio or "",
        "profile_picture": user.profile_picture,
        "topics": getattr(user, "topics", []),
        "teaching_rate": float(user.teaching_rate) if user.teaching_rate is not None else None,
        "coaching_rate": float(user.coaching_rate) if user.coaching_rate is not None else None,
        "hourly_rate": float(user.teaching_rate if user.teaching_rate is not None else (user.coaching_rate if user.coaching_rate is not None else (user.hourly_rate or 300))),
        "currency": CURRENCY_CODE,
        "currency_symbol": CURRENCY_SYMBOL,
        "stats": {
            "sessions_count": sessions_count,
            "mentors_count": len(mentors_met),
            "hours_count": round(total_minutes / 60, 1),
            "avg_rating": None,  # No review model exists; never fabricate ratings.
        },
    }


def error(message, status=400, field=None):
    return jsonify({"ok": False, "message": message, "field": field}), status


def credentials_bucket():
    supabase_url = os.environ.get("SUPABASE_URL")
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_role_key:
        raise RuntimeError("Supabase Storage credentials are not configured.")

    client = create_client(supabase_url, service_role_key)
    return client.storage.from_(CREDENTIALS_BUCKET)


def cleanup_credential_objects(object_paths):
    if not object_paths:
        return
    try:
        credentials_bucket().remove(object_paths)
    except Exception:
        current_app.logger.warning("Credential cleanup deferred; Storage details suppressed.")


def avatars_bucket():
    supabase_url = os.environ.get("SUPABASE_URL")
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_role_key:
        raise RuntimeError("Supabase Storage credentials are not configured.")

    client = create_client(supabase_url, service_role_key)
    return client.storage.from_(AVATARS_BUCKET)


def cleanup_avatar_object(object_path):
    if not object_path:
        return
    try:
        avatars_bucket().remove([object_path])
    except Exception:
        current_app.logger.exception("Failed to remove avatar object from Supabase Storage: %s", object_path)


def public_avatar_object_path(url):
    """Return the object key for an HTTPS public avatar URL on this project."""
    if not isinstance(url, str) or re.search(r"[\s\x00-\x1f\x7f?#]", url):
        return None

    try:
        public = urllib.parse.urlsplit(url)
        project = urllib.parse.urlsplit(os.environ.get("SUPABASE_URL", ""))
        public_port = 443 if public.port is None else public.port
        project_port = 443 if project.port is None else project.port
    except ValueError:
        return None

    if (
        public.scheme != "https"
        or not public.hostname
        or public.hostname != project.hostname
        or public_port != project_port
        or not re.fullmatch(r"(?:\[[^\[\]]+\]|[^:\[\]\\]+)(?::[0-9]+)?", public.netloc)
        or project.netloc.endswith(":")
        or public.username is not None
        or public.password is not None
    ):
        return None

    prefix = f"{project.path.rstrip('/')}/storage/v1/object/public/{AVATARS_BUCKET}/"
    if not public.path.startswith(prefix):
        return None
    return public.path[len(prefix):]


def is_uploaded_avatar_url(url, object_path):
    return public_avatar_object_path(url) == object_path


def extract_supabase_avatar_path(url, user_id):
    """Extract a managed avatar key only from the specified user's folder."""
    object_path = public_avatar_object_path(url)
    user_prefix = f"user_{user_id}/"
    if object_path is None or not object_path.startswith(user_prefix):
        return None

    filename = object_path[len(user_prefix):]
    if filename in ("", ".") or ".." in filename or any(char in filename for char in "/\\%"):
        return None
    return object_path


def validate_avatar_file(file):
    """
    Validates uploaded avatar file:
    - Must be provided with a valid filename
    - Extension must be in {jpg, jpeg, png, webp}
    - Non-empty and at most MAX_AVATAR_BYTES (2 MB)
    - Magic bytes match the declared image extension
    Returns: (ext, size, error_message)
    """
    if not file or not getattr(file, "filename", None):
        return None, None, "Please select an image."

    name = secure_filename(file.filename or "")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in AVATAR_ALLOWED_EXTENSIONS:
        return None, None, "Only JPG, PNG, and WebP images are allowed."

    try:
        stream = file.stream
        header = stream.read(16)
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
    except Exception:
        return None, None, "Failed to read image file."

    if size == 0:
        return None, None, "The selected image file is empty."

    if size > MAX_AVATAR_BYTES:
        return None, None, "Image file must not exceed 2 MB."

    is_valid = False
    if ext in ("jpg", "jpeg"):
        is_valid = header.startswith(b"\xff\xd8\xff")
    elif ext == "png":
        is_valid = header.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext == "webp":
        is_valid = len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"

    if not is_valid:
        return None, None, f'File does not appear to be a valid {ext.upper()} image.'

    return ext, size, None


def cleanup_uncommitted_application_objects(paths, commit_started):
    if not paths:
        return
    if not commit_started:
        cleanup_credential_objects(paths)
        return
    try:
        referenced = {row[0] for row in db.session.query(Credential.stored_name).filter(Credential.stored_name.in_(paths)).all()}
        db.session.rollback()
    except Exception:
        db.session.rollback()
        current_app.logger.warning("Application evidence cleanup deferred; database outcome requires review.")
        return
    cleanup_credential_objects([path for path in paths if path not in referenced])


def check_upload(file):
    name = secure_filename(file.filename or "")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    if ext not in FILE_SIGNATURES:
        return None, f'"{file.filename}" is not supported. Use PDF, JPG or PNG.'

    head = file.stream.read(8)
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)

    if not any(head.startswith(sig) for sig in FILE_SIGNATURES[ext]):
        return None, f'"{file.filename}" does not look like a real {ext.upper()} file.'
    if size == 0:
        return None, f'"{file.filename}" is empty.'
    if size > MAX_FILE_BYTES:
        return None, f'"{file.filename}" is over 5 MB.'

    if ext in ("jpg", "jpeg", "png"):
        if __package__:
            from .payments import valid_image
        else:
            from payments import valid_image
        content = file.stream.read(MAX_FILE_BYTES + 1)
        file.stream.seek(0)
        if not valid_image(content, ext == "png"):
            return None, "The evidence image is invalid or exceeds supported dimensions."
    return ext, None


# ---------- Health Check & Static Pages ----------
@main.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


@main.route("/")
def home():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for("main.admin_dashboard"))

        return redirect(url_for("main.dashboard"))

    return render_template("landing.html")


@main.route("/auth")
def auth_page():
    return render_template("auth.html")


@main.route("/register")
def register_page():
    return render_template("register.html")


@main.route("/api/csrf-token", methods=["GET"])
def csrf_token():
    return jsonify({"csrf_token": generate_csrf()})


# ---------- Register & Login API ----------
@main.route("/api/auth/register", methods=["POST"])
def register():
    # Registration only creates a standard Student account. Mentor/Coach
    # applications (and any supporting evidence) are submitted after sign-up,
    # so this view never touches Storage, Credential or RoleApplication.
    form = request.form

    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    confirm = form.get("confirm_password") or ""

    if not 2 <= len(name) <= 80:
        return error("Enter your full name (2 to 80 characters).", field="name")
    if not EMAIL_RE.match(email):
        return error("Enter a valid email address.", field="email")
    if len(email) > 120:
        return error("Email must not exceed 120 characters.", field="email")
    if (
        len(password) < 8
        or not re.search(r"[a-z]", password)
        or not re.search(r"[A-Z]", password)
        or not re.search(r"\d", password)
    ):
        return error(
            "Use at least 8 characters, with upper and lower case letters and a number.",
            field="password",
        )
    if password != confirm:
        return error("Passwords don't match.", field="confirmPassword")
    if form.get("acknowledged") != "true":
        return error("Confirm that you understand the review before you continue.", field="ack")
    if User.query.filter_by(email=email).first():
        return error("That email is already registered. Try logging in instead.", 409, field="email")

    try:
        user = User(display_name=name, email=email)
        user.set_password(password)
        user.roles = ["student"]

        db.session.add(user)
        db.session.commit()

    except IntegrityError:
        db.session.rollback()
        return error("That email is already registered. Try logging in instead.", 409, field="email")

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Registration failed")
        return error("We couldn't create your account. Please try again.", 500)

    login_user(user)
    return jsonify({
        "ok": True,
        "pending": False,
        "message": "Your student account is active. Welcome to Learn2Play! You can apply to become a Mentor or Coach from your account after you sign in.",
        "redirect": url_for("main.dashboard"),
        "user": user_data(user),
    }), 201


@main.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = data.get("email", "")
    password = data.get("password", "")
    remember = data.get("remember", False)

    if not isinstance(email, str) or not isinstance(password, str):
        return error("Please enter your email and password.")

    email = email.strip().lower()
    user = User.query.filter_by(email=email).first()

    if not user or not user.check_password(password):
        return error("Incorrect email or password.", 401)
    if not user.is_active:
        return error("This account is inactive.", 403)

    # Active accounts with roles == [] (pending or rejected provider-only
    # registrations) may sign in with limited permissions. Roles are never
    # assigned here; they only change through admin approval.
    login_user(user, remember=remember is True)

# Redirect admins and regular users to their respective dashboards
    redirect_url = (
        url_for("main.admin_dashboard")
        if user.is_admin
        else url_for("main.dashboard")
    )

    return jsonify({
        "ok": True,
        "message": "Logged in successfully.",
        "redirect": redirect_url,
        "user": user_data(user),
    })

@main.route("/api/auth/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return jsonify({"ok": True, "message": "Logged out successfully."})


@main.route("/api/me", methods=["GET"])
def get_current_user():
    if not current_user.is_authenticated:
        return error("You are not logged in.", 401)
    return jsonify({"ok": True, "user": user_data(current_user)})


# ---------- Admin Endpoints ----------
@main.route("/api/admin/applications", methods=["GET"])
@admin_required
def list_applications():
    status = request.args.get("status", "pending")
    rows = (
        RoleApplication.query.filter_by(status=status)
        .order_by(func.coalesce(RoleApplication.submitted_at, RoleApplication.created_at).asc())
        .all()
    )

    items = []
    for app_row in rows:
        user = db.session.get(User, app_row.user_id)
        # A file belongs to the application it was uploaded for. Legacy files
        # (application_id NULL) have an unknown application, so they stay
        # visible on every application of that user, exactly as before.
        creds = (
            Credential.query.filter(
                Credential.user_id == app_row.user_id,
                or_(
                    Credential.application_id == app_row.id,
                    Credential.application_id.is_(None),
                ),
            )
            .order_by(Credential.id.asc())
            .all()
        )
        items.append({
            "id": app_row.id,
            "role": app_row.role,
            "status": app_row.status,
            "bio": app_row.bio,
            "links": [l for l in (app_row.links or "").split("\n") if l],
            "submitted_at": (app_row.submitted_at or app_row.created_at).isoformat(),
            "user": {"id": user.id, "name": user.display_name, "email": user.email},
            "credentials": [
                {
                    "id": c.id,
                    "name": c.original_name,
                    "url": url_for("main.download_credential", credential_id=c.id),
                }
                for c in creds
            ],
        })

    return jsonify({"ok": True, "applications": items})


# ---------- Admin review of provider applications (Batch 2.1C) ----------
# action in the URL -> (status after the review, history event_type, message)
REVIEW_ACTIONS = {
    "approve": ("approved", "approved", "Application approved."),
    "reject": ("rejected", "rejected", "Application rejected."),
    "request-revision": ("revision_required", "revision_requested", "Revision requested."),
}
# PROPOSED defaults (owner to confirm): what counts as a "meaningful" note.
REVISION_NOTE_MIN_LENGTH = 10
REVIEW_NOTE_MAX_LENGTH = 500          # RoleApplication.review_note is String(500)


def parse_revision_note(payload):
    """Return (note, None) or (None, problem). "reason" is accepted as an alias
    of "note" so the existing reject text field can be reused by the frontend."""
    raw = payload.get("note") if "note" in payload else payload.get("reason")
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        return None, "The revision note must be text."
    note = raw.strip()
    if "\x00" in note:
        return None, "The note contains unsupported characters."
    if not note:
        return None, "Please explain what the applicant needs to change."
    if len(note) < REVISION_NOTE_MIN_LENGTH:
        return None, f"The revision note must be at least {REVISION_NOTE_MIN_LENGTH} characters."
    if len(note) > REVIEW_NOTE_MAX_LENGTH:
        return None, f"The revision note must be {REVIEW_NOTE_MAX_LENGTH} characters or fewer."
    return note, None


@main.route("/api/admin/applications/<int:application_id>/<action>", methods=["POST"])
@admin_required
def review_application(application_id, action):
    """pending -> approved | rejected | revision_required, nothing else.

    The status change, the role grant (approve only) and one append-only
    RoleApplicationHistory row are committed together or not at all.
    """
    if action not in REVIEW_ACTIONS:
        return error("Unknown action.", 404)
    new_status, event_type, success_message = REVIEW_ACTIONS[action]

    app_row = db.session.get(RoleApplication, application_id)
    if not app_row:
        return error("Application not found.", 404)
    if app_row.status != "pending":
        return error("This application was already reviewed.", 409)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    note = None
    if action == "reject":
        # Existing behaviour: optional, trimmed and truncated.
        raw_reason = payload.get("reason", "")
        if not isinstance(raw_reason, str) or "\x00" in raw_reason:
            return error("The rejection reason must be valid text.", 400, field="reason")
        note = raw_reason.strip()[:REVIEW_NOTE_MAX_LENGTH] or None
    elif action == "request-revision":
        note, problem = parse_revision_note(payload)
        if problem:
            return error(problem, 400, field="note")

    admin_id = current_user.id
    try:
        applicant = User.query.filter_by(id=app_row.user_id).with_for_update().populate_existing().first()
        if applicant is None:
            db.session.rollback()
            return error("Applicant account not found.", 404)
        # Re-read under a row lock: another admin may have reviewed it meanwhile.
        row = (
            RoleApplication.query.filter_by(id=application_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
        if row is None:
            db.session.rollback()
            return error("Application not found.", 404)
        if row.status != "pending":
            db.session.rollback()
            return error("This application was already reviewed.", 409)

        if action == "approve":
            if row.role not in VERIFIED_ROLES:
                db.session.rollback()
                return error("This application has an unsupported role.", 409)
        now = utcnow()
        result = {"id": row.id, "user_id": row.user_id, "role": row.role}
        # Snapshot of the state this decision replaces, taken before any change.
        history = RoleApplicationHistory(
            actor_id=admin_id,
            decision_note=note,
            application_id=row.id,
            user_id=row.user_id,
            role=row.role,
            event_type=event_type,
            from_status="pending",
            to_status=new_status,
            prior_bio=row.bio,
            prior_links=row.links,
            prior_review_note=row.review_note,
            prior_reviewed_by=row.reviewed_by,
            prior_reviewed_at=row.reviewed_at,
            prior_submitted_at=row.submitted_at or row.created_at,
            created_at=now,
        )

        row.status = new_status
        if action != "approve":
            row.review_note = note
        row.reviewed_by = admin_id
        row.reviewed_at = now
        if action == "approve":
            applicant.roles = sorted(set(applicant.roles) | {row.role})
        final_note = row.review_note
        db.session.add(history)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.warning("Saving the application review failed; internal details suppressed.")
        return error("We couldn't save the review. Please try again.", 500)

    result.update({
        "status": new_status,
        "review_note": final_note,
        "reviewed_by": admin_id,
        "reviewed_at": now.isoformat(),
    })
    return jsonify({"ok": True, "message": success_message, "application": result})


@main.route("/admin/credentials/<int:credential_id>")
@admin_required
def download_credential(credential_id):
    cred = db.session.get(Credential, credential_id)
    if not cred:
        return error("File not found.", 404)
    try:
        file_bytes = credentials_bucket().download(cred.stored_name)
        extension = (
            cred.original_name.rsplit(".", 1)[-1].lower()
            if "." in cred.original_name
            else ""
        )
        mimetype = CREDENTIAL_MIME_TYPES.get(extension, "application/octet-stream")
        return send_file(
            BytesIO(file_bytes),
            mimetype=mimetype,
            as_attachment=True,
            download_name=cred.original_name,
        )
    except Exception:
        current_app.logger.exception("Credential download from Storage failed")
        return error("Unable to retrieve the credential file.", 500)


# ---------- Provider Applications (Batch 2.1B) ----------
def application_status_for(user, role, row):
    """not_submitted | pending | approved | rejected | revision_required."""
    if row is not None:
        return row.status
    return "approved" if role in user.roles else "not_submitted"


def application_conflict_message(user, role, row):
    """Why this user cannot submit for this role right now, or None if allowed."""
    label = APPLICATION_ROLE_LABELS[role]
    if role in user.roles or (row is not None and row.status == "approved"):
        return f"Your {label} application is already approved."
    if row is None or row.status in RESUBMITTABLE_STATUSES:
        return None
    if row.status == "pending":
        return f"Your {label} application is already pending review."
    return f"Your {label} application can't be changed right now."


def serialize_application_credential(cred):
    # Safe display metadata only: never the storage path or a download URL.
    return {
        "id": cred.id,
        "name": cred.original_name,
        "uploaded_at": cred.created_at.isoformat() if cred.created_at else None,
    }


def serialize_application(user, role, row):
    status = application_status_for(user, role, row)
    credentials = []
    if row is not None:
        credentials = (
            Credential.query.filter_by(user_id=user.id, application_id=row.id)
            .order_by(Credential.id.asc())
            .all()
        )
    submitted_at = (row.submitted_at or row.created_at) if row is not None else None
    return {
        "role": role,
        "status": status,
        "experience_description": row.bio if row is not None else "",
        "links": [l for l in (row.links or "").split("\n") if l] if row is not None else [],
        "review_note": (
            row.review_note
            if row is not None and status in RESUBMITTABLE_STATUSES
            else None
        ),
        "submitted_at": submitted_at.isoformat() if submitted_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row is not None and row.reviewed_at else None,
        "credentials": [serialize_application_credential(c) for c in credentials],
        "can_submit": application_conflict_message(user, role, row) is None,
    }


def parse_application_request():
    """Read and validate the submitted fields.

    Returns (data, problem). problem is (message, field) or None; data holds
    the cleaned description and links plus the uploaded file objects.
    """
    description_problem = (
        f"Describe your experience in {EXPERIENCE_MIN_LENGTH} to {EXPERIENCE_MAX_LENGTH} characters.",
        "experience_description",
    )
    links_problem = (
        f"Links must start with http:// or https:// (max {MAX_LINKS}).",
        "links",
    )

    if request.is_json:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None, ("The request body must be a JSON object.", None)
        raw_description = body.get("experience_description")
        raw_links = body.get("links", [])
        if isinstance(raw_links, str):
            raw_links = [raw_links]
        if not isinstance(raw_links, list) or any(not isinstance(l, str) for l in raw_links):
            return None, links_problem
        uploads = []
    else:
        raw_description = request.form.get("experience_description")
        raw_links = request.form.getlist("links")
        uploads = [f for f in request.files.getlist("proofs") if f and f.filename]

    if not isinstance(raw_description, str):
        return None, description_problem
    description = raw_description.strip()
    if not EXPERIENCE_MIN_LENGTH <= len(description) <= EXPERIENCE_MAX_LENGTH:
        return None, description_problem

    links = []
    for raw in raw_links:
        links.extend(part.strip() for part in raw.splitlines() if part.strip())

    # PostgreSQL text columns cannot hold NUL characters.
    if "\x00" in description or any("\x00" in link for link in links):
        return None, ("The text contains characters that are not allowed.", "experience_description")
    if len(links) > MAX_LINKS or any(
        not LINK_RE.match(link) or len(link) > MAX_LINK_LENGTH for link in links
    ):
        return None, links_problem

    return {"description": description, "links": links, "uploads": uploads}, None


@main.route("/api/me/applications", methods=["GET"])
@login_required
def get_my_applications():
    rows = {
        row.role: row
        for row in RoleApplication.query.filter_by(user_id=current_user.id).all()
    }
    # Files uploaded before Batch 2.1B have no known application. They are
    # listed separately and never attributed to the mentor or coach application.
    legacy_credentials = (
        Credential.query.filter(
            Credential.user_id == current_user.id,
            Credential.application_id.is_(None),
        )
        .order_by(Credential.id.asc())
        .all()
    )
    response = jsonify({
        "ok": True,
        "applications": {
            role: serialize_application(current_user, role, rows.get(role))
            for role in ("mentor", "coach")
        },
        "legacy_credentials": [serialize_application_credential(c) for c in legacy_credentials],
        "limits": {
            "experience_min_length": EXPERIENCE_MIN_LENGTH,
            "experience_max_length": EXPERIENCE_MAX_LENGTH,
            "max_links": MAX_LINKS,
            "max_files": MAX_FILES,
            "max_file_bytes": MAX_FILE_BYTES,
            "allowed_file_types": sorted(FILE_SIGNATURES),
        },
    })
    response.headers["Cache-Control"] = "no-store, private"
    return response


@main.route("/api/me/applications/<role>", methods=["POST"])
@login_required
def submit_my_application(role):
    """Submit or resubmit the current user's application for one provider role.

    Never changes user.roles: only an admin approval can grant a role.
    """
    if role not in APPLICATION_ROLE_LABELS:
        return error("Unknown role. Apply as mentor or coach.", 404, field="role")

    user_id = current_user.id
    row = RoleApplication.query.filter_by(user_id=user_id, role=role).first()
    conflict = application_conflict_message(current_user, role, row)
    if conflict:
        return error(conflict, 409, field="status")

    data, problem = parse_application_request()
    if problem:
        return error(problem[0], 400, field=problem[1])

    # Validate every file before anything is uploaded.
    uploads = data["uploads"]
    existing_files = (
        Credential.query.filter_by(user_id=user_id, application_id=row.id).count()
        if row is not None
        else 0
    )
    if len(uploads) > MAX_FILES:
        return error(f"You can upload up to {MAX_FILES} files.", 400, field="proofs")
    if existing_files + len(uploads) > MAX_FILES:
        return error(
            f"This application already has {existing_files} file(s); "
            f"you can add {max(MAX_FILES - existing_files, 0)} more.",
            400,
            field="proofs",
        )
    checked_files = []
    for upload in uploads:
        ext, file_problem = check_upload(upload)
        if file_problem:
            return error(file_problem, 400, field="proofs")
        checked_files.append((upload, ext))

    # Storage paths are generated here, never taken from the client.
    planned = []
    for upload, ext in checked_files:
        planned.append({
            "path": f"{user_id}/{role}/{uuid.uuid4().hex}.{ext}",
            "ext": ext,
            "name": secure_filename(upload.filename)[-255:],
            "data": upload.stream.read(),
        })
    intended_paths = [item["path"] for item in planned]

    # Storage is reached only when files were attached.
    if planned:
        # End the read-only transaction so no database connection is held open
        # during network I/O; the state is re-read under a lock below.
        db.session.rollback()
        try:
            bucket = credentials_bucket()
            for item in planned:
                bucket.upload(
                    item["path"],
                    item["data"],
                    {"content-type": CREDENTIAL_MIME_TYPES[item["ext"]], "upsert": "false"},
                )
        except Exception:
            current_app.logger.warning("Application evidence upload failed; internal details suppressed.")
            cleanup_credential_objects(intended_paths)
            return error("We couldn't upload your files. Please try again.", 500, field="proofs")

    created = False
    commit_started = False
    try:
        applicant = User.query.filter_by(id=user_id).with_for_update().populate_existing().first()
        if applicant is None or not applicant.is_active:
            db.session.rollback()
            cleanup_credential_objects(intended_paths)
            return error("Account is unavailable.", 403)
        # Re-read under a row lock: the state may have changed during the uploads.
        row = (
            RoleApplication.query.filter_by(user_id=user_id, role=role)
            .with_for_update()
            .populate_existing()
            .first()
        )
        conflict = application_conflict_message(applicant, role, row)
        if conflict:
            db.session.rollback()
            cleanup_credential_objects(intended_paths)
            return error(conflict, 409, field="status")

        if row is not None and Credential.query.filter_by(user_id=user_id, application_id=row.id).count() + len(planned) > MAX_FILES:
            db.session.rollback()
            cleanup_credential_objects(intended_paths)
            return error("This application already has the maximum number of files.", 409, field="proofs")
        now = utcnow()
        links_text = "\n".join(data["links"])
        if row is None:
            created = True
            row = RoleApplication(
                user_id=user_id,
                role=role,
                bio=data["description"],
                links=links_text,
                status="pending",
                created_at=now,
                submitted_at=now,
            )
            db.session.add(row)
            db.session.flush()
            history = RoleApplicationHistory(
                actor_id=user_id,
                application_id=row.id,
                user_id=user_id,
                role=role,
                event_type="submitted",
                from_status="not_submitted",
                to_status="pending",
                created_at=now,
            )
        else:
            # Keep the replaced state, including the review decision, in history.
            history = RoleApplicationHistory(
                actor_id=user_id,
                application_id=row.id,
                user_id=user_id,
                role=role,
                event_type="resubmitted",
                from_status=row.status,
                to_status="pending",
                prior_bio=row.bio,
                prior_links=row.links,
                prior_review_note=row.review_note,
                prior_reviewed_by=row.reviewed_by,
                prior_reviewed_at=row.reviewed_at,
                prior_submitted_at=row.submitted_at or row.created_at,
                created_at=now,
            )
            row.bio = data["description"]
            row.links = links_text
            row.status = "pending"
            row.review_note = None
            row.reviewed_by = None
            row.reviewed_at = None
            row.submitted_at = now
        db.session.add(history)

        for item in planned:
            db.session.add(Credential(
                user_id=user_id,
                application_id=row.id,
                original_name=item["name"],
                stored_name=item["path"],
            ))
        db.session.flush()
        commit_started = True
        db.session.commit()

    except IntegrityError:
        db.session.rollback()
        cleanup_uncommitted_application_objects(intended_paths, commit_started)
        return error(
            "Your application was just submitted from another session. Refresh and try again.",
            409,
            field="status",
        )
    except Exception:
        db.session.rollback()
        current_app.logger.warning("Saving the provider application failed; internal details suppressed.")
        cleanup_uncommitted_application_objects(intended_paths, commit_started)
        return error("We couldn't save your application. Please try again.", 500)

    label = APPLICATION_ROLE_LABELS[role]
    verb = "submitted" if created else "resubmitted"
    return jsonify({
        "ok": True,
        "message": f"Your {label} application was {verb} for review.",
        "application": serialize_application(current_user, role, row),
    }), (201 if created else 200)



# ---------- Profile Management APIs (Requirements 2 & 3) ----------
@main.route("/api/profile", methods=["POST"])
@login_required
def update_profile():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    bio = data.get("bio")
    topics = data.get("topics")

    if not (2 <= len(name) <= 80):
        return error("Enter your full name (2 to 80 characters).", field="name")

    is_mentor = "mentor" in current_user.roles
    is_coach = "coach" in current_user.roles

    # Validate and handle rates
    parsed_teaching_rate = None
    has_teaching_rate = "teaching_rate" in data

    if has_teaching_rate:
        if not is_mentor:
            return error("Only approved Teachers can set a teaching rate.", 403, field="teaching_rate")
        try:
            parsed_teaching_rate = parse_rate(data["teaching_rate"])
        except ValueError as ex:
            return error(str(ex), 400, field="teaching_rate")

    parsed_coaching_rate = None
    has_coaching_rate = "coaching_rate" in data

    if has_coaching_rate:
        if not is_coach:
            return error("Only approved Coaches can set a coaching rate.", 403, field="coaching_rate")
        try:
            parsed_coaching_rate = parse_rate(data["coaching_rate"])
        except ValueError as ex:
            return error(str(ex), 400, field="coaching_rate")

    # Legacy rate parameter compatibility
    if "hourly_rate" in data and not has_teaching_rate and not has_coaching_rate:
        if not (is_mentor or is_coach):
            return error("Only teachers and coaches can set hourly rates.", 403, field="hourly_rate")
        try:
            legacy_val = parse_rate(data["hourly_rate"])
            if is_mentor:
                parsed_teaching_rate = legacy_val
                has_teaching_rate = True
            if is_coach:
                parsed_coaching_rate = legacy_val
                has_coaching_rate = True
        except ValueError as ex:
            return error(str(ex), 400, field="hourly_rate")

    try:
        current_user.display_name = name

        if bio is not None:
            current_user.bio = str(bio).strip()[:500]
        if topics is not None:
            current_user.topics = topics

        if has_teaching_rate:
            current_user.teaching_rate = parsed_teaching_rate
            if parsed_teaching_rate is not None:
                current_user.hourly_rate = int(parsed_teaching_rate)

        if has_coaching_rate:
            current_user.coaching_rate = parsed_coaching_rate
            if parsed_coaching_rate is not None and not has_teaching_rate:
                current_user.hourly_rate = int(parsed_coaching_rate)

        if "profile_picture" in data and data["profile_picture"] is not None:
            pic = str(data["profile_picture"]).strip()
            current_user.profile_picture = pic if pic else None

        db.session.commit()
        return jsonify({
            "ok": True,
            "message": "Profile updated successfully.",
            "user": user_data(current_user),
        })

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to update profile")
        return error("Failed to save changes. Please try again.", 500)


@main.route("/api/profile/avatar", methods=["POST"])
@login_required
def upload_avatar():
    if "avatar" not in request.files:
        return error("No image file provided.")

    file = request.files["avatar"]
    ext, size, val_err = validate_avatar_file(file)
    if val_err:
        return error(val_err)

    stored_filename = f"{uuid.uuid4().hex[:16]}.{ext}"
    stored_path = f"user_{current_user.id}/{stored_filename}"
    mime_type = AVATAR_MIME_TYPES[ext]
    old_avatar_url = current_user.profile_picture

    # 1. Upload new image first
    try:
        bucket = avatars_bucket()
        file.seek(0)
        file_bytes = file.read()
        bucket.upload(
            path=stored_path,
            file=file_bytes,
            file_options={
                "content-type": mime_type,
                "upsert": "false",
            },
        )
    except Exception as e:
        current_app.logger.exception("Supabase avatar upload failed")
        err_str = str(e).lower()
        if "bucket not found" in err_str or "not found" in err_str:
            return error("Avatar storage bucket is not available. Please ensure the 'avatars' bucket is created in Supabase.", 503)
        return error("Failed to upload avatar image to storage. Please try again.", 503)

    try:
        public_url = bucket.get_public_url(stored_path)
        if not is_uploaded_avatar_url(public_url, stored_path):
            raise ValueError("Storage did not return the expected public avatar URL.")
    except Exception:
        current_app.logger.warning("Could not generate the public URL for the uploaded avatar.")
        cleanup_avatar_object(stored_path)
        return error("Failed to upload avatar image to storage. Please try again.", 503)

    # 2. Update current_user.profile_picture and commit DB
    try:
        current_user.profile_picture = public_url
        db.session.commit()
    except Exception:
        # 3. If DB commit fails, rollback and remove new object from Supabase to prevent orphans
        db.session.rollback()
        current_app.logger.exception("Database commit failed after avatar upload; removing uploaded object")
        cleanup_avatar_object(stored_path)
        return error("Failed to update profile picture in database.", 500)

    # 4. Only after successful DB commit, attempt cleanup of previous Supabase avatar
    old_path_to_clean = extract_supabase_avatar_path(old_avatar_url, current_user.id)
    if old_path_to_clean and old_path_to_clean != stored_path:
        cleanup_avatar_object(old_path_to_clean)

    return jsonify({
        "ok": True,
        "message": "Avatar uploaded successfully.",
        "profile_picture": public_url,
    })


@main.route("/api/account/password", methods=["POST"])
@login_required
def update_password():
    data = request.get_json(silent=True) or {}
    current_pw = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""

    if not current_pw or not new_pw:
        return error("Please provide both your current and new password.")
    if not current_user.check_password(current_pw):
        return error("Your current password is incorrect.", 403, field="currentPassword")
    if (
        len(new_pw) < 8
        or not re.search(r"[a-z]", new_pw)
        or not re.search(r"[A-Z]", new_pw)
        or not re.search(r"\d", new_pw)
    ):
        return error(
            "Use at least 8 characters, with upper and lower case letters and a number.",
            field="newPassword",
        )
    if current_pw == new_pw:
        return error("New password must be different from your current password.", field="newPassword")

    try:
        current_user.set_password(new_pw)
        db.session.commit()
        return jsonify({"ok": True, "message": "Your password has been updated successfully."})
    except Exception:
        db.session.rollback()
        return error("Failed to update password. Please try again.", 500)


# ---------- Provider Directory API ----------
@main.route("/api/tutors", methods=["GET"])
def get_registered_tutors():
    users = User.query.filter_by(is_active=True).all()
    tutors = []

    for u in users:
        roles = u.roles
        is_mentor = "mentor" in roles
        is_coach = "coach" in roles

        if is_mentor or is_coach:
            category = "academic" if is_mentor and not is_coach else ("gaming" if is_coach and not is_mentor else "both")
            role_label = "Academic Teacher" if is_mentor and not is_coach else ("Gaming Coach" if is_coach and not is_mentor else "Teacher & Coach")

            display_rate = None
            if is_mentor and u.teaching_rate is not None:
                display_rate = float(u.teaching_rate)
            elif is_coach and u.coaching_rate is not None:
                display_rate = float(u.coaching_rate)
            elif u.hourly_rate is not None:
                display_rate = float(u.hourly_rate)

            tutors.append({
                "id": u.id,
                "name": u.display_name,
                "category": category,
                "role_label": role_label,
                "bio": u.bio or "Verified Learn2Play educator in Naga City.",
                "topics": u.topics if hasattr(u, "topics") else [],
                "profile_picture": u.profile_picture,
                "teaching_rate": float(u.teaching_rate) if u.teaching_rate is not None else None,
                "coaching_rate": float(u.coaching_rate) if u.coaching_rate is not None else None,
                "price": display_rate if display_rate is not None else 300,
                "currency": CURRENCY_CODE,
                "currency_symbol": CURRENCY_SYMBOL,
                "rating": None,
                "reviews": 0,
            })

    return jsonify({"ok": True, "tutors": tutors})


@main.route("/api/account/export", methods=["GET"])
@login_required
def export_user_data():
    apps = RoleApplication.query.filter_by(user_id=current_user.id).all()
    creds = Credential.query.filter_by(user_id=current_user.id).all()

    payload = {
        "account": user_data(current_user),
        "applications": [
            {
                "role": a.role,
                "status": a.status,
                "submitted_at": a.created_at.isoformat(),
            }
            for a in apps
        ],
        "uploaded_credentials": [c.original_name for c in creds],
        "exported_at": utcnow().isoformat(),
    }

    response = jsonify(payload)
    response.headers["Content-Disposition"] = "attachment; filename=learn2play_data.json"
    return response


@main.route("/api/account/deactivate", methods=["POST"])
@login_required
def deactivate_account():
    try:
        current_user.is_active = False
        db.session.commit()
        logout_user()
        return jsonify({
            "ok": True,
            "message": "Account deactivated.",
            "redirect": url_for("main.auth_page"),
        })
    except Exception:
        db.session.rollback()
        return error("Failed to deactivate account.", 500)


# ---------- Provider Schedule API (Requirement 4) ----------
@main.route("/api/schedule", methods=["GET"])
@provider_required
def get_provider_schedule_api():
    now = utcnow()
    appointments = (
        Appointment.query.filter_by(provider_id=current_user.id)
        .order_by(Appointment.start_time.asc())
        .all()
    )

    items = []
    for appt in appointments:
        student = appt.student
        items.append({
            "id": appt.id,
            "student_id": appt.student_id,
            "student_name": student.display_name if student else "Student",
            "student_avatar": student.profile_picture if student else None,
            "topic": appt.topic,
            "role": appt.role,
            "role_label": "Academic" if appt.role == "mentor" else "Gaming",
            "status": appt.status,
            "start_time_iso": appt.start_time.isoformat(),
            "start_time_formatted": format_dt_pht(appt.start_time),
            "time_range": format_time_range_pht(appt.start_time, appt.duration_minutes),
            "duration_minutes": appt.duration_minutes,
            "price": float(appt.price) if appt.price is not None else None,
            "currency": appt.currency or CURRENCY_CODE,
            "location_type": appt.location_type or "Online",
            "meeting_link": appt.meeting_link or "",
            "notes": appt.notes or "",
            "is_upcoming": appt.status in ("accepted", "scheduled") and appt.start_time >= now,
        })

    return jsonify({"ok": True, "appointments": items, "timezone": "Philippine Time (PHT, UTC+8)"})


@main.route("/api/tutors/<int:user_id>/availability", methods=["GET"])
@login_required
def tutor_availability(user_id):
    mentor = db.session.get(User, user_id)
    if not mentor or not mentor.is_active or not any(r in mentor.roles for r in ("mentor", "coach")):
        return error("Mentor not found.", 404)
    day_text = request.args.get("date", "")
    try:
        selected = date.fromisoformat(day_text)
    except ValueError:
        return error("Choose a valid date.")
    today = utcnow() + timedelta(hours=8)
    if selected < today.date() or selected > today.date() + timedelta(days=60):
        return jsonify({"ok": True, "slots": []})
    if MentorUnavailableDate.query.filter_by(mentor_id=user_id, unavailable_date=selected).first():
        return jsonify({"ok": True, "slots": []})
    windows = MentorAvailability.query.filter_by(mentor_id=user_id, weekday=selected.weekday()).all()
    slots = []
    booked = participant_bookings(user_id, current_user.id).all()
    for window in windows:
        start = datetime.combine(selected, time.fromisoformat(window.start_time))
        end = datetime.combine(selected, time.fromisoformat(window.end_time))
        while start + timedelta(hours=1) <= end:
            utc_start = start - timedelta(hours=8)
            conflict = any(a.start_time < utc_start + timedelta(hours=1) and
                a.start_time + timedelta(minutes=a.duration_minutes or 60) > utc_start for a in booked)
            if not conflict and start > today:
                slots.append(start.strftime("%H:%M"))
            start += timedelta(hours=1)
    return jsonify({"ok": True, "slots": slots, "timezone": "Asia/Manila"})


@main.route("/api/availability", methods=["GET", "POST", "DELETE"])
@provider_required
@reservation_errors
def manage_availability():
    if request.method == "GET":
        rows = MentorAvailability.query.filter_by(mentor_id=current_user.id).order_by(
            MentorAvailability.weekday, MentorAvailability.start_time
        ).all()
        return jsonify({"ok": True, "availability": [
            {"id": r.id, "weekday": r.weekday, "start_time": r.start_time, "end_time": r.end_time}
            for r in rows
        ]})
    lock_schedules(current_user.id)
    data = request.get_json(silent=True) or {}
    if request.method == "DELETE":
        row = MentorAvailability.query.filter_by(id=data.get("id"), mentor_id=current_user.id).first()
        if not row:
            return error("Availability window not found.", 404)
        db.session.delete(row)
        db.session.commit()
        return jsonify({"ok": True})
    try:
        weekday = int(data.get("weekday"))
        start = time.fromisoformat(str(data.get("start_time", "")))
        end = time.fromisoformat(str(data.get("end_time", "")))
    except (ValueError, TypeError):
        return error("Choose a weekday and valid start and end times.")
    if weekday not in range(7) or start >= end or start.minute % 60 or end.minute % 60:
        return error("Availability must use whole-hour slots with the end after the start.")
    row = MentorAvailability(mentor_id=current_user.id, weekday=weekday,
                             start_time=start.strftime("%H:%M"), end_time=end.strftime("%H:%M"))
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return error("That availability window already exists.")
    return jsonify({"ok": True, "id": row.id}), 201


@main.route("/api/unavailable-dates", methods=["GET", "POST", "DELETE"])
@provider_required
@reservation_errors
def manage_unavailable_dates():
    if request.method == "GET":
        rows = MentorUnavailableDate.query.filter_by(mentor_id=current_user.id).order_by(
            MentorUnavailableDate.unavailable_date
        ).all()
        return jsonify({"ok": True, "dates": [{"id": r.id, "date": r.unavailable_date.isoformat()} for r in rows]})
    lock_schedules(current_user.id)
    data = request.get_json(silent=True) or {}
    if request.method == "DELETE":
        row = MentorUnavailableDate.query.filter_by(id=data.get("id"), mentor_id=current_user.id).first()
        if not row:
            return error("Unavailable date not found.", 404)
        db.session.delete(row)
        db.session.commit()
        return jsonify({"ok": True})
    try:
        selected = date.fromisoformat(data.get("date", ""))
    except (ValueError, TypeError):
        return error("Choose a valid date.")
    today = (utcnow() + timedelta(hours=8)).date() + timedelta(days=1)
    if selected < today or selected > today + timedelta(days=365):
        return error("Unavailable dates must be within the next year.")
    row = MentorUnavailableDate(mentor_id=current_user.id, unavailable_date=selected)
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return error("That date is already marked unavailable.")
    return jsonify({"ok": True}), 201


@main.route("/api/bookings", methods=["POST"])
@login_required
@reservation_errors
def create_booking():
    if "student" not in current_user.roles or current_user.is_admin:
        return error("Only student accounts can request a session.", 403)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return error("Provide a valid booking request.")
    try:
        mentor_id = int(data.get("mentor_id"))
    except (TypeError, ValueError):
        return error("Mentor not found.", 404)
    topic = data.get("topic") or ""
    message = data.get("message") or ""
    if not isinstance(topic, str) or not isinstance(message, str):
        return error("Booking topic and message must be text.")
    topic, message = topic.strip()[:120], message.strip()[:1000]
    try:
        day = date.fromisoformat(data.get("date", ""))
        slot = time.fromisoformat(data.get("time", ""))
    except (TypeError, ValueError):
        return error("Choose an available date and time.")
    if slot.tzinfo is not None:
        return error("Choose a local Philippine time without a timezone suffix.")
    users = lock_schedules(mentor_id, current_user.id)
    mentor = users.get(mentor_id)
    if not current_user.is_active or "student" not in current_user.roles or current_user.is_admin:
        return error("Only active student accounts can request a session.", 403)
    if not mentor or not mentor.is_active or not any(r in mentor.roles for r in ("mentor", "coach")):
        return error("Mentor not found.", 404)
    if mentor.id == current_user.id:
        return error("You cannot book a session with yourself.", 403)
    requested_role = data.get("role")
    if requested_role is not None and requested_role not in ("mentor", "coach"):
        return error("Choose a valid session track.")
    role = requested_role or ("mentor" if "mentor" in mentor.roles else "coach")
    if role not in mentor.roles:
        return error("This educator does not offer that session track.", 400)
    if not topic:
        return error("Enter a subject or session topic.", field="topic")
    if slot.minute or slot.second or slot.microsecond:
        return error("Choose a whole-hour available slot.")
    if MentorUnavailableDate.query.filter_by(mentor_id=mentor.id, unavailable_date=day).first():
        return error("The mentor is unavailable on that date.")
    local_start = datetime.combine(day, slot)
    local_end = local_start + timedelta(hours=1)
    windows = MentorAvailability.query.filter_by(mentor_id=mentor.id, weekday=day.weekday()).all()
    window = any(datetime.combine(day, time.fromisoformat(w.start_time)) <= local_start and
                 datetime.combine(day, time.fromisoformat(w.end_time)) >= local_end for w in windows)
    start_utc = datetime.combine(day, slot) - timedelta(hours=8)
    now_utc = utcnow()
    if not window or start_utc <= now_utc or start_utc > now_utc + timedelta(days=60):
        return error("That time is outside the mentor's available schedule.")
    booked = participant_bookings(mentor.id, current_user.id).all()
    clash = overlaps(booked, start_utc)
    if clash:
        return error("That time was just booked. Please choose another slot.", 409)
    rate = mentor.teaching_rate if role == "mentor" else mentor.coaching_rate
    if rate is None:
        rate = mentor.hourly_rate
    appt = Appointment(provider_id=mentor.id, student_id=current_user.id, role=role,
        topic=topic, start_time=start_utc, duration_minutes=60, status="pending",
        price=Decimal(str(rate or 0)).quantize(Decimal("0.01")), currency="PHP", location_type="Online", notes=message,
        payment_required=Decimal(str(rate or 0)) > 0)
    try:
        db.session.add(appt)
        db.session.flush()
        if appt.payment_required:
            if __package__:
                from .payments import new_payment
            else:
                from payments import new_payment
            db.session.add(new_payment(appt))
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return error("Booking could not be saved. Please retry.", 503)
    return jsonify({"ok": True, "payment_url": url_for("payments.student_payment", booking_id=appt.id) if appt.payment_required else None, "booking_id": appt.id, "message": "Booking request sent to the mentor."}), 201


@main.route("/api/bookings", methods=["GET"])
@login_required
def list_my_bookings():
    rows = Appointment.query.filter(
        (Appointment.student_id == current_user.id) | (Appointment.provider_id == current_user.id)
    ).order_by(Appointment.start_time.asc()).all()
    return jsonify({"ok": True, "bookings": [{
        "id": a.id, "student_id": a.student_id, "provider_id": a.provider_id,
        "student": a.student.display_name if a.student else "Student",
        "mentor": a.provider.display_name if a.provider else "Mentor",
        "role": a.role, "topic": a.topic, "status": a.status,
        "provider_confirmed": a.provider_confirmed_at is not None,
        "student_confirmed": a.student_confirmed_at is not None,
        "attendance_disputed": a.attendance_disputed,
        "attendance_closed": a.attendance_closed_at is not None,
        "attendance_resolution": a.attendance_resolution,
        "attendance_resolution_reason": a.attendance_resolution_reason,
        "xp_awarded": XpEvent.query.filter_by(appointment_id=a.id).first() is not None,
        "start_time": a.start_time.replace(tzinfo=timezone.utc).isoformat(),
        "formatted": format_dt_pht(a.start_time), "duration": a.duration_minutes,
        "price": float(a.price) if a.price is not None else None, "notes": a.notes or "",
        "payment_status": a.payment.status if a.payment and (a.student_id == current_user.id or current_user.is_admin) else None,
        "payment_reference": a.payment.reference if a.payment and (a.student_id == current_user.id or current_user.is_admin) else None,
        "payment_satisfied": a.payment_satisfied,
        "payment_url": url_for("payments.student_payment", booking_id=a.id) if a.payment and (a.student_id == current_user.id or current_user.is_admin) else None,
        "reconciliation_state": a.payment.reconciliation_state if a.payment and (a.student_id == current_user.id or current_user.is_admin) else None,
        "reconciliation_explanation": a.payment.reconciliation_explanation if a.payment and (a.student_id == current_user.id or current_user.is_admin) else None,
        "ended_at": a.ended_at.isoformat() if a.ended_at else None,
        "ending_reason": a.ending_reason,
        "meeting_room": bool(a.meeting_room)
    } for a in rows]})


def booking_ending_reason(default=None):
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ReconciliationError("Provide a valid booking-ending object.", 400)
    if data.get("reason") is None and default is not None:
        return default
    return text_field(data.get("reason"), "a booking ending reason")


@main.route("/api/bookings/<int:booking_id>/<action>", methods=["POST"])
@login_required
@reservation_errors
def update_booking(booking_id, action):
    participants = None
    if action == "accept":
        snapshot = db.session.get(Appointment, booking_id)
        if snapshot is None:
            return error("Booking not found.", 404)
        if snapshot.provider_id != current_user.id:
            return error("You cannot update this booking.", 403)
        participants = (snapshot.provider_id, snapshot.student_id)
        lock_schedules(*participants)
    payment = None
    if action in ("cancel", "reject"):
        try:
            payment, appt = lock_booking_end(booking_id)
        except ReconciliationError as exc:
            return error(exc.message, exc.status)
    else:
        appt = Appointment.query.filter_by(id=booking_id).populate_existing().with_for_update().first()
    if not appt:
        return error("Booking not found.", 404)
    if action in ("accept", "reject"):
        if action == "accept" and not provider_approved_for_role(current_user, appt.role):
            return error("You are not approved for this booking role.", 403)
        if appt.provider_id != current_user.id or appt.status != "pending":
            return error("You cannot update this booking.", 403)
        if action == "reject":
            try:
                end_booking(appt, payment, current_user.id, "rejected",
                            booking_ending_reason("Rejected by the assigned provider."))
            except ReconciliationError as exc:
                return error(exc.message, exc.status)
        else:
            if participants != (appt.provider_id, appt.student_id):
                return error("This booking changed. Refresh and retry.", 409)
            others = participant_bookings(*participants).filter(Appointment.id != appt.id).all()
            if overlaps(others, appt.start_time, appt.duration_minutes):
                return error("This session conflicts with another reservation. Reject or cancel the conflicting request first.", 409)
            appt.status = "accepted"
            appt.meeting_room = appt.meeting_room or "l2-" + secrets.token_urlsafe(18)
    elif action == "cancel":
        if current_user.id not in (appt.student_id, appt.provider_id) or appt.status not in ("pending", "accepted", "scheduled"):
            return error("You cannot cancel this booking.", 403)
        try:
            role = "student" if current_user.id == appt.student_id else "provider"
            end_booking(appt, payment, current_user.id, "cancelled",
                        booking_ending_reason(f"Cancelled by the assigned {role}."))
        except ReconciliationError as exc:
            return error(exc.message, exc.status)
    elif action in ("complete", "confirm-attendance", "dispute-attendance"):
        if action == "complete" and not provider_approved_for_role(current_user, appt.role):
            return error("You are not approved for this booking role.", 403)
        if __package__:
            from .attendance import AttendanceError, apply_attendance
        else:
            from attendance import AttendanceError, apply_attendance
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return error("Provide a valid attendance object.")
        try:
            apply_attendance(appt, current_user.id, action, data.get("note", ""))
        except AttendanceError as exc:
            db.session.rollback()
            return error(exc.message, exc.status)
    else:
        return error("Unsupported booking action.")
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return error("Booking update could not be saved. Please retry.", 503)
    return jsonify({"ok": True, "status": appt.status,
        "provider_confirmed": appt.provider_confirmed_at is not None,
        "student_confirmed": appt.student_confirmed_at is not None,
        "attendance_disputed": appt.attendance_disputed,
        "attendance_closed": appt.attendance_closed_at is not None})


@main.route("/api/admin/bookings/<int:booking_id>/<action>", methods=["POST"])
@admin_required
@reservation_errors
def admin_end_booking(booking_id, action):
    if action not in ("cancel", "reject"):
        return error("Choose cancel or reject.", 400)
    try:
        payment, appt = lock_booking_end(booking_id)
        if appt is None:
            return error("Booking not found.", 404)
        end_booking(appt, payment, current_user.id,
                    "cancelled" if action == "cancel" else "rejected", booking_ending_reason())
        db.session.commit()
    except ReconciliationError as exc:
        return error(exc.message, exc.status)
    return jsonify(ok=True, status=appt.status, message="Booking ending recorded. No refund was processed.")


@main.route("/api/student/progress", methods=["GET"])
@login_required
def student_progress():
    if current_user.is_admin or "student" not in current_user.roles:
        return error("Only student accounts can view progress.", 403)

    tz = ZoneInfo("Asia/Manila")
    now = datetime.now(timezone.utc).astimezone(tz)
    week_start = now.date() - timedelta(days=now.weekday())
    events = XpEvent.query.filter_by(user_id=current_user.id).order_by(XpEvent.created_at.asc()).all()
    total = sum(event.points for event in events)
    academic = sum(event.points for event in events if event.path == "academic")
    gaming = sum(event.points for event in events if event.path == "gaming")
    week_events = [event for event in events if event.created_at.replace(tzinfo=timezone.utc).astimezone(tz).date() >= week_start]
    academic_week = sum(1 for event in week_events if event.path == "academic")
    gaming_week = sum(1 for event in week_events if event.path == "gaming")
    week_paths = {event.path for event in week_events}
    balance_streak, current_week_balanced = _balance_streak_from_events(events, week_start, tz)
    levels = [(0, "Beginner"), (100, "Learner"), (250, "Competitor"), (500, "Achiever"), (1000, "Elite Student Gamer")]
    level_index = max(i for i, (threshold, _) in enumerate(levels) if total >= threshold)
    level_at, level_name = levels[level_index]
    next_level = levels[level_index + 1] if level_index + 1 < len(levels) else None
    level_end = next_level[0] if next_level else level_at + 500

    return jsonify({
        "ok": True,
        "timezone": "Asia/Manila",
        "total_xp": total,
        "academic_xp": academic,
        "gaming_xp": gaming,
        "level": level_index + 1,
        "level_name": level_name,
        "next_level_xp": next_level[0] if next_level else None,
        "level_progress": min(100, round((total - level_at) / (level_end - level_at) * 100)),
        "quests": {
            "academic_week": {"count": min(academic_week, 3), "target": 3},
            "gaming_week": {"count": min(gaming_week, 2), "target": 2},
            "balanced_week": {"count": len(week_paths), "target": 2},
            "first_milestone": {"count": min(total, 100), "target": 100},
        },
        "badges": {
            "academic": academic > 0,
            "gaming": gaming > 0,
            "combined": academic > 0 and gaming > 0,
        },
        "week_start": week_start.isoformat(),
        "balance_streak_weeks": balance_streak,
        "current_week_balanced": current_week_balanced,
        "activity_days": sorted({event.created_at.replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat() for event in week_events}),
    })


@main.route("/api/student/leaderboard-preference", methods=["POST"])
@login_required
def update_leaderboard_preference():
    if current_user.is_admin or "student" not in current_user.roles:
        return error("Only student accounts can change leaderboard participation.", 403)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return error("Provide a valid preference object.")
    opted_in = data.get("opted_in")
    if not isinstance(opted_in, bool):
        return error("Choose whether to opt in or out.", field="opted_in")
    current_user.leaderboard_opt_in = opted_in
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return error("Preference could not be saved. Please retry.", 503)
    return jsonify({"ok": True, "opted_in": current_user.leaderboard_opt_in})


@main.route("/api/leaderboard/weekly", methods=["GET"])
@login_required
def weekly_leaderboard_data():
    if current_user.is_admin or "student" not in current_user.roles:
        return error("Only student accounts can view the leaderboard.", 403)
    tz = ZoneInfo("Asia/Manila")
    now = datetime.now(timezone.utc).astimezone(tz)
    local_start = datetime.combine(now.date() - timedelta(days=now.weekday()), time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=7)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    # Rank in the database, keeping the public result bounded to 50.
    # Private progress can find the requesting student below that cutoff
    # without loading every participant into Python.
    ranking = (
        db.session.query(
            User.id.label("user_id"), User.display_name.label("display_name"),
            func.sum(XpEvent.points).label("weekly_xp"),
            func.row_number().over(order_by=(
                func.sum(XpEvent.points).desc(), User.display_name.asc(), User.id.asc(),
            )).label("rank"),
        )
        .join(XpEvent, XpEvent.user_id == User.id)
        .filter(
            User.is_active.is_(True),
            User.is_admin.is_(False),
            User.leaderboard_opt_in.is_(True),
            XpEvent.created_at >= start_utc,
            XpEvent.created_at < end_utc,
        )
        .group_by(User.id, User.display_name)
        .subquery()
    )
    rows = db.session.query(ranking).order_by(ranking.c.rank).limit(50).all()
    own_row = (
        db.session.query(ranking.c.rank, ranking.c.weekly_xp)
        .filter(ranking.c.user_id == current_user.id).first()
        if current_user.leaderboard_opt_in else None
    )
    return jsonify({
        "ok": True,
        "week_start": local_start.date().isoformat(),
        "week_end": (local_end.date() - timedelta(days=1)).isoformat(),
        "timezone": "Asia/Manila",
        "opted_in": current_user.leaderboard_opt_in,
        "your_progress": (
            {"weekly_xp": int(own_row.weekly_xp), "rank": own_row.rank}
            if own_row else ({"weekly_xp": 0, "rank": None} if current_user.leaderboard_opt_in else None)
        ),
        "entries": [
            {"rank": row.rank, "display_name": row.display_name, "weekly_xp": int(row.weekly_xp)}
            for row in rows
        ],
    })




@main.route("/api/sessions/<int:booking_id>/join", methods=["GET"])
@login_required
def join_session(booking_id):
    appt = db.session.get(Appointment, booking_id)
    if not appt or (appt.student_id != current_user.id and appt.provider_id != current_user.id):
        return error("Session not found.", 404)
    if appt.status not in ("accepted", "scheduled") or not appt.meeting_room:
        return error("The mentor has not confirmed this session.", 403)
    if not appt.payment_satisfied:
        return error("Payment must be verified before joining.", 403)
    return jsonify({"ok": True, "url": url_for("main.meeting_page", booking_id=appt.id)})


@main.route("/sessions/<int:booking_id>/meeting", methods=["GET"])
@login_required
def meeting_page(booking_id):
    appt = db.session.get(Appointment, booking_id)
    if not appt or (appt.student_id != current_user.id and appt.provider_id != current_user.id):
        return error("Session not found.", 404)
    if appt.status not in ("accepted", "scheduled") or not appt.meeting_room:
        return error("The mentor has not confirmed this session.", 403)
    if not appt.payment_satisfied:
        return error("Payment must be verified before joining.", 403)
    try:
        participant_token = create_jaas_participant_token(appt, current_user)
    except JaasConfigurationError:
        return error("Video meetings are temporarily unavailable. Please contact support.", 503)
    return render_template(
        "meeting.html",
        jaas_app_id=current_app.config["JAAS_APP_ID"],
        meeting_room=appt.meeting_room,
        participant_token=participant_token,
    )


# ---------- Frontend View Routes ----------
@main.route("/dashboard", methods=["GET"])
@login_required
def dashboard():

    if current_user.is_admin:
        return redirect(url_for("main.admin_dashboard"))
    
    next_session = None
    prev_session = None

    if any(r in current_user.roles for r in ("mentor", "coach")):
        now = utcnow()
        # Actual upcoming session
        next_session = (
            Appointment.query.filter(Appointment.provider_id == current_user.id, Appointment.status.in_(["accepted", "scheduled"]))
            .filter(Appointment.start_time >= now)
            .order_by(Appointment.start_time.asc())
            .first()
        )
        # Actual previous session
        prev_session = (
            Appointment.query.filter_by(provider_id=current_user.id)
            .filter((Appointment.status == "completed") | (Appointment.status.in_(["accepted", "scheduled"]) & (Appointment.start_time < now)))
            .order_by(Appointment.start_time.desc())
            .first()
        )

    # Pending provider roles: applied but not yet approved
    pending_provider_roles = []
    if current_user.is_authenticated:
        apps = RoleApplication.query.filter_by(user_id=current_user.id, status="pending").all()
        pending_provider_roles = [a.role for a in apps]

    return render_template(
        "shared/dashboard.html",
        next_session=next_session,
        prev_session=prev_session,
        format_dt_pht=format_dt_pht,
        pending_provider_roles=pending_provider_roles,
    )


@main.route("/schedule")
@provider_required
def schedule():
    now = utcnow()

    # Nearest upcoming scheduled sessions
    upcoming = (
        Appointment.query.filter(Appointment.provider_id == current_user.id, Appointment.status.in_(["accepted", "scheduled"]))
        .filter(Appointment.start_time >= now)
        .order_by(Appointment.start_time.asc())
        .all()
    )

    # Past completed sessions or past scheduled sessions
    past = (
        Appointment.query.filter_by(provider_id=current_user.id)
        .filter(
            (Appointment.status == "completed")
            | (Appointment.status.in_(["accepted", "scheduled"]) & (Appointment.start_time < now))
        )
        .order_by(Appointment.start_time.desc())
        .all()
    )

    # Cancelled sessions
    cancelled = (
        Appointment.query.filter(Appointment.provider_id == current_user.id, Appointment.status.in_(["cancelled", "rejected"]))
        .order_by(Appointment.start_time.desc())
        .all()
    )

    return render_template(
        "mentor/schedule.html",
        upcoming=upcoming,
        past=past,
        cancelled=cancelled,
        total_count=len(upcoming) + len(past) + len(cancelled),
        format_dt_pht=format_dt_pht,
        format_time_range_pht=format_time_range_pht,
        timezone_label="Philippine Time (PHT, UTC+8)",
    )

@main.route("/tutors/<int:user_id>")
@login_required
def view_tutor_profile(user_id):
    tutor = db.session.get(User, user_id)
    # Ensure user exists, is active, and is actually a teacher or coach
    if not tutor or not tutor.is_active or not any(r in tutor.roles for r in ("mentor", "coach")):
        abort(404)

    is_mentor = "mentor" in tutor.roles
    is_coach = "coach" in tutor.roles
    role_label = (
        "Academic Teacher & Gaming Coach" if is_mentor and is_coach
        else ("Academic Teacher" if is_mentor else "Gaming Coach")
    )

    return render_template(
        "tutor_profile.html",
        tutor=tutor,
        role_label=role_label,
        is_mentor=is_mentor,
        is_coach=is_coach,
        availability=MentorAvailability.query.filter_by(mentor_id=tutor.id).order_by(
            MentorAvailability.weekday, MentorAvailability.start_time
        ).all(),
    )


@main.route("/tutors/<int:user_id>/book")
@login_required
def book_tutor(user_id):
    tutor = db.session.get(User, user_id)
    if not tutor or not tutor.is_active or not any(r in tutor.roles for r in ("mentor", "coach")):
        abort(404)
    if "student" not in current_user.roles or current_user.is_admin or current_user.id == tutor.id:
        return redirect(url_for("main.view_tutor_profile", user_id=user_id))
    return render_template(
        "student/book-session.html",
        tutor=tutor,
        is_mentor="mentor" in tutor.roles,
        is_coach="coach" in tutor.roles,
    )

@main.route("/profile")
@login_required
def profile():
    if any(role in current_user.roles for role in ("mentor", "coach")):
        return render_template(
            "mentor/profile.html",
            availability=MentorAvailability.query.filter_by(mentor_id=current_user.id).order_by(
                MentorAvailability.weekday, MentorAvailability.start_time
            ).all(),
            credentials=Credential.query.filter_by(user_id=current_user.id).order_by(
                Credential.created_at.desc()
            ).all(),
        )
    return render_template("shared/profile.html")


@main.route("/sessions")
@login_required
def sessions():
    return render_template("shared/sessions.html")


@main.route("/leaderboard")
@login_required
def leaderboard_page():
    if current_user.is_admin or "student" not in current_user.roles:
        return error("Only student accounts can view the leaderboard.", 403)
    return render_template("student/leaderboard.html")


@main.route("/settings")
@login_required
def settings():
    return render_template("shared/settings.html")


@main.route("/help")
def help():
    return render_template("help.html")


# messages route moved to messages_page() below

# ============================================================
# ROUTES ADDITION — Admin pages, Reports API, Messages API,
# Verification-status API, and pending-provider guard.
# Paste/append this block at the END of routes.py, before
# or after the existing @main.route("/messages") handler
# (replace the stub messages route entirely).
# ============================================================

# Update the import at the top of routes.py to include Report and Message:
# from .models import Appointment, Credential, Message, Report, RoleApplication, RoleApplicationHistory, User, XpEvent, AttendanceEvent, utcnow

# ---------- Pending-provider helper ----------

def get_pending_roles(user):
    """Return the list of roles the user has applied for but not yet been approved."""
    apps = RoleApplication.query.filter_by(user_id=user.id, status="pending").all()
    return [a.role for a in apps]


# ---------- Admin Stats API ----------

@main.route("/api/admin/stats", methods=["GET"])
@admin_required
def admin_stats():
    pending_count = RoleApplication.query.filter_by(status="pending").count()
    total_users = User.query.count()
    all_users = User.query.all()
    total_mentors = sum(1 for u in all_users if "mentor" in u.roles)
    total_coaches = sum(1 for u in all_users if "coach" in u.roles)
    total_students = sum(1 for u in all_users if "student" in u.roles)
    total_sessions = Appointment.query.count()
    pending_bookings = Appointment.query.filter_by(status="pending").count()
    now = utcnow()
    upcoming = Appointment.query.filter(Appointment.status.in_(["accepted", "scheduled"]), Appointment.start_time >= now).count()
    recent = Appointment.query.order_by(Appointment.created_at.desc()).limit(8).all()
    open_reports = Report.query.filter_by(status="open").count()
    return jsonify({
        "ok": True,
        "pending_verifications": pending_count,
        "total_users": total_users,
        "total_mentors": total_mentors,
        "total_coaches": total_coaches,
        "total_students": total_students,
        "total_sessions": total_sessions,
        "total_bookings": total_sessions,
        "pending_bookings": pending_bookings,
        "upcoming_sessions": upcoming,
        "recent_activity": [{"student": a.student.display_name if a.student else "Student",
            "mentor": a.provider.display_name if a.provider else "Mentor", "topic": a.topic,
            "status": a.status, "created_at": format_dt_pht(a.created_at)} for a in recent],
        "open_reports": open_reports,
    })


# ---------- Admin User Management API ----------

@main.route("/api/admin/users", methods=["GET"])
@admin_required
def admin_list_users():
    users = User.query.order_by(User.id.asc()).all()
    items = []
    for u in users:
        pending = RoleApplication.query.filter_by(user_id=u.id, status="pending").all()
        items.append({
            "id": u.id,
            "name": u.display_name,
            "email": u.email,
            "roles": u.roles,
            "is_admin": u.is_admin,
            "is_active": u.is_active,
            "pending_roles": [a.role for a in pending],
        })
    return jsonify({"ok": True, "users": items})


@main.route("/api/admin/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def admin_toggle_user_active(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return error("User not found.", 404)
    if user.id == current_user.id:
        return error("You cannot deactivate your own account.", 400)
    user.is_active = not user.is_active
    db.session.commit()
    state = "activated" if user.is_active else "deactivated"
    return jsonify({"ok": True, "message": f"User {state}.", "is_active": user.is_active})


@main.route("/api/admin/users/<int:user_id>/set-admin", methods=["POST"])
@admin_required
def admin_set_admin(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return error("User not found.", 404)
    if user.id == current_user.id:
        return error("You cannot change your own admin status.", 400)
    data = request.get_json(silent=True) or {}
    user.is_admin = bool(data.get("is_admin", False))
    db.session.commit()
    return jsonify({"ok": True, "message": "Admin status updated.", "is_admin": user.is_admin})


# ---------- Reports API ----------

REPORT_REASONS = [
    "Harassment",
    "Inappropriate content",
    "Cheating / unfair play",
    "No-show / missed session",
    "Unprofessional conduct",
    "Fake credentials",
    "Spam",
    "Other",
]


@main.route("/api/reports", methods=["POST"])
@login_required
def submit_report():
    data = request.get_json(silent=True) or {}
    reported_id = data.get("reported_id")
    reason = (data.get("reason") or "").strip()
    details = (data.get("details") or "").strip()
    appointment_id = data.get("appointment_id")  # optional

    if not reported_id:
        return error("Please specify who you are reporting.", field="reported_id")
    if reported_id == current_user.id:
        return error("You cannot report yourself.")
    reported_user = db.session.get(User, reported_id)
    if not reported_user or not reported_user.is_active:
        return error("That user does not exist.", 404)
    if reason not in REPORT_REASONS:
        return error("Please select a valid reason.", field="reason")
    if details and len(details) > 2000:
        return error("Details must not exceed 2000 characters.", field="details")

    appt = None
    if appointment_id:
        appt = db.session.get(Appointment, appointment_id)
        if not appt or (appt.student_id != current_user.id and appt.provider_id != current_user.id):
            return error("Invalid session reference.", field="appointment_id")

    try:
        report = Report(
            reporter_id=current_user.id,
            reported_id=reported_id,
            appointment_id=appt.id if appt else None,
            reason=reason,
            details=details or None,
        )
        db.session.add(report)
        db.session.commit()
        return jsonify({"ok": True, "message": "Your report has been submitted. We'll review it shortly."}), 201
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to save report")
        return error("Failed to submit report. Please try again.", 500)


@main.route("/api/admin/reports", methods=["GET"])
@admin_required
def admin_list_reports():
    status_filter = request.args.get("status", "open")
    query = Report.query
    if status_filter != "all":
        query = query.filter_by(status=status_filter)
    reports = query.order_by(Report.created_at.desc()).all()

    items = []
    for r in reports:
        reporter = db.session.get(User, r.reporter_id)
        reported = db.session.get(User, r.reported_id)
        items.append({
            "id": r.id,
            "reason": r.reason,
            "details": r.details or "",
            "status": r.status,
            "admin_note": r.admin_note or "",
            "created_at": r.created_at.isoformat(),
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "reporter": {"id": reporter.id, "name": reporter.display_name, "email": reporter.email} if reporter else None,
            "reported": {"id": reported.id, "name": reported.display_name, "email": reported.email} if reported else None,
            "appointment_id": r.appointment_id,
        })
    return jsonify({"ok": True, "reports": items})


@main.route("/api/admin/reports/<int:report_id>/<action>", methods=["POST"])
@admin_required
def admin_review_report(report_id, action):
    if action not in {"reviewed", "dismissed"}:
        return error("Unknown action.", 404)
    report = db.session.get(Report, report_id)
    if not report:
        return error("Report not found.", 404)
    data = request.get_json(silent=True) or {}
    report.status = action
    report.admin_note = str(data.get("note") or "").strip()[:1000] or None
    report.reviewed_by = current_user.id
    report.reviewed_at = utcnow()
    db.session.commit()
    return jsonify({"ok": True, "message": f"Report marked as {action}."})


@main.route("/api/report-reasons", methods=["GET"])
@login_required
def report_reasons():
    return jsonify({"ok": True, "reasons": REPORT_REASONS})


# ---------- Verification Status API ----------

@main.route("/api/me/verification-status", methods=["GET"])
@login_required
def verification_status():
    pending = RoleApplication.query.filter_by(user_id=current_user.id, status="pending").all()
    approved = RoleApplication.query.filter_by(user_id=current_user.id, status="approved").all()
    rejected = RoleApplication.query.filter_by(user_id=current_user.id, status="rejected").all()
    revision = RoleApplication.query.filter_by(user_id=current_user.id, status="revision_required").all()
    return jsonify({
        "ok": True,
        "pending_roles": [a.role for a in pending],
        "approved_roles": [a.role for a in approved],
        "rejected_roles": [a.role for a in rejected],
        "revision_required_roles": [a.role for a in revision],
    })


# ---------- Messages / Chat API ----------

@main.route("/api/messages/threads", methods=["GET"])
@login_required
def message_threads():
    """Return all conversation threads for the current user, split by type."""
    uid = current_user.id

    # Peer-to-peer threads (appointment_id IS NULL)
    p2p_raw = Message.query.filter(
        Message.appointment_id == None,  # noqa: E711
        (Message.sender_id == uid) | (Message.recipient_id == uid),
    ).order_by(Message.created_at.desc()).all()

    # Group by the other user
    p2p_map = {}
    for msg in p2p_raw:
        other_id = msg.recipient_id if msg.sender_id == uid else msg.sender_id
        if other_id not in p2p_map:
            other = db.session.get(User, other_id)
            unread = Message.query.filter_by(
                sender_id=other_id, recipient_id=uid, appointment_id=None, is_read=False
            ).count()
            p2p_map[other_id] = {
                "type": "peer",
                "other_user": {"id": other.id, "name": other.display_name, "avatar": other.profile_picture} if other else None,
                "last_message": msg.body[:100],
                "last_at": msg.created_at.isoformat(),
                "unread": unread,
            }

    # Session threads (appointment_id IS NOT NULL)
    session_raw = Message.query.filter(
        Message.appointment_id != None,  # noqa: E711
        (Message.sender_id == uid) | (Message.recipient_id == uid),
    ).order_by(Message.created_at.desc()).all()

    session_map = {}
    for msg in session_raw:
        aid = msg.appointment_id
        if aid not in session_map:
            appt = db.session.get(Appointment, aid)
            other_id = appt.student_id if appt and appt.provider_id == uid else (appt.provider_id if appt else None)
            other = db.session.get(User, other_id) if other_id else None
            unread = Message.query.filter_by(appointment_id=aid, recipient_id=uid, is_read=False).count()
            session_map[aid] = {
                "type": "session",
                "appointment_id": aid,
                "topic": appt.topic if appt else "Session",
                "other_user": {"id": other.id, "name": other.display_name, "avatar": other.profile_picture} if other else None,
                "last_message": msg.body[:100],
                "last_at": msg.created_at.isoformat(),
                "unread": unread,
            }

    return jsonify({
        "ok": True,
        "peer_threads": list(p2p_map.values()),
        "session_threads": list(session_map.values()),
    })


@main.route("/api/messages/thread/<int:other_user_id>", methods=["GET"])
@login_required
def peer_thread(other_user_id):
    """Fetch all peer-to-peer messages between current_user and other_user_id."""
    other = db.session.get(User, other_user_id)
    if not other or not other.is_active:
        return error("User not found.", 404)

    uid = current_user.id
    msgs = Message.query.filter(
        Message.appointment_id == None,  # noqa: E711
        ((Message.sender_id == uid) & (Message.recipient_id == other_user_id)) |
        ((Message.sender_id == other_user_id) & (Message.recipient_id == uid)),
    ).order_by(Message.created_at.asc()).all()

    # Mark incoming as read
    for m in msgs:
        if m.recipient_id == uid and not m.is_read:
            m.is_read = True
    db.session.commit()

    return jsonify({
        "ok": True,
        "messages": [
            {
                "id": m.id,
                "sender_id": m.sender_id,
                "body": m.body,
                "created_at": m.created_at.isoformat(),
                "is_read": m.is_read,
            }
            for m in msgs
        ],
        "other_user": {"id": other.id, "name": other.display_name, "avatar": other.profile_picture},
    })


@main.route("/api/messages/session/<int:appointment_id>", methods=["GET"])
@login_required
def session_thread(appointment_id):
    """Fetch messages for a specific booked session thread."""
    appt = db.session.get(Appointment, appointment_id)
    if not appt:
        return error("Session not found.", 404)
    uid = current_user.id
    if appt.student_id != uid and appt.provider_id != uid:
        return error("Access denied.", 403)

    msgs = Message.query.filter_by(appointment_id=appointment_id).order_by(Message.created_at.asc()).all()

    for m in msgs:
        if m.recipient_id == uid and not m.is_read:
            m.is_read = True
    db.session.commit()

    return jsonify({
        "ok": True,
        "appointment": {"id": appt.id, "topic": appt.topic, "status": appt.status},
        "messages": [
            {
                "id": m.id,
                "sender_id": m.sender_id,
                "body": m.body,
                "created_at": m.created_at.isoformat(),
                "is_read": m.is_read,
            }
            for m in msgs
        ],
    })


@main.route("/api/messages/send", methods=["POST"])
@login_required
def send_message_api():
    data = request.get_json(silent=True) or {}
    recipient_id = data.get("recipient_id")
    body = (data.get("body") or "").strip()
    appointment_id = data.get("appointment_id")  # optional

    if not recipient_id:
        return error("No recipient specified.", field="recipient_id")
    if not body:
        return error("Message cannot be empty.", field="body")
    if len(body) > 3000:
        return error("Message is too long (max 3000 characters).", field="body")
    if recipient_id == current_user.id:
        return error("You cannot message yourself.")

    recipient = db.session.get(User, recipient_id)
    if not recipient or not recipient.is_active:
        return error("Recipient not found.", 404)

    # Peer-to-peer: only students can initiate P2P (appointment_id=None)
    if not appointment_id:
        if "student" not in current_user.roles and not current_user.is_admin:
            return error("Only students can send peer-to-peer messages.", 403)
        if "student" not in recipient.roles and not recipient.is_admin:
            return error("You can only send peer messages to fellow students.", 403)

    appt = None
    if appointment_id:
        appt = db.session.get(Appointment, appointment_id)
        if not appt:
            return error("Session not found.", 404)
        uid = current_user.id
        if appt.student_id != uid and appt.provider_id != uid:
            return error("You are not part of this session.", 403)
        expected_recipient = appt.provider_id if uid == appt.student_id else appt.student_id
        if recipient_id != expected_recipient:
            return error("Session messages can only be sent to the other participant.", 403)

    try:
        msg = Message(
            sender_id=current_user.id,
            recipient_id=recipient_id,
            appointment_id=appt.id if appt else None,
            body=body,
        )
        db.session.add(msg)
        db.session.commit()
        return jsonify({
            "ok": True,
            "message": {
                "id": msg.id,
                "sender_id": msg.sender_id,
                "body": msg.body,
                "created_at": msg.created_at.isoformat(),
                "is_read": msg.is_read,
            },
        }), 201
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to send message")
        return error("Failed to send message. Please try again.", 500)


# ---------- Admin Page Routes ----------

@main.route("/admin")
@main.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template("admin/dashboard.html")


@main.route("/admin/verification")
@admin_required
def admin_verification():
    return render_template("admin/verification.html")


@main.route("/admin/reports")
@admin_required
def admin_reports_page():
    return render_template("admin/reports.html")


@main.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin/users.html")


@main.route("/admin/settings")
@admin_required
def admin_settings():
    return render_template("admin/settings.html")


# ---------- Replace stub messages route ----------
# (Delete or comment out the old /messages route and use this one)

@main.route("/messages")
@login_required
def messages_page():
    return render_template("shared/messages.html")

# Peer user search — returns students the current user can start a P2P chat with
@main.route("/api/messages/peer-users", methods=["GET"])
@login_required
def peer_user_search():
    q = (request.args.get("q") or "").strip().lower()
    if not q or len(q) < 2:
        return jsonify({"ok": True, "users": []})
    all_users = User.query.filter_by(is_active=True).all()
    results = []
    for u in all_users:
        if u.id == current_user.id:
            continue
        if "student" not in u.roles:
            continue
        if q in u.display_name.lower() or q in u.email.lower():
            results.append({
                "id": u.id,
                "name": u.display_name,
                "avatar": u.profile_picture,
            })
        if len(results) >= 10:
            break
    return jsonify({"ok": True, "users": results})


@main.after_request
def private_progress_response(response):
    if request.endpoint in {"main.student_progress", "main.weekly_leaderboard_data", "main.leaderboard_page", "main.update_leaderboard_preference"}:
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@main.route("/admin/attendance")
@admin_required
def admin_attendance():
    state = request.args.get("state", "unresolved")
    if state not in ("unresolved", "resolved"):
        return error("Choose unresolved or resolved attendance disputes.")
    query = Appointment.query.filter(Appointment.attendance_disputed_at.is_not(None))
    query = query.filter(Appointment.attendance_resolved_at.is_(None) if state == "unresolved" else Appointment.attendance_resolved_at.is_not(None))
    pagination = query.order_by(Appointment.attendance_disputed_at.desc()).paginate(
        page=request.args.get("page", 1, type=int), per_page=25, error_out=False)
    return render_template("admin/attendance.html", rows=pagination.items, pagination=pagination,
                           state=state, format_dt=format_dt_pht)


@main.route("/api/admin/attendance/<int:booking_id>/resolve", methods=["POST"])
@admin_required
def admin_resolve_attendance(booking_id):
    if __package__:
        from .attendance import AttendanceError, resolve_dispute
    else:
        from attendance import AttendanceError, resolve_dispute
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return error("Provide a valid resolution object.")
    appt = Appointment.query.filter_by(id=booking_id).populate_existing().with_for_update().first()
    if not appt:
        return error("Booking not found.", 404)
    try:
        resolve_dispute(appt, current_user.id, data.get("outcome"), data.get("reason"))
        db.session.commit()
    except AttendanceError as exc:
        db.session.rollback()
        return error(exc.message, exc.status)
    except SQLAlchemyError:
        db.session.rollback()
        return error("Dispute resolution could not be saved. Please retry.", 503)
    return jsonify(ok=True, outcome=appt.attendance_resolution, message="Decision recorded. No XP was awarded by this action.")
