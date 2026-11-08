"""Checkout creation, webhook inbox and idempotent, convergent fulfillment.

Authority rules (contract 7):
* Only verified *provider state* grants access. A webhook body only names which
  purchase to re-evaluate; the handler retrieves current Checkout state through the
  adapter and decides from that, so duplicate, delayed, conflicting or reordered events
  all converge on the same result.
* The purchase is resolved from the stored Checkout ID (or stored/looked-up payment
  intent for charge events). Provider metadata must agree with the stored row but is
  never the sole binding.
* Purchase state only moves forward: pending < failed < paid < refunded. A refunded
  purchase never reactivates; entitlement rows are kept (revoked) so a source purchase
  can never be granted twice. Another independently paid purchase still grants.
* Provider I/O happens in the handler before publication; the DB publication is a
  single lease-fenced transaction through the queue API.
No payload, key or provider text is logged or stored beyond the minimized event projection.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.stripe import (
    CheckoutRequest, ProviderNotFound, ProviderRejected, ProviderUnavailable,
    ProviderCheckout, StripeGateway, WebhookEvent,
)
from app.contracts.enums import ReportState, SessionState
from app.jobs.queue import Failure, JobSpec, Lease, enqueue
from app.jobs.worker import Publication, StageFailure, StageRegistry
from app.models import Entitlement, Purchase, Report, ReportRevision, Session, StripeEvent
from app.services.accounts import AccountService
from app.services.auth import authenticate
from app.services.idempotency import execute_idempotent
from app.services.problems import ProblemError, conflict, not_found
from app.settings import Settings

PRODUCT = "report_unlock"
CURRENCY = "usd"
BILLING_CONFIG_HASH = hashlib.sha256(b"upclose-billing-v1").hexdigest()
COMPLETION_EVENTS = frozenset({"checkout.session.completed", "checkout.session.async_payment_succeeded",
                               "checkout.session.async_payment_failed"})
REFUND_EVENTS = frozenset({"charge.refunded"})
MAX_ATTEMPTS = 3  # queue contract: initial + two retries
_RANK = {"pending": 0, "failed": 1, "paid": 2, "refunded": 3}
_REUSE_WINDOW = timedelta(hours=23)  # inside Stripe's 24 h idempotency-key retention


def has_active_entitlement(db: DBSession, report_id: UUID) -> bool:
    """Derived access check for the report assembler. There is no stored paid flag.

    Active means an unrevoked report_unlock entitlement whose source purchase is
    currently `paid` (a refunded/failed purchase can never satisfy this).
    """
    return bool(db.scalar(select(exists().where(
        Entitlement.report_id == report_id, Entitlement.kind == PRODUCT, Entitlement.revoked_at.is_(None),
        Entitlement.source_purchase_id == Purchase.id, Purchase.state == "paid"))))


# ------------------------------------------------------------------ checkout

def _lock_owner(db: DBSession, owner_id: UUID) -> Session:
    """Lock an already-authorized inspection; one being deleted is gone (404), as for owner reads."""
    owner = db.scalar(select(Session).where(Session.id == owner_id).with_for_update())
    if owner is None or owner.state in (SessionState.DELETING, SessionState.DELETED):
        raise not_found()
    return owner


class CheckoutService:
    def __init__(self, factory: sessionmaker[DBSession], settings: Settings, gateway: StripeGateway) -> None:
        self.factory = factory
        self.settings = settings
        self.gateway = gateway
        self.accounts = AccountService(factory, settings)

    def create(self, token: str, slug: str, idempotency_key: str | None = None) -> tuple[str | None, bool]:
        owner_id = self._authorized_owner(token, slug)
        if self.settings.demo_free_unlock:
            return None, self._demo_unlock(owner_id, slug, idempotency_key)
        return self._stripe_checkout(owner_id, slug, idempotency_key), False

    def _authorized_owner(self, token: str, slug: str) -> UUID:
        """The inspection this bearer may buy for, by the same rule as reading the report (ReportService):
        the inspection's owner token, or the account the inspection is saved to. A token for another
        inspection or an account the inspection is not saved to is 404; a token valid for neither is 401."""
        with self.factory.begin() as db:
            try:
                return authenticate(db, token)
            except ProblemError:
                pass
        account = self.accounts.verify_token(token)  # 401 TOKEN_INVALID when it is not an account token either
        with self.factory.begin() as db:
            session_id = db.scalar(select(Report.session_id).where(Report.share_slug == slug))
            owner = None if session_id is None else db.get(Session, session_id)
            if owner is None or owner.account_id != account.id:
                raise not_found()
            return owner.id

    def _demo_unlock(self, owner_id: UUID, slug: str, idempotency_key: str | None = None) -> bool:
        # Demo stand-in (DEMO_FREE_UNLOCK, fixture payments only): owner-only, ready-only,
        # recorded as a labeled demo purchase + entitlement. No charge, no provider call.
        if self.settings.payments_mode != "fixture":
            raise ProblemError(503, "INTERNAL", "Checkout unavailable", "Demo unlock requires fixture payments mode.")
        purchase_id, _request, _checkout_id = self._persist_purchase(owner_id, slug, idempotency_key)
        with self.factory.begin() as db:
            owner = _lock_owner(db, owner_id)
            purchase = db.scalar(select(Purchase).where(Purchase.id == purchase_id, Purchase.session_id == owner.id).with_for_update())
            if purchase is None or purchase.state != "pending":
                raise conflict()
            purchase.checkout_id = f"demo-unlock-{purchase.id}"
            purchase.payment_intent_id = f"pi_demo_unlock_{purchase.id}"
            purchase.state = "paid"
            db.execute(insert(Entitlement).values(session_id=purchase.session_id, report_id=purchase.report_id, source_purchase_id=purchase.id, kind=PRODUCT).on_conflict_do_nothing(index_elements=[Entitlement.source_purchase_id, Entitlement.kind]))
        return True

    def _stripe_checkout(self, owner_id: UUID, slug: str, idempotency_key: str | None = None) -> str:
        purchase_id, request, checkout_id = self._persist_purchase(owner_id, slug, idempotency_key)
        # External I/O is outside any DB transaction; the purchase row already exists.
        try:
            if checkout_id is not None:
                current = self.gateway.retrieve_checkout(checkout_id)
                if (current.checkout_id != checkout_id or current.livemode or current.mode != 'payment'
                        or current.client_reference_id != str(purchase_id)
                        or current.metadata.get('purchase_id') != str(purchase_id)
                        or current.metadata.get('report_id') != str(request.report_id)
                        or current.amount_total != request.amount_cents or current.currency != request.currency):
                    raise conflict()
                if current.status != 'open':
                    if current.status == 'expired':
                        with self.factory.begin() as db:
                            _lock_owner(db, owner_id)
                            db.execute(update(Purchase).where(Purchase.id == purchase_id,
                                Purchase.state == 'pending').values(state='failed'))
                    raise conflict()
            created = self.gateway.create_checkout(request)
        except (ProviderUnavailable, ProviderRejected, ProviderNotFound):
            raise ProblemError(503, "INTERNAL", "Checkout unavailable",
                               "Checkout could not be started. Try again shortly.") from None
        with self.factory.begin() as db:
            owner = _lock_owner(db, owner_id)
            bound = db.scalar(update(Purchase).where(
                Purchase.id == purchase_id, Purchase.session_id == owner.id, Purchase.state == "pending", (Purchase.checkout_id.is_(None)) | (Purchase.checkout_id == created.checkout_id),
            ).values(checkout_id=created.checkout_id).returning(Purchase.id))
            if bound is None:
                raise conflict()
        return created.url

    def _persist_purchase(self, owner_id: UUID, slug: str, idempotency_key: str | None) -> tuple[UUID, CheckoutRequest, str | None]:
        cfg = self.settings
        with self.factory.begin() as db:
            owner = _lock_owner(db, owner_id)  # 404 deleting/deleted
            report = db.scalar(select(Report).where(Report.share_slug == slug, Report.session_id == owner.id))
            if report is None:
                raise not_found()
            ready = report.current_revision_id is not None and db.scalar(select(ReportRevision.state).where(
                ReportRevision.id == report.current_revision_id)) == ReportState.READY
            if not ready:
                raise ProblemError(409, "REPORT_NOT_READY", "Report not ready", "The report is not ready to unlock yet.")
            if has_active_entitlement(db, report.id):
                raise conflict()
            now = db.scalar(select(func.clock_timestamp()))
            def select_purchase() -> dict[str, object]:
                # Bound pending sessions are reused too: another click is not another purchase.
                purchase = db.scalar(select(Purchase).where(
                    Purchase.report_id == report.id, Purchase.state == "pending",
                    Purchase.created_at > now - _REUSE_WINDOW).order_by(Purchase.created_at).limit(1).with_for_update())
                if purchase is None:
                    purchase = Purchase(session_id=owner.id, report_id=report.id, expected_amount=cfg.report_price_cents,
                                        currency=CURRENCY, state="pending")
                    db.add(purchase)
                    db.flush()
                return {'purchase_id': str(purchase.id)}
            result = (select_purchase() if idempotency_key is None else execute_idempotent(db,
                authority_id=owner.id, route=f"/reports/{slug}/checkout", key=idempotency_key,
                body={'product': PRODUCT}, operation=select_purchase))
            purchase = db.get(Purchase, UUID(str(result['purchase_id'])))
            # The durable mapping never stores a provider URL. Do not reuse a provider
            # idempotency key beyond its safe window or restart a terminal purchase.
            if (purchase is None or purchase.session_id != owner.id or purchase.report_id != report.id
                    or purchase.state != 'pending' or purchase.created_at <= now - _REUSE_WINDOW):
                raise conflict()
            base = cfg.web_origin.rstrip("/")
            # No identifiers in return URLs: navigation grants nothing, and slugs stay out of Stripe.
            request = CheckoutRequest(
                purchase_id=purchase.id, report_id=report.id, amount_cents=purchase.expected_amount,
                currency=purchase.currency, success_url=f"{base}/checkout/return?result=success",
                cancel_url=f"{base}/checkout/return?result=cancel",
                price_id=cfg.stripe_report_price_id)
            return purchase.id, request, purchase.checkout_id


# ------------------------------------------------------------------ webhook inbox

def record_event(factory: sessionmaker[DBSession], event: WebhookEvent) -> bool:
    """Insert inbox row and its global billing job in ONE transaction.

    Returns True when the event row was new. Redelivery hits the primary key and the
    job idempotency key, so no second job exists. Any database error propagates (5xx).
    """
    spec = JobSpec(None, "stripe_event", event.event_id, "billing_event",
                   hashlib.sha256(event.event_id.encode()).hexdigest(), BILLING_CONFIG_HASH)
    with factory.begin() as db:
        inserted = db.scalar(insert(StripeEvent).values(
            event_id=event.event_id, event_type=event.event_type, payload=event.payload,
        ).on_conflict_do_nothing(index_elements=[StripeEvent.event_id]).returning(StripeEvent.event_id))
        enqueue(db, spec)  # idempotent by key; also repairs a missing job for an existing row
        return inserted is not None


# ------------------------------------------------------------------ decision (pure)

@dataclass(frozen=True, slots=True)
class PurchaseView:
    id: UUID
    report_id: UUID
    state: str
    checkout_id: str | None
    payment_intent_id: str | None
    expected_amount: int
    currency: str


@dataclass(frozen=True, slots=True)
class Decision:
    state: str
    payment_intent_id: str | None = None
    error: str | None = None  # stable code; the event is marked failed and nothing changes

    @property
    def grant(self) -> bool:
        return self.state == "paid" and self.error is None

    @property
    def revoke(self) -> bool:
        return self.state == "refunded" and self.error is None


def decide(purchase: PurchaseView, provider: ProviderCheckout, event_type: str) -> Decision:
    def reject(code: str) -> Decision:
        return Decision(purchase.state, None, code)

    if (provider.checkout_id != purchase.checkout_id or provider.client_reference_id != str(purchase.id)
            or provider.metadata.get("purchase_id") != str(purchase.id)
            or provider.metadata.get("report_id") != str(purchase.report_id)):
        return reject("BINDING_MISMATCH")
    if (provider.payment_intent_id and purchase.payment_intent_id
            and provider.payment_intent_id != purchase.payment_intent_id):
        return reject("PAYMENT_INTENT_MISMATCH")
    if purchase.state == "refunded":
        return Decision("refunded", purchase.payment_intent_id)  # terminal; old events cannot reactivate
    if provider.livemode:
        return reject("LIVEMODE_REJECTED")
    if provider.mode != "payment":
        return reject("MODE_REJECTED")
    if provider.status == "complete" and provider.payment_status == "paid":
        if provider.amount_total != purchase.expected_amount or provider.currency != purchase.currency:
            return reject("AMOUNT_MISMATCH")
        if not provider.payment_intent_id:
            return reject("PAYMENT_INTENT_MISSING")
        if provider.amount_refunded > 0:  # any positive refund revokes (design default)
            return Decision("refunded", provider.payment_intent_id)
        return Decision("paid", provider.payment_intent_id)
    if purchase.state == "paid":
        return Decision("paid", purchase.payment_intent_id)  # unpaid view cannot demote a verified payment
    if event_type == "checkout.session.async_payment_failed" and provider.status == "complete":
        return Decision("failed")
    return Decision(purchase.state)


# ------------------------------------------------------------------ handler

def _view(purchase: Purchase) -> PurchaseView:
    return PurchaseView(purchase.id, purchase.report_id, purchase.state, purchase.checkout_id,
                        purchase.payment_intent_id, purchase.expected_amount, purchase.currency)


def _finish(event_id: str, state: str, error: str | None):
    """Publisher body: mark the inbox row inside the fenced transaction."""
    def apply(db: DBSession) -> None:
        db.execute(update(StripeEvent).where(StripeEvent.event_id == event_id).values(
            processing_state=state, processed_at=func.now(), error=error))
    return apply


def _refs(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    obj = payload.get("data", {}).get("object", {})
    intent = obj.get("payment_intent") if isinstance(obj.get("payment_intent"), str) else None
    checkout = obj.get("id") if obj.get("object") == "checkout.session" and isinstance(obj.get("id"), str) else None
    return checkout, intent


class BillingHandler:
    def __init__(self, factory: sessionmaker[DBSession], gateway: StripeGateway) -> None:
        self.factory = factory
        self.gateway = gateway

    def __call__(self, lease: Lease) -> Publication:
        with self.factory() as db:
            event = db.get(StripeEvent, lease.subject_id)
            if event is None:
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            event_id, event_type, payload, state = event.event_id, event.event_type, event.payload, event.processing_state
        if state in ("processed", "ignored", "failed"):
            return Publication()  # re-run after a lost lease: effects already committed
        if event_type not in COMPLETION_EVENTS | REFUND_EVENTS:
            return Publication(_finish(event_id, "ignored", None))
        if payload.get("livemode") is not False:
            return Publication(_finish(event_id, "failed", "LIVEMODE_REJECTED"))
        obj_kind = payload.get("data", {}).get("object", {}).get("object")
        if (event_type in COMPLETION_EVENTS and obj_kind != "checkout.session") or (
                event_type in REFUND_EVENTS and obj_kind != "charge"):
            return Publication(_finish(event_id, "failed", "UNEXPECTED_OBJECT"))
        checkout_id, intent = _refs(payload)
        purchase_id = self._resolve(checkout_id, intent)
        if purchase_id is None:
            if lease.attempt < MAX_ATTEMPTS:  # may race the checkout-binding commit; retry visibly
                raise StageFailure(Failure("TRANSIENT"))
            return Publication(_finish(event_id, "failed", "UNMATCHED_PURCHASE"))
        stored_checkout = self._checkout_of(purchase_id)
        try:
            provider = self.gateway.retrieve_checkout(stored_checkout)
        except ProviderUnavailable:
            raise StageFailure(Failure("TRANSIENT")) from None
        except ProviderNotFound:
            return Publication(_finish(event_id, "failed", "PROVIDER_NOT_FOUND"))
        except ProviderRejected:
            raise StageFailure(Failure("UNAUTHORIZED", retryable=False)) from None

        def publish(db: DBSession) -> None:
            purchase = db.scalar(select(Purchase).where(Purchase.id == purchase_id).with_for_update())
            if purchase is None:
                raise ValueError("purchase vanished")
            decision = decide(_view(purchase), provider, event_type)
            if decision.error is None and decision.payment_intent_id and decision.payment_intent_id != purchase.payment_intent_id:
                clash = db.scalar(select(Purchase.id).where(
                    Purchase.payment_intent_id == decision.payment_intent_id, Purchase.id != purchase.id))
                if clash is not None:
                    decision = Decision(purchase.state, None, "PAYMENT_INTENT_CONFLICT")
            if decision.error is not None:
                _finish(event_id, "failed", decision.error)(db)
                return
            new_state = decision.state if _RANK[decision.state] >= _RANK[purchase.state] else purchase.state
            if decision.payment_intent_id and purchase.payment_intent_id is None and new_state in ("paid", "refunded"):
                purchase.payment_intent_id = decision.payment_intent_id
            purchase.state = new_state
            if new_state == "paid":
                db.execute(insert(Entitlement).values(
                    session_id=purchase.session_id, report_id=purchase.report_id,
                    source_purchase_id=purchase.id, kind=PRODUCT,
                ).on_conflict_do_nothing(index_elements=[Entitlement.source_purchase_id, Entitlement.kind]))
            elif new_state == "refunded":
                db.execute(update(Entitlement).where(
                    Entitlement.source_purchase_id == purchase.id, Entitlement.kind == PRODUCT,
                    Entitlement.revoked_at.is_(None)).values(revoked_at=func.now()))
            _finish(event_id, "processed", None)(db)

        return Publication(publish)

    def _resolve(self, checkout_id: str | None, intent: str | None) -> UUID | None:
        with self.factory() as db:
            if checkout_id is not None:
                return db.scalar(select(Purchase.id).where(Purchase.checkout_id == checkout_id))
            if intent is None:
                return None
            found = db.scalar(select(Purchase.id).where(Purchase.payment_intent_id == intent))
        if found is not None:
            return found
        try:  # refund (or other charge event) before completion: ask the provider which Checkout owns it
            owner_checkout = self.gateway.checkout_id_for_payment_intent(intent)
        except ProviderUnavailable:
            raise StageFailure(Failure("TRANSIENT")) from None
        except (ProviderNotFound, ProviderRejected):
            return None
        if owner_checkout is None:
            return None
        with self.factory() as db:
            return db.scalar(select(Purchase.id).where(Purchase.checkout_id == owner_checkout))

    def _checkout_of(self, purchase_id: UUID) -> str:
        with self.factory() as db:
            checkout = db.scalar(select(Purchase.checkout_id).where(Purchase.id == purchase_id))
        if checkout is None:
            raise StageFailure(Failure("INVALID_INPUT", retryable=False))
        return checkout


def register_billing_handlers(registry: StageRegistry, factory: sessionmaker[DBSession],
                              gateway: StripeGateway) -> None:
    """Install the `billing_event` stage. Root wires this in the worker entry point."""
    registry.register("billing_event", BillingHandler(factory, gateway))
