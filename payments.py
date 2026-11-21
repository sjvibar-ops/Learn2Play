"""Manual payment workflow. Storage credentials never leave the backend."""
import os
import uuid
import warnings
from io import BytesIO
from pathlib import Path
from decimal import Decimal

from flask import Blueprint, current_app, request, render_template, jsonify, send_file, url_for
from flask_login import current_user, login_required
from supabase import create_client
from PIL import Image, UnidentifiedImageError

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

if __package__:
    from .admin_audit import AuditAction, AuditReason, AuditTarget, record_admin_event
    from .financial_ledger import approval_complete, approval_ready, initial_allocation_terms, post_initial_allocation
    from .reconciliation import TRANSITIONS, STATES, ReconciliationError, decide
    from .extensions import db
    from .models import Appointment, Payment, PaymentEvent, utcnow
    from .routes import admin_required, error, format_dt_pht
else:
    from admin_audit import AuditAction, AuditReason, AuditTarget, record_admin_event
    from financial_ledger import approval_complete, approval_ready, initial_allocation_terms, post_initial_allocation
    from reconciliation import TRANSITIONS, STATES, ReconciliationError, decide
    from extensions import db
    from models import Appointment, Payment, PaymentEvent, utcnow
    from routes import admin_required, error, format_dt_pht

payments = Blueprint("payments", __name__)
MAX_RECEIPT_BYTES = 5 * 1024 * 1024
METHODS = ("GCash", "Maya", "Bank transfer")
ACTIVE = ("pending", "accepted", "scheduled")


MAX_RECEIPT_DIMENSION = 8192
MAX_RECEIPT_PIXELS = 16_000_000


def valid_image(content, png):
    """Check container integrity and fully decode a bounded, static PNG/JPEG.

    Pillow's truncated-image recovery remains disabled (its default). Restricting
    plugins avoids invoking unrelated image decoders on untrusted receipt bytes.
    """
    expected = "PNG" if png else "JPEG"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content), formats=[expected]) as image:
                width, height = image.size
                if (not 0 < width <= MAX_RECEIPT_DIMENSION
                        or not 0 < height <= MAX_RECEIPT_DIMENSION
                        or width * height > MAX_RECEIPT_PIXELS
                        or getattr(image, "n_frames", 1) != 1):
                    return False
                image.verify()
            # verify() checks structure, not pixel decompression. Reopen and load
            # the entire image so invalid IDAT/JPEG scan data cannot pass.
            with Image.open(BytesIO(content), formats=[expected]) as image:
                image.load()
        return True
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        return False


def new_payment(appointment):
    return Payment(appointment_id=appointment.id, student_id=appointment.student_id,
                   amount=Decimal(appointment.price).quantize(Decimal("0.01")),
                   reference="L2P-" + uuid.uuid4().hex.upper())


def receipts_bucket():
    # Fail closed if an operator accidentally configures a public bucket.
    client = create_client(os.environ.get("SUPABASE_URL", ""),
                           os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""))
    name = current_app.config["PAYMENT_RECEIPTS_BUCKET"]
    metadata = client.storage.get_bucket(name)
    public = metadata.get("public") if isinstance(metadata, dict) else getattr(metadata, "public", None)
    if public is not False:
        raise RuntimeError("Receipt storage must be private")
    return client.storage.from_(name)


def discard(path):
    try:
        receipts_bucket().remove([path])
    except Exception:
        current_app.logger.warning("Payment receipt cleanup failed; reconciliation required.")


def owned_payment(booking_id, lock=False):
    query = Payment.query.filter_by(appointment_id=booking_id, student_id=current_user.id)
    if lock:
        query = query.with_for_update()
    return query.first()


def transfer_claims(payment, blocking_only=False):
    """Compare using the same database expressions as the unique index.

    Submissions never reserve a transfer. Pending/rejected rows do not block approval;
    their current reference still appears in admin warnings for investigation.
    """
    if not payment.method or not payment.transaction_reference:
        return []
    query = Payment.query.filter(
        Payment.id != payment.id,
        func.lower(func.trim(Payment.method)) == func.lower(func.trim(payment.method)),
        func.lower(func.trim(Payment.transaction_reference)) == func.lower(func.trim(payment.transaction_reference)),
    )
    if blocking_only:
        query = query.filter(Payment.status.in_(("SUBMITTED", "VERIFIED")))
    return query.order_by(Payment.id).all()


def verified_transfer_conflict(exc):
    """Identify only our unique index violation, without exposing driver errors."""
    diagnostic = getattr(exc.orig, "diag", None)
    return (getattr(diagnostic, "constraint_name", None) == "uq_payments_verified_transfer"
            or "uq_payments_verified_transfer" in str(exc.orig))


def record_event(payment, event_type, previous_status, actor_id, **audit_fields):
    event = PaymentEvent(
        payment_id=payment.id, appointment_id=payment.appointment_id,
        student_id=payment.student_id, actor_id=actor_id,
        reviewer_id=payment.reviewed_by, event_type=event_type,
        previous_status=previous_status, new_status=payment.status,
        revision=payment.submission_revision, receipt_path=payment.receipt_path,
        method=payment.method, transaction_reference=payment.transaction_reference,
        rejection_reason=payment.rejection_reason, submitted_at=payment.submitted_at,
        reviewed_at=payment.reviewed_at, created_at=utcnow(), **audit_fields,
    )
    db.session.add(event)
    return event


def preserve_legacy_snapshot(payment, actor_id):
    # No guessed past events: record only the facts still present, observed now.
    if payment.submission_revision == 0 and (
            payment.receipt_path or payment.submitted_at or payment.reviewed_at
            or payment.status != "PENDING"):
        if not PaymentEvent.query.filter_by(payment_id=payment.id, event_type="LEGACY_SNAPSHOT").first():
            record_event(payment, "LEGACY_SNAPSHOT", payment.status, actor_id)
            # Flush before changing the projection; snapshot values are scalars.
            db.session.flush()


def event_details(event):
    result = dict(id=event.id, event_type=event.event_type, revision=event.revision,
                  previous_status=event.previous_status, new_status=event.new_status,
                  created_at=event.created_at.isoformat(),
                  submitted_at=event.submitted_at.isoformat() if event.submitted_at else None,
                  reviewed_at=event.reviewed_at.isoformat() if event.reviewed_at else None,
                  rejection_reason=event.rejection_reason,
                  receipt_url=url_for("payments.historical_receipt", payment_id=event.payment_id, event_id=event.id) if event.receipt_path else None)
    if event.reconciliation_state is not None:
        result.update(reconciliation_previous_state=event.reconciliation_previous_state,
                      reconciliation_state=event.reconciliation_state,
                      reconciliation_version=event.reconciliation_version,
                      reconciliation_explanation=event.reconciliation_explanation,
                      booking_status=event.booking_status,
                      booking_ended_at=event.booking_ended_at.isoformat() if event.booking_ended_at else None,
                      booking_ending_reason=event.booking_ending_reason)
    if current_user.is_admin:
        result.update(reviewer_id=event.reviewer_id,
                      reviewer=event.reviewer.display_name if event.reviewer else None,
                      actor_id=event.actor_id, actor_name=event.actor.display_name if event.actor else None,
                      decision_reason=event.decision_reason, confirmation_notes=event.confirmation_notes,
                      evidence_reference=event.evidence_reference, authority_name=event.authority_name,
                      authority_capacity=event.authority_capacity,
                      funds_received_confirmed=event.funds_received_confirmed,
                      independent_check_confirmed=event.independent_check_confirmed,
                      authorized_refund_confirmed=event.authorized_refund_confirmed,
                      account_holder_refund_confirmed=event.account_holder_refund_confirmed,
                      authorized_retention_confirmed=event.authorized_retention_confirmed,
                      method=event.method,
                      transaction_reference=event.transaction_reference)
    return result


def details(payment):
    result = dict(id=payment.id, booking_id=payment.appointment_id, reference=payment.reference,
                amount=format(payment.amount, ".2f"), currency=payment.appointment.currency,
                status=payment.status, method=payment.method,
                transaction_reference=payment.transaction_reference,
                submitted_at=payment.submitted_at.isoformat() if payment.submitted_at else None,
                reviewed_at=payment.reviewed_at.isoformat() if payment.reviewed_at else None,
                rejection_reason=payment.rejection_reason,
                submission_revision=payment.submission_revision,
                reconciliation_state=payment.reconciliation_state,
                reconciliation_version=payment.reconciliation_version,
                reconciliation_explanation=payment.reconciliation_explanation,
                booking_ended_at=payment.appointment.ended_at.isoformat() if payment.appointment.ended_at else None,
                booking_ending_reason=payment.appointment.ending_reason)
    if current_user.is_admin:
        result["reviewed_by"] = payment.reviewer.display_name if payment.reviewer else None
    return result


@payments.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@payments.route("/payments")
@login_required
def history():
    rows = Payment.query.filter_by(student_id=current_user.id).order_by(Payment.created_at.desc()).all()
    return render_template("payments/history.html", rows=rows, format_dt=format_dt_pht)


@payments.route("/payments/bookings/<int:booking_id>")
@login_required
def student_payment(booking_id):
    payment = owned_payment(booking_id)
    if payment is None:
        return error("Payment not found.", 404)
    return render_template("payments/detail.html", payment=payment, methods=METHODS,
                           format_dt=format_dt_pht, active=ACTIVE)


@payments.route("/api/payments/bookings/<int:booking_id>")
@login_required
def payment_info(booking_id):
    payment = owned_payment(booking_id)
    if not payment:
        return error("Payment not found.", 404)
    return jsonify(ok=True, payment=details(payment), history=[event_details(event) for event in payment.events])


@payments.route("/api/payments/bookings/<int:booking_id>/submit", methods=["POST"])
@login_required
def submit(booking_id):
    student_id = current_user.id
    payment = owned_payment(booking_id)
    if not payment:
        return error("Payment not found.", 404)
    appointment = Appointment.query.filter_by(id=booking_id).one()
    if payment.status not in ("PENDING", "REJECTED") or appointment.status not in ACTIVE:
        return error("This payment cannot be submitted in its current state.", 409)
    transaction = request.form.get("transaction_reference", "").strip()
    method = request.form.get("method", "")
    if len(transaction) < 4 or len(transaction) > 120 or any(ord(c) < 32 for c in transaction):
        return error("Enter a transaction reference of 4–120 characters.")
    if method not in METHODS:
        return error("Choose a supported payment method.")
    upload = request.files.get("receipt")
    extension = Path(upload.filename or "").suffix.lower() if upload else ""
    if extension not in (".png", ".jpg", ".jpeg"):
        return error("Upload a PNG or JPEG receipt.")
    content = upload.read(MAX_RECEIPT_BYTES + 1)
    if len(content) > MAX_RECEIPT_BYTES:
        return error("Receipt must be no larger than 5 MB.", 413)
    png = extension == ".png"
    if upload.mimetype != ("image/png" if png else "image/jpeg") or not valid_image(content, png):
        return error("Receipt content does not match its image format.")
    path = f"{payment.student_id}/{payment.id}/{uuid.uuid4().hex}{extension}"
    snapshot = (payment.status, payment.receipt_path, payment.updated_at, payment.submission_revision)
    payment_id = payment.id
    # Do not keep a connection, transaction or row lock during network I/O.
    # Capture scalar values before rollback expires ORM objects.
    db.session.rollback()
    try:
        receipts_bucket().upload(path, content, {"content-type": "image/png" if png else "image/jpeg", "upsert": "false"})
    except Exception:
        db.session.rollback()
        return error("Receipt storage is unavailable. Please retry.", 503)
    commit_started = False
    try:
        payment = Payment.query.filter_by(id=payment_id, appointment_id=booking_id,
                                          student_id=student_id).populate_existing().with_for_update().first()
        appointment = Appointment.query.filter_by(id=booking_id).populate_existing().with_for_update().first()
        if (payment is None or appointment is None or appointment.status not in ACTIVE
                or payment.status not in ("PENDING", "REJECTED")
                or (payment.status, payment.receipt_path, payment.updated_at, payment.submission_revision) != snapshot):
            db.session.rollback()
            discard(path)
            return error("This payment changed during upload. Reload before trying again.", 409)
        preserve_legacy_snapshot(payment, student_id)
        previous_status = payment.status
        payment.submission_revision += 1
        payment.receipt_path = path
        payment.transaction_reference = transaction
        payment.method = method
        payment.status = "SUBMITTED"
        payment.submitted_at = utcnow()
        payment.reviewed_by = None
        payment.reviewed_at = None
        payment.rejection_reason = None
        record_event(payment, "RESUBMITTED" if previous_status == "REJECTED" else "SUBMITTED", previous_status, student_id)
        db.session.flush()
        commit_started = True
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        # A lost commit acknowledgement can leave both the payment and its
        # history pointing at this receipt. Keep it if the outcome is unknown.
        if commit_started:
            current_app.logger.warning("Receipt cleanup deferred for payment %s; commit outcome is uncertain.", payment_id)
            return error("Payment submission could not be confirmed. Reload to check its status before retrying.", 503)
        discard(path)
        return error("Payment could not be saved. Please retry.", 503)
    # Committed historical receipts are retained; only uncommitted uploads are discarded.
    return jsonify(ok=True, message="Submitted for manual review. Your payment is not yet verified.")


@payments.route("/payments/<int:payment_id>/receipt")
@login_required
def receipt(payment_id):
    payment = db.session.get(Payment, payment_id)
    if not payment or (not current_user.is_admin and payment.student_id != current_user.id) or not payment.receipt_path:
        return error("Receipt not found.", 404)
    path = payment.receipt_path
    db.session.rollback()
    try:
        content = receipts_bucket().download(path)
    except Exception:
        return error("Receipt is temporarily unavailable.", 503)
    return send_file(BytesIO(content), mimetype="image/png" if path.endswith(".png") else "image/jpeg",
                     download_name="receipt" + Path(path).suffix, as_attachment=True, max_age=0)


@payments.route("/api/admin/payments/<int:payment_id>/history")
@admin_required
def admin_history(payment_id):
    payment = db.session.get(Payment, payment_id)
    if payment is None:
        return error("Payment not found.", 404)
    return jsonify(ok=True, payment=details(payment), history=[event_details(event) for event in payment.events])


@payments.route("/payments/<int:payment_id>/events/<int:event_id>/receipt")
@login_required
def historical_receipt(payment_id, event_id):
    event = PaymentEvent.query.filter_by(id=event_id, payment_id=payment_id).first()
    if (event is None or not event.receipt_path
            or (not current_user.is_admin and (
                event.student_id != current_user.id or event.payment.student_id != current_user.id))):
        return error("Receipt not found.", 404)
    path = event.receipt_path
    db.session.rollback()
    try:
        content = receipts_bucket().download(path)
    except Exception:
        return error("Receipt is temporarily unavailable.", 503)
    return send_file(BytesIO(content), mimetype="image/png" if path.endswith(".png") else "image/jpeg",
                     download_name="receipt" + Path(path).suffix, as_attachment=True, max_age=0)


@payments.route("/admin/payments")
@admin_required
def admin_payments():
    status = request.args.get("status", "SUBMITTED")
    if status not in ("PENDING", "SUBMITTED", "VERIFIED", "REJECTED", "ALL"):
        return error("Invalid payment status.")
    query = Payment.query
    selected_id = request.args.get("payment_id", type=int)
    if "payment_id" in request.args and selected_id is None:
        return error("Invalid payment identifier.")
    if selected_id is not None:
        query = query.filter_by(id=selected_id)
    if status != "ALL":
        query = query.filter_by(status=status)
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Payment.updated_at.desc()).paginate(page=page, per_page=30, error_out=False)
    duplicate_claims = {payment.id: transfer_claims(payment) for payment in pagination.items}
    blocked_ids = {payment.id for payment in pagination.items
                   if any(claim.status in ("SUBMITTED", "VERIFIED") for claim in duplicate_claims[payment.id])}
    return render_template("payments/admin.html", rows=pagination.items, pagination=pagination,
                           status=status, format_dt=format_dt_pht,
                           duplicate_claims=duplicate_claims, blocked_ids=blocked_ids, recon_transitions=TRANSITIONS)


@payments.route("/api/admin/payments/<int:payment_id>/<action>", methods=["POST"])
@admin_required
def review(payment_id, action):
    if action not in ("approve", "reject"):
        return error("Unsupported payment action.")
    payment = Payment.query.filter_by(id=payment_id).populate_existing().with_for_update().first()
    if not payment:
        return error("Payment not found.", 404)
    appointment = Appointment.query.filter_by(id=payment.appointment_id).populate_existing().with_for_update().one()
    if (payment.status != "SUBMITTED" or appointment.status not in ACTIVE) and not (
        action == "approve" and payment.status == "VERIFIED"
    ):
        return error("Only submitted payments for active bookings can be reviewed.", 409)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return error("Provide a valid review object.")
    if action == "approve" and data.get("confirmed") is not True:
        return error("Confirm independent verification of the receiving account.")
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        return error("Enter a rejection reason.")
    reason = reason.strip()
    if action == "reject" and not 5 <= len(reason) <= 500:
        return error("Enter a meaningful rejection reason of 5–500 characters.")
    revision = data.get("expected_revision")
    if action == "approve":
        if type(revision) is not int or not 0 <= revision <= 2**31 - 1:
            return error("Provide the receipt revision you reviewed.")
        if revision != payment.submission_revision:
            return error("The receipt revision changed. Reload and review the current proof.", 409)
    try:
        if action == "approve" and not approval_ready():
            return error("Financial payment approvals are currently unavailable.", 503)
        if payment.status == "VERIFIED":
            if approval_complete(payment_id, revision):
                return jsonify(ok=True, already_approved=True, payment=details(payment))
            return error("This payment was already verified and requires review before any financial change.", 409)
        if action == "approve" and PaymentEvent.query.filter_by(
            payment_id=payment_id, new_status="VERIFIED",
        ).first() is not None:
            return error("Earlier verification history requires manual review.", 409)
        if action == "approve":
            if payment.method not in METHODS or not payment.transaction_reference or not payment.transaction_reference.strip():
                return error("Valid payment method and transaction reference are required before verification.", 409)
            if transfer_claims(payment, blocking_only=True):
                return error("Conflicting transfer claim: another submitted or verified payment uses this method and transaction reference. Review all matching claims and reject any incorrect claim before approving. An already verified transfer cannot be allocated again.", 409)
            terms = initial_allocation_terms(payment, appointment)
        preserve_legacy_snapshot(payment, current_user.id)
        previous_status = payment.status
        payment.status = "VERIFIED" if action == "approve" else "REJECTED"
        payment.reviewed_by = current_user.id
        payment.reviewed_at = utcnow()
        payment.rejection_reason = reason if action == "reject" else None
        verification = record_event(payment, payment.status, previous_status, current_user.id)
        if action == "approve":
            db.session.flush()
            record_admin_event(
                actor=current_user, action=AuditAction.PAYMENT_APPROVED, target_type=AuditTarget.PAYMENT,
                target_id=payment_id, reason=AuditReason.RECEIVING_ACCOUNT_CHECKED,
                before={"payment_status": previous_status},
                after={"payment_status": "VERIFIED", "currency": appointment.currency,
                       "amount_centavos": sum(terms)},
                verification_event_id=verification.id,
            )
            db.session.flush()
            post_initial_allocation(payment, appointment, verification, terms)
        db.session.commit()
    except ValueError:
        db.session.rollback()
        return error("Payment evidence or financial details require manual review.", 409)
    except IntegrityError as exc:
        db.session.rollback()
        if action == "approve" and verified_transfer_conflict(exc):
            return error("This transfer was already verified for another payment. Refresh the queue and reconcile the conflicting claims.", 409)
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if action == "approve" and constraint in {
            "uq_ledger_initial_allocation", "uq_ledger_payment_event_kind", "uq_admin_audit_verification_event",
        }:
            try:
                if approval_complete(payment_id, revision):
                    saved = db.session.get(Payment, payment_id)
                    return jsonify(ok=True, already_approved=True, payment=details(saved))
            except SQLAlchemyError:
                db.session.rollback()
                return error("Review could not be saved. Please retry.", 503)
            return error("Existing financial records require manual review.", 409)
        return error("Review could not be saved. Please retry.", 503)
    except SQLAlchemyError:
        db.session.rollback()
        return error("Review could not be saved. Please retry.", 503)
    return jsonify(ok=True, payment=details(payment))


@payments.route("/admin/payments/reconciliation")
@admin_required
def reconciliation_queue():
    state = request.args.get("state", "NEEDS_REVIEW")
    if state not in (*STATES, "ALL"):
        return error("Choose a valid reconciliation state.")
    query = Payment.query.join(Appointment).filter(Appointment.status.in_(("cancelled", "rejected")))
    if state != "ALL":
        query = query.filter(Payment.reconciliation_state == state)
    pagination = query.order_by(Payment.updated_at.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=30, error_out=False)
    duplicate_claims = {payment.id: transfer_claims(payment) for payment in pagination.items}
    blocked_ids = {payment.id for payment in pagination.items if any(claim.status in ("SUBMITTED", "VERIFIED") for claim in duplicate_claims[payment.id])}
    return render_template("payments/admin.html", rows=pagination.items, pagination=pagination,
                           status="ALL", format_dt=format_dt_pht, duplicate_claims=duplicate_claims,
                           blocked_ids=blocked_ids, recon_transitions=TRANSITIONS,
                           reconciliation_mode=True, recon_state=state, recon_states=STATES)


@payments.route("/api/admin/payments/<int:payment_id>/reconcile", methods=["POST"])
@admin_required
def reconcile(payment_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error("Provide a valid reconciliation object.")
    try:
        payment = Payment.query.filter_by(id=payment_id).populate_existing().with_for_update().first()
        if payment is None:
            return error("Payment not found.", 404)
        appointment = Appointment.query.filter_by(id=payment.appointment_id).populate_existing().with_for_update().one()
        decide(payment, appointment, current_user.id, data)
        db.session.commit()
    except ReconciliationError as exc:
        db.session.rollback()
        return error(exc.message, exc.status)
    except SQLAlchemyError:
        db.session.rollback()
        return error("Reconciliation could not be saved. Please retry.", 503)
    return jsonify(ok=True, payment=details(payment), message="External confirmation recorded. The application did not process a refund or move money.")
