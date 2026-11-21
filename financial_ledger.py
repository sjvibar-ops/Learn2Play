"""Phase 1 journal primitives. Keep external I/O and workflow rules in callers."""

from decimal import Decimal

from flask import current_app
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

if __package__:
    from .extensions import db
    from .models import LedgerAccount, LedgerLine, LedgerTransaction
else:
    from extensions import db
    from models import LedgerAccount, LedgerLine, LedgerTransaction


MAX_CENTAVOS = 2**63 - 1


def allocate_payment(amount: Decimal, currency: str) -> tuple[int, int]:
    """Return (commission, provider) in exact integer centavos."""
    if currency != "PHP":
        raise ValueError("Only PHP is supported")
    if (not isinstance(amount, Decimal) or not amount.is_finite()
            or not Decimal("0.01") <= amount <= Decimal("92233720368547758.07")):
        raise ValueError("Payment amount must be a positive Decimal within the supported range")
    # Inspect fractional precision first so an extreme Decimal exponent cannot
    # force as_integer_ratio() to allocate an enormous denominator.
    _, digits, exponent = amount.as_tuple()
    if exponent < -2 and any(digits[exponent + 2:]):
        raise ValueError("Payment amount must be a whole number of centavos")
    numerator, denominator = amount.as_integer_ratio()
    gross = numerator * 100 // denominator
    commission = (gross + 2) // 5
    return commission, gross - commission


def approval_ready():
    if not current_app.config.get("PAYMENT_LEDGER_APPROVAL_ENABLED"):
        return False
    if db.session.get_bind().dialect.name != "postgresql":
        return False
    if db.session.connection().get_isolation_level() != "READ COMMITTED":
        return False
    return db.session.execute(text("SELECT public.phase2_approval_ready()")).scalar_one()


def approval_complete(payment_id, revision):
    return db.session.execute(text("SELECT public.phase2_allocation_complete(:payment_id, :revision)"), {
        "payment_id": payment_id, "revision": revision,
    }).scalar_one()


def initial_allocation_terms(payment, appointment):
    if (payment.appointment_id != appointment.id or payment.student_id != appointment.student_id
            or appointment.provider_id == appointment.student_id or not appointment.payment_required
            or not payment.receipt_path or not payment.receipt_path.strip() or payment.submitted_at is None
            or not payment.transaction_reference or not 4 <= len(payment.transaction_reference.strip()) <= 120):
        raise ValueError("Payment evidence or booking financial details require review")
    return allocate_payment(payment.amount, appointment.currency)


def _allocation_account(kind, currency, provider_id):
    key = {"kind": kind, "currency": currency, "provider_id": provider_id}
    account = LedgerAccount.query.filter_by(**key).one_or_none()
    if account is None:
        statement = insert(LedgerAccount).values(**key)
        if provider_id is None:
            statement = statement.on_conflict_do_nothing(
                index_elements=["kind", "currency"], index_where=LedgerAccount.provider_id.is_(None),
            )
        else:
            statement = statement.on_conflict_do_nothing(constraint="uq_ledger_provider_account")
        db.session.execute(statement)
        # A conflicting INSERT may have waited for another transaction. Read
        # the winning account in a new READ COMMITTED statement.
        account = LedgerAccount.query.filter_by(**key).one()
    return account.id


def post_initial_allocation(payment, appointment, verification, terms):
    """Post the reviewed payment only, under the caller's Payment → Appointment locks."""
    if (payment.status != "VERIFIED" or verification.event_type != "VERIFIED"
            or verification.payment_id != payment.id or verification.appointment_id != appointment.id
            or verification.revision != payment.submission_revision
            or verification.actor_id != payment.reviewed_by
            or terms != initial_allocation_terms(payment, appointment)):
        raise ValueError("Allocation does not match the payment verification")
    commission, provider = terms
    currency = appointment.currency
    clearing = _allocation_account("payment_clearing", currency, None)
    lines = [(clearing, commission + provider)]
    if commission:
        lines.append((_allocation_account("platform_commission", currency, None), -commission))
    lines.append((_allocation_account("provider_pending", currency, appointment.provider_id), -provider))
    return post_transaction(
        kind="allocation", currency=currency, lines=lines, reason="Initial verified payment allocation",
        payment_id=payment.id, appointment_id=appointment.id, provider_id=appointment.provider_id,
        payment_event_id=verification.id, actor_id=payment.reviewed_by,
    )


def post_transaction(
    *, kind, currency, lines, reason, actor_id=None, system_actor=None,
    payment_id=None, appointment_id=None, provider_id=None, payment_event_id=None,
    admin_audit_id=None, original_transaction_id=None,
):
    """Flush and seal a journal in the caller's transaction.

    Lines are (account_id, signed centavos), in their intended order. Callers
    validate authorization and business rules, acquire domain locks before
    posting, and own commit or rollback. A duplicate source raises an integrity
    error rather than writing another journal.
    """
    if db.session.get_bind().dialect.name != "postgresql":
        raise RuntimeError("Ledger posting requires PostgreSQL")
    if currency != "PHP":
        raise ValueError("Only PHP is supported")
    entries = []
    for ordinal, (account_id, centavos) in enumerate(lines, start=1):
        if type(centavos) is not int or not -MAX_CENTAVOS <= centavos <= MAX_CENTAVOS:
            raise ValueError("Journal values must be integer centavos within the supported range")
        entries.append(LedgerLine(ordinal=ordinal, account_id=account_id, currency=currency, centavos=centavos))
    transaction = LedgerTransaction(
        kind=kind, currency=currency, reason=reason, actor_id=actor_id, system_actor=system_actor,
        payment_id=payment_id, appointment_id=appointment_id, provider_id=provider_id,
        payment_event_id=payment_event_id, admin_audit_id=admin_audit_id,
        original_transaction_id=original_transaction_id,
    )
    db.session.add(transaction)
    db.session.flush()
    for entry in entries:
        entry.transaction_id = transaction.id
    db.session.add_all(entries)
    db.session.flush()
    db.session.execute(
        text("SELECT public.seal_ledger_transaction(:journal_id)"), {"journal_id": transaction.id},
    )
    db.session.refresh(transaction, ["sealed"])
    return transaction
