"""T09b-billing: checkout, signed webhook inbox and convergent fulfillment on PostgreSQL.

Fixture mode only: the gateway is the in-process synthetic `FixtureStripe`, webhook
bodies are synthetic and signed with a test secret. No Stripe network call, key or real
payment is involved; passing here is not a Stripe test-mode receipt.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.adapters.stripe import (
    FixtureStripe, ProviderUnavailable, StripeTestModeGateway, build_gateway, sign_payload,
)
from app.contracts.enums import ReportState, SessionState
from app.jobs.queue import JobQueue
from app.jobs.worker import Publication, StageRegistry, run_once
from app.main import create_app
from app.models import Entitlement, Job, Purchase, Report, ReportRevision, Session, StripeEvent
from app.services.auth import token_hash
from app.services.billing import has_active_entitlement, register_billing_handlers
from app.settings import Settings

WEBHOOK_SECRET = "whsec_synthetic_test_secret"
PRICE = 1900


@pytest.fixture(scope="module")
def factory():
    url = os.environ.get("BILLING_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires isolated BILLING_TEST_DATABASE_URL or MODEL_TEST_DATABASE_URL")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing billing checks outside an explicitly named upclose_test_* PostgreSQL database")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True,
                   env={**os.environ, "DATABASE_URL": url})
    engine = create_engine(url, pool_size=8, max_overflow=8, connect_args={"connect_timeout": 3})
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def isolate(factory):
    with factory.begin() as db:  # earlier tests' billing jobs must not be claimed here
        db.execute(update(Job).values(state="dead", lease_until=None, lease_token=None, worker_id=None))


@dataclass
class Env:
    factory: sessionmaker
    gateway: FixtureStripe
    client: TestClient
    token: str
    slug: str
    report_id: UUID
    session_id: UUID

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.token}


def make_env(factory, *, ready: bool = True, gateway: FixtureStripe | None = None) -> Env:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with factory.begin() as db:
        owner = Session(token_hash=token_hash(token), consent_version="synthetic-test", consent_at=now,
                        state=SessionState.FINALIZED, finalized_at=now)
        db.add(owner)
        db.flush()
        report = Report(session_id=owner.id, share_slug=secrets.token_urlsafe(24))
        db.add(report)
        db.flush()
        revision = ReportRevision(session_id=owner.id, report_id=report.id, revision_no=1)
        if ready:
            revision.state = ReportState.READY
            revision.manifest = {"synthetic": True}
            revision.manifest_hash = "a" * 64
            revision.score_result = {}
            revision.rendered_at = now
        db.add(revision)
        db.flush()
        report.current_revision_id = revision.id
        env_ids = (owner.id, report.id, report.share_slug)
    gateway = gateway or FixtureStripe()
    app = create_app(Settings(stripe_webhook_secret=SecretStr(WEBHOOK_SECRET)),
                     database_probe=lambda _: True, session_factory=factory, payments=gateway)
    client = TestClient(app, client=(f"198.19.{uuid4().int % 256}.{uuid4().int % 254 + 1}", 12345))
    return Env(factory, gateway, client, token, env_ids[2], env_ids[1], env_ids[0])


def start_checkout(env: Env) -> tuple[UUID, str]:
    response = env.client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"},
                               headers=env.auth)
    assert response.status_code == 201, response.text
    assert response.json()["checkout_url"].startswith("https://checkout.fixture.invalid/synthetic/")
    with env.factory() as db:
        purchase = db.scalar(select(Purchase).where(Purchase.report_id == env.report_id,
                                                    Purchase.checkout_id.is_not(None)).order_by(Purchase.created_at.desc()))
        return purchase.id, purchase.checkout_id


def session_object(checkout_id: str, env: Env, *, intent: str | None = None, payment_status: str = "paid") -> dict:
    state = env.gateway.retrieve_checkout(checkout_id)
    return {"object": "checkout.session", "id": checkout_id, "payment_intent": intent or state.payment_intent_id,
            "payment_status": payment_status, "status": "complete", "mode": "payment", "amount_total": PRICE,
            "currency": "usd", "livemode": False, "metadata": dict(state.metadata)}


def charge_object(intent: str, amount_refunded: int = PRICE) -> dict:
    return {"object": "charge", "id": "ch_test_synthetic", "payment_intent": intent, "amount": PRICE,
            "amount_refunded": amount_refunded, "currency": "usd", "refunded": True, "livemode": False}


def send(env: Env, event_type: str, obj: dict, *, event_id: str | None = None, livemode: bool = False,
         secret: str = WEBHOOK_SECRET, timestamp: int | None = None):
    event_id = event_id or "evt_synthetic_" + uuid4().hex
    body = json.dumps({"id": event_id, "object": "event", "type": event_type, "livemode": livemode,
                       "created": int(time.time()), "data": {"object": obj}})
    header = sign_payload(body, secret, timestamp)
    response = env.client.post("/api/v1/webhooks/stripe", content=body,
                               headers={"Stripe-Signature": header, "Content-Type": "application/json"})
    return event_id, response


def deliver(env: Env, event_type: str, obj: dict, **kwargs) -> str:
    event_id, response = send(env, event_type, obj, **kwargs)
    assert response.status_code == 200, response.text
    return event_id


def drain(env: Env, *, rounds: int = 5) -> None:
    queue = JobQueue(env.factory)
    registry = StageRegistry()
    register_billing_handlers(registry, env.factory, env.gateway)
    for _ in range(rounds):
        make_due(env.factory)
        while run_once(queue, registry, "billing-test"):
            pass


def make_due(factory) -> None:
    with factory.begin() as db:
        db.execute(update(Job).where(Job.stage == "billing_event", Job.state == "queued").values(
            available_at=func.clock_timestamp() - timedelta(seconds=1)))


def purchase_of(factory, purchase_id: UUID) -> Purchase:
    with factory() as db:
        return db.get(Purchase, purchase_id)


def entitlements(factory, report_id: UUID) -> list[Entitlement]:
    with factory() as db:
        return list(db.scalars(select(Entitlement).where(Entitlement.report_id == report_id)))


def active(factory, report_id: UUID) -> bool:
    with factory() as db:
        return has_active_entitlement(db, report_id)


def event_row(factory, event_id: str) -> StripeEvent:
    with factory() as db:
        return db.get(StripeEvent, event_id)


def jobs_for(factory, event_id: str) -> list[Job]:
    with factory() as db:
        return list(db.scalars(select(Job).where(Job.subject_kind == "stripe_event", Job.subject_id == event_id)))


def paid_checkout(env: Env) -> tuple[UUID, str, str]:
    purchase_id, checkout_id = start_checkout(env)
    state = env.gateway.simulate_completion(checkout_id)
    return purchase_id, checkout_id, state.payment_intent_id


# ------------------------------------------------------------------ webhook authenticity and inbox

def test_invalid_signatures_rejected_and_nothing_persisted(factory):
    env = make_env(factory)
    _, checkout_id = start_checkout(env)
    env.gateway.simulate_completion(checkout_id)
    obj = session_object(checkout_id, env)
    with factory() as db:
        before = db.scalar(select(func.count()).select_from(StripeEvent))
    event_id, wrong = send(env, "checkout.session.completed", obj, secret="whsec_other")
    assert wrong.status_code == 400 and wrong.json()["code"] == "VALIDATION"
    _, stale = send(env, "checkout.session.completed", obj, timestamp=int(time.time()) - 3600)
    assert stale.status_code == 400
    body = json.dumps({"id": "evt_x", "object": "event", "type": "checkout.session.completed", "livemode": False,
                       "data": {"object": obj}})
    for headers in ({}, {"Stripe-Signature": "t=1,v1=00"}, {"Stripe-Signature": "garbage"},
                    {"Stripe-Signature": sign_payload(body + " ", WEBHOOK_SECRET)}):  # signature over a different body
        assert env.client.post("/api/v1/webhooks/stripe", content=body, headers=headers).status_code == 400
    assert env.client.post("/api/v1/webhooks/stripe", content=b"\xff\xfe", headers={
        "Stripe-Signature": sign_payload("x", WEBHOOK_SECRET)}).status_code == 400
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StripeEvent)) == before
    assert jobs_for(factory, event_id) == []


def test_webhook_unconfigured_secret_is_5xx_not_open(factory):
    app = create_app(Settings(), database_probe=lambda _: True, session_factory=factory, payments=FixtureStripe())
    response = TestClient(app).post("/api/v1/webhooks/stripe", content=b"{}", headers={"Stripe-Signature": "t=1,v1=0"})
    assert response.status_code == 503


def test_duplicate_event_id_returns_200_with_one_event_and_one_job(factory):
    env = make_env(factory)
    _, checkout_id, _ = paid_checkout(env)
    obj = session_object(checkout_id, env)
    event_id = deliver(env, "checkout.session.completed", obj)
    assert deliver(env, "checkout.session.completed", obj, event_id=event_id) == event_id
    assert deliver(env, "checkout.session.completed", obj, event_id=event_id) == event_id
    assert len(jobs_for(factory, event_id)) == 1
    job = jobs_for(factory, event_id)[0]
    assert job.session_id is None and job.stage == "billing_event"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(StripeEvent).where(StripeEvent.event_id == event_id)) == 1
    assert event_row(factory, event_id).processing_state == "pending"
    stored = event_row(factory, event_id).payload
    assert set(stored["data"]["object"]) <= {"id", "object", "payment_intent", "payment_status", "status", "mode", "metadata",
                                             "amount_total", "currency", "livemode", "client_reference_id", "metadata"}


def test_webhook_database_down_returns_5xx(factory):
    dead = sessionmaker(create_engine("postgresql+psycopg://u:p@127.0.0.1:1/none", connect_args={"connect_timeout": 1}))
    app = create_app(Settings(stripe_webhook_secret=SecretStr(WEBHOOK_SECRET)), database_probe=lambda _: True,
                     session_factory=dead, payments=FixtureStripe())
    env = make_env(factory)
    env.client = TestClient(app)
    obj = {"object": "checkout.session", "id": "cs_test_synthetic_x", "livemode": False}
    _, response = send(env, "checkout.session.completed", obj)
    assert response.status_code == 503 and response.json()["code"] == "INTERNAL"


def test_inbox_row_and_job_commit_together(factory, monkeypatch):
    env = make_env(factory)
    import app.services.billing as billing

    def boom(*args, **kwargs):
        raise OperationalError("simulated", None, Exception("db lost"))

    monkeypatch.setattr(billing, "enqueue", boom)
    event_id, response = send(env, "checkout.session.completed", {"object": "checkout.session", "id": "cs_x", "livemode": False})
    assert response.status_code == 503
    assert event_row(factory, event_id) is None and jobs_for(factory, event_id) == []


# ------------------------------------------------------------------ checkout

def test_checkout_persists_purchase_first_and_binds_checkout(factory):
    env = make_env(factory)
    purchase_id, checkout_id = start_checkout(env)
    purchase = purchase_of(factory, purchase_id)
    assert (purchase.expected_amount, purchase.currency, purchase.state) == (1900, "usd", "pending")
    assert purchase.checkout_id == checkout_id and purchase.payment_intent_id is None
    assert env.gateway.last_idempotency_key == str(purchase_id)
    session = env.gateway.retrieve_checkout(checkout_id)
    assert session.metadata == {"purchase_id": str(purchase_id), "report_id": str(env.report_id)}
    assert not active(factory, env.report_id)


def test_checkout_failure_leaves_unbound_purchase_that_is_reused(factory):
    env = make_env(factory)
    env.gateway.create_failures.append(ProviderUnavailable("down"))
    first = env.client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"}, headers=env.auth)
    assert first.status_code == 503 and first.json()["code"] == "INTERNAL"
    with factory() as db:
        rows = list(db.scalars(select(Purchase).where(Purchase.report_id == env.report_id)))
    assert len(rows) == 1 and rows[0].checkout_id is None and rows[0].state == "pending"
    purchase_id, checkout_id = start_checkout(env)
    assert purchase_id == rows[0].id and env.gateway.last_idempotency_key == str(rows[0].id)


@pytest.mark.parametrize("product", ["report_unlock_plus", "seller_kit", "subscription", "", None, 19])
def test_other_products_rejected(factory, product):
    env = make_env(factory)
    response = env.client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": product}, headers=env.auth)
    assert response.status_code == 422 and response.json()["code"] == "VALIDATION"
    assert env.client.post(f"/api/v1/reports/{env.slug}/checkout", json={}, headers=env.auth).status_code == 422
    assert env.gateway.create_calls == 0


def test_checkout_requires_ready_report(factory):
    env = make_env(factory, ready=False)
    response = env.client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"}, headers=env.auth)
    assert response.status_code == 409 and response.json()["code"] == "REPORT_NOT_READY"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Purchase).where(Purchase.report_id == env.report_id)) == 0
    assert env.gateway.create_calls == 0


def test_share_slug_holder_and_foreign_owner_cannot_create_checkout(factory):
    env = make_env(factory)
    other = make_env(factory)
    body = {"product": "report_unlock"}
    path = f"/api/v1/reports/{env.slug}/checkout"
    assert env.client.post(path, json=body).status_code == 401  # slug alone grants nothing
    assert env.client.post(path, json=body, headers={"Authorization": "Bearer " + secrets.token_urlsafe(32)}).status_code == 401
    foreign = env.client.post(path, json=body, headers=other.auth)
    assert foreign.status_code == 404 and foreign.json()["code"] == "NOT_FOUND"
    assert env.client.post("/api/v1/reports/unknown-slug-0000000000000/checkout", json=body, headers=env.auth).status_code == 404
    assert env.gateway.create_calls == 0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Purchase).where(Purchase.report_id == env.report_id)) == 0


def test_deleting_session_cannot_start_checkout(factory):
    env = make_env(factory)
    with factory.begin() as db:
        db.execute(update(Session).where(Session.id == env.session_id).values(state=SessionState.DELETING))
    response = env.client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"}, headers=env.auth)
    assert response.status_code == 404 and env.gateway.create_calls == 0


def test_already_unlocked_report_conflicts(factory):
    env = make_env(factory)
    _, checkout_id, _ = paid_checkout(env)
    deliver(env, "checkout.session.completed", session_object(checkout_id, env))
    drain(env)
    assert active(factory, env.report_id)
    response = env.client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"}, headers=env.auth)
    assert response.status_code == 409 and response.json()["code"] == "STATE_CONFLICT"


def test_browser_return_grants_nothing(factory):
    env = make_env(factory)
    purchase_id, checkout_id, _ = paid_checkout(env)  # provider says paid, but no verified event processed yet
    for path in ("/checkout/return?result=success", f"/checkout/return?result=success&purchase={purchase_id}",
                 f"/api/v1/reports/{env.slug}/checkout?result=success", f"/api/v1/reports/{env.slug}/unlock",
                 f"/api/v1/purchases/{purchase_id}", f"/api/v1/webhooks/stripe?checkout={checkout_id}"):
        assert env.client.get(path, headers=env.auth).status_code in (404, 405)
    assert not active(factory, env.report_id) and entitlements(factory, env.report_id) == []
    assert purchase_of(factory, purchase_id).state == "pending"


# ------------------------------------------------------------------ fulfillment

def test_duplicate_and_different_events_grant_exactly_once(factory):
    env = make_env(factory)
    purchase_id, checkout_id, intent = paid_checkout(env)
    obj = session_object(checkout_id, env)
    ids = [deliver(env, "checkout.session.completed", obj),
           deliver(env, "checkout.session.async_payment_succeeded", obj),
           deliver(env, "checkout.session.completed", obj)]
    ids.append(deliver(env, "checkout.session.completed", obj, event_id=ids[0]))  # duplicate delivery
    assert len({job.id for i in ids for job in jobs_for(factory, i)}) == 3
    drain(env)
    purchase = purchase_of(factory, purchase_id)
    assert purchase.state == "paid" and purchase.payment_intent_id == intent
    assert len(entitlements(factory, env.report_id)) == 1 and active(factory, env.report_id)
    assert all(event_row(factory, i).processing_state == "processed" for i in ids)
    assert all(job.state == "succeeded" for i in ids for job in jobs_for(factory, i))


def test_concurrent_workers_on_different_events_grant_once(factory):
    env = make_env(factory)
    purchase_id, checkout_id, _ = paid_checkout(env)
    obj = session_object(checkout_id, env)
    ids = [deliver(env, "checkout.session.completed" if n % 2 else "checkout.session.async_payment_succeeded", obj)
           for n in range(4)]

    def work(n: int) -> None:
        registry = StageRegistry()
        register_billing_handlers(registry, factory, env.gateway)
        run_once(JobQueue(factory), registry, f"concurrent-{n}")

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(work, range(4)))
    drain(env, rounds=2)
    assert len(entitlements(factory, env.report_id)) == 1
    assert purchase_of(factory, purchase_id).state == "paid"
    assert all(event_row(factory, i).processing_state == "processed" for i in ids)


def test_unpaid_completion_grants_nothing_until_async_success(factory):
    env = make_env(factory)
    purchase_id, checkout_id = start_checkout(env)
    env.gateway.simulate_completion(checkout_id, paid=False)  # delayed method: complete but unpaid
    first = deliver(env, "checkout.session.completed", session_object(checkout_id, env, payment_status="unpaid"))
    # a forged/optimistic body claiming paid cannot override provider state either
    second = deliver(env, "checkout.session.completed", session_object(checkout_id, env, payment_status="paid"))
    drain(env)
    assert purchase_of(factory, purchase_id).state == "pending" and purchase_of(factory, purchase_id).payment_intent_id is None
    assert entitlements(factory, env.report_id) == [] and not active(factory, env.report_id)
    assert event_row(factory, first).processing_state == event_row(factory, second).processing_state == "processed"
    env.gateway.simulate_override(checkout_id, payment_status="paid")
    deliver(env, "checkout.session.async_payment_succeeded", session_object(checkout_id, env))
    drain(env)
    assert purchase_of(factory, purchase_id).state == "paid" and active(factory, env.report_id)


def test_async_payment_failure_marks_failed_and_later_success_still_converges(factory):
    env = make_env(factory)
    purchase_id, checkout_id = start_checkout(env)
    env.gateway.simulate_completion(checkout_id, paid=False)
    deliver(env, "checkout.session.async_payment_failed", session_object(checkout_id, env, payment_status="unpaid"))
    drain(env)
    assert purchase_of(factory, purchase_id).state == "failed" and not active(factory, env.report_id)
    env.gateway.simulate_override(checkout_id, payment_status="paid")  # provider later reports success
    deliver(env, "checkout.session.async_payment_succeeded", session_object(checkout_id, env))
    drain(env)
    assert purchase_of(factory, purchase_id).state == "paid" and active(factory, env.report_id)
    # a late failure event arriving afterwards cannot demote verified payment
    deliver(env, "checkout.session.async_payment_failed", session_object(checkout_id, env, payment_status="unpaid"))
    drain(env)
    assert purchase_of(factory, purchase_id).state == "paid" and active(factory, env.report_id)


MISMATCHES = {
    "amount": ({"amount_total": 100}, "AMOUNT_MISMATCH"),
    "currency": ({"currency": "eur"}, "AMOUNT_MISMATCH"),
    "mode": ({"mode": "subscription"}, "MODE_REJECTED"),
    "livemode": ({"livemode": True}, "LIVEMODE_REJECTED"),
    "report_binding": ({"metadata": {"purchase_id": "x", "report_id": str(uuid4())}}, "BINDING_MISMATCH"),
    "purchase_binding": ({"client_reference_id": str(uuid4())}, "BINDING_MISMATCH"),
    "not_complete": ({"status": "open"}, None),
}


@pytest.mark.parametrize("name", list(MISMATCHES))
def test_unverified_provider_state_never_grants(factory, name):
    overrides, code = MISMATCHES[name]
    env = make_env(factory)
    purchase_id, checkout_id = start_checkout(env)
    env.gateway.simulate_completion(checkout_id)
    if name == "report_binding":
        overrides = {"metadata": {"purchase_id": str(purchase_id), "report_id": str(uuid4())}}
    env.gateway.simulate_override(checkout_id, **overrides)
    event_id = deliver(env, "checkout.session.completed", session_object(checkout_id, env))  # body claims valid
    drain(env)
    assert purchase_of(factory, purchase_id).state == "pending"
    assert entitlements(factory, env.report_id) == [] and not active(factory, env.report_id)
    row = event_row(factory, event_id)
    assert row.processing_state == ("failed" if code else "processed") and row.error == code


def test_event_claiming_livemode_is_rejected_even_if_provider_state_is_valid(factory):
    env = make_env(factory)
    purchase_id, checkout_id, _ = paid_checkout(env)
    event_id = deliver(env, "checkout.session.completed", session_object(checkout_id, env), livemode=True)
    drain(env)
    row = event_row(factory, event_id)
    assert (row.processing_state, row.error) == ("failed", "LIVEMODE_REJECTED")
    assert purchase_of(factory, purchase_id).state == "pending" and not active(factory, env.report_id)


def test_event_naming_another_reports_checkout_cannot_unlock_this_report(factory):
    shared = FixtureStripe()
    env_a, env_b = make_env(factory, gateway=shared), make_env(factory, gateway=shared)
    purchase_a, _ = start_checkout(env_a)
    purchase_b, checkout_b, _ = paid_checkout(env_b)
    # Delivered through A's context, but it names B's Checkout: only B's own purchase is evaluated.
    deliver(env_a, "checkout.session.completed", session_object(checkout_b, env_b))
    drain(env_a)
    assert purchase_of(factory, purchase_a).state == "pending" and not active(factory, env_a.report_id)
    assert purchase_of(factory, purchase_b).state == "paid" and active(factory, env_b.report_id)
    assert entitlements(factory, env_a.report_id) == []


def test_payment_intent_cannot_be_claimed_by_two_purchases(factory):
    env = make_env(factory)
    _, first_checkout, intent = paid_checkout(env)
    deliver(env, "checkout.session.completed", session_object(first_checkout, env))
    drain(env)
    # second purchase's provider state reuses the same payment intent id
    other = make_env(factory)
    _, second_checkout = start_checkout(other)
    other.gateway.simulate_completion(second_checkout, payment_intent_id=intent)
    event_id = deliver(other, "checkout.session.completed", session_object(second_checkout, other))
    drain(other)
    row = event_row(factory, event_id)
    assert (row.processing_state, row.error) == ("failed", "PAYMENT_INTENT_CONFLICT")
    assert not active(factory, other.report_id) and active(factory, env.report_id)


def test_unsupported_events_are_recorded_ignored(factory):
    env = make_env(factory)
    event_id = deliver(env, "customer.subscription.created", {"object": "subscription", "id": "sub_x", "livemode": False})
    other = deliver(env, "checkout.session.expired", {"object": "checkout.session", "id": "cs_x", "livemode": False})
    drain(env)
    for identifier in (event_id, other):
        row = event_row(factory, identifier)
        assert row.processing_state == "ignored" and row.processed_at is not None
        assert [job.state for job in jobs_for(factory, identifier)] == ["succeeded"]


def test_unmatched_checkout_retries_visibly_then_records_failure(factory):
    env = make_env(factory)
    event_id = deliver(env, "checkout.session.completed", {
        "object": "checkout.session", "id": "cs_test_unknown", "payment_status": "paid", "livemode": False})
    queue = JobQueue(factory)
    registry = StageRegistry()
    register_billing_handlers(registry, factory, env.gateway)
    assert run_once(queue, registry, "billing-test")
    job = jobs_for(factory, event_id)[0]
    assert (job.state, job.attempts, job.last_error) == ("queued", 1, "TRANSIENT")
    assert event_row(factory, event_id).processing_state == "pending"
    drain(env)
    row, job = event_row(factory, event_id), jobs_for(factory, event_id)[0]
    assert (row.processing_state, row.error, job.state, job.attempts) == ("failed", "UNMATCHED_PURCHASE", "succeeded", 3)


# ------------------------------------------------------------------ refunds and ordering

def test_refund_before_completion_converges_to_revoked_without_ever_granting(factory):
    env = make_env(factory)
    purchase_id, checkout_id, intent = paid_checkout(env)
    env.gateway.simulate_refund(checkout_id, PRICE)
    refund_ids = [deliver(env, "charge.refunded", charge_object(intent)) for _ in range(2)]
    drain(env)  # purchase has no stored payment_intent: the adapter resolves the charge's Checkout
    purchase = purchase_of(factory, purchase_id)
    assert purchase.state == "refunded" and purchase.payment_intent_id == intent
    assert entitlements(factory, env.report_id) == [] and not active(factory, env.report_id)
    late = [deliver(env, "checkout.session.completed", session_object(checkout_id, env)),
            deliver(env, "checkout.session.async_payment_succeeded", session_object(checkout_id, env))]
    deliver(env, "charge.refunded", charge_object(intent), event_id=refund_ids[0])
    drain(env)
    assert purchase_of(factory, purchase_id).state == "refunded"
    assert entitlements(factory, env.report_id) == [] and not active(factory, env.report_id)
    assert all(event_row(factory, i).processing_state == "processed" for i in refund_ids + late)


def test_paid_then_refunded_revokes_and_old_completion_never_reactivates(factory):
    env = make_env(factory)
    purchase_id, checkout_id, intent = paid_checkout(env)
    completion = session_object(checkout_id, env)
    deliver(env, "checkout.session.completed", completion)
    drain(env)
    assert active(factory, env.report_id)
    env.gateway.simulate_refund(checkout_id, 500)  # any positive (partial) refund revokes
    for _ in range(3):
        deliver(env, "charge.refunded", charge_object(intent, 500))
    drain(env)
    granted = entitlements(factory, env.report_id)
    assert len(granted) == 1 and granted[0].revoked_at is not None
    assert purchase_of(factory, purchase_id).state == "refunded" and not active(factory, env.report_id)
    revoked_at = granted[0].revoked_at
    deliver(env, "checkout.session.completed", completion)  # replay of an old completion under a new event id
    deliver(env, "charge.refunded", charge_object(intent, 500))
    drain(env)
    again = entitlements(factory, env.report_id)
    assert len(again) == 1 and again[0].revoked_at == revoked_at and not active(factory, env.report_id)
    assert purchase_of(factory, purchase_id).state == "refunded"


def test_refund_event_without_refunded_provider_state_does_not_revoke(factory):
    env = make_env(factory)
    purchase_id, checkout_id, intent = paid_checkout(env)
    deliver(env, "checkout.session.completed", session_object(checkout_id, env))
    drain(env)
    deliver(env, "charge.refunded", charge_object(intent))  # provider truth (no refund) wins over a stale event body
    drain(env)
    assert purchase_of(factory, purchase_id).state == "paid" and active(factory, env.report_id)


def test_second_independent_purchase_survives_first_refund(factory):
    env = make_env(factory)
    first_id, first_checkout, first_intent = paid_checkout(env)
    deliver(env, "checkout.session.completed", session_object(first_checkout, env))
    drain(env)
    env.gateway.simulate_refund(first_checkout, PRICE)
    deliver(env, "charge.refunded", charge_object(first_intent))
    drain(env)
    assert not active(factory, env.report_id)
    second_id, second_checkout, _ = paid_checkout(env)  # checkout is allowed again once access is gone
    assert second_id != first_id
    deliver(env, "checkout.session.completed", session_object(second_checkout, env))
    drain(env)
    assert active(factory, env.report_id) and purchase_of(factory, second_id).state == "paid"
    deliver(env, "charge.refunded", charge_object(first_intent))  # duplicate refund of purchase one
    deliver(env, "checkout.session.completed", session_object(first_checkout, env))
    drain(env)
    assert active(factory, env.report_id)
    states = {e.source_purchase_id: e.revoked_at is None for e in entitlements(factory, env.report_id)}
    assert states == {first_id: False, second_id: True}
    env.gateway.simulate_refund(second_checkout, PRICE)
    deliver(env, "charge.refunded", charge_object(purchase_intent(factory, second_id)))
    drain(env)
    assert not active(factory, env.report_id)


def purchase_intent(factory, purchase_id: UUID) -> str:
    return purchase_of(factory, purchase_id).payment_intent_id


# ------------------------------------------------------------------ retries and fencing

def test_failed_db_transaction_during_processing_is_retried_without_losing_event(factory):
    env = make_env(factory)
    purchase_id, checkout_id, _ = paid_checkout(env)
    event_id = deliver(env, "checkout.session.completed", session_object(checkout_id, env))
    real_registry = StageRegistry()
    register_billing_handlers(real_registry, factory, env.gateway)
    real = real_registry.handler("billing_event")
    calls = {"n": 0}

    def flaky(lease):
        publication = real(lease)
        if calls["n"]:
            return publication
        calls["n"] += 1

        def publish_then_lose_database(db):
            publication.publish(db)  # effects staged inside the fenced transaction...
            raise OperationalError("simulated", None, Exception("connection lost"))  # ...then the commit fails

        return Publication(publish_then_lose_database)

    registry = StageRegistry()
    registry.register("billing_event", flaky)
    queue = JobQueue(factory)
    assert run_once(queue, registry, "billing-test")
    job = jobs_for(factory, event_id)[0]
    assert (job.state, job.attempts, job.last_error) == ("queued", 1, "INTERNAL")
    assert event_row(factory, event_id).processing_state == "pending"  # inbox row survived
    assert purchase_of(factory, purchase_id).state == "pending" and entitlements(factory, env.report_id) == []
    make_due(factory)
    assert run_once(queue, registry, "billing-test")
    assert event_row(factory, event_id).processing_state == "processed"
    assert purchase_of(factory, purchase_id).state == "paid" and len(entitlements(factory, env.report_id)) == 1


def test_provider_unavailable_is_retried_then_converges(factory):
    env = make_env(factory)
    purchase_id, checkout_id, _ = paid_checkout(env)
    obj = session_object(checkout_id, env)
    env.gateway.retrieve_failures.append(ProviderUnavailable("timeout"))
    event_id = deliver(env, "checkout.session.completed", obj)
    queue = JobQueue(factory)
    registry = StageRegistry()
    register_billing_handlers(registry, factory, env.gateway)
    assert run_once(queue, registry, "billing-test")
    job = jobs_for(factory, event_id)[0]
    assert (job.state, job.last_error) == ("queued", "TRANSIENT") and not active(factory, env.report_id)
    drain(env)
    assert purchase_of(factory, purchase_id).state == "paid" and len(entitlements(factory, env.report_id)) == 1


def test_stale_lease_cannot_publish_entitlement(factory):
    env = make_env(factory)
    purchase_id, checkout_id, _ = paid_checkout(env)
    event_id = deliver(env, "checkout.session.completed", session_object(checkout_id, env))
    registry = StageRegistry()
    register_billing_handlers(registry, factory, env.gateway)
    queue = JobQueue(factory)
    lease = queue.claim("stale-worker", stages=["billing_event"])
    assert lease is not None and lease.subject_id == event_id and lease.session_id is None
    publication = registry.handler("billing_event")(lease)
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == lease.job_id).values(lease_until=func.clock_timestamp() - timedelta(seconds=1)))
    assert queue.complete(lease, publication.publish) is False
    assert purchase_of(factory, purchase_id).state == "pending" and entitlements(factory, env.report_id) == []
    assert event_row(factory, event_id).processing_state == "pending"
    drain(env)  # a new worker publishes exactly once
    assert purchase_of(factory, purchase_id).state == "paid" and len(entitlements(factory, env.report_id)) == 1


# ------------------------------------------------------------------ derived access and adapter guards

def test_entitlement_is_derived_from_unrevoked_row_and_paid_purchase(factory):
    env = make_env(factory)
    purchase_id, checkout_id, _ = paid_checkout(env)
    deliver(env, "checkout.session.completed", session_object(checkout_id, env))
    drain(env)
    assert active(factory, env.report_id)
    with factory.begin() as db:  # a surviving row on a non-paid purchase must not count
        db.execute(update(Purchase).where(Purchase.id == purchase_id).values(state="failed"))
    assert not active(factory, env.report_id)
    with factory.begin() as db:
        db.execute(update(Purchase).where(Purchase.id == purchase_id).values(state="paid"))
        db.execute(update(Entitlement).where(Entitlement.source_purchase_id == purchase_id).values(revoked_at=func.now()))
    assert not active(factory, env.report_id)
    assert not active(factory, uuid4())


def test_live_keys_and_accidental_test_mode_are_refused():
    for key in ("sk_live_" + "x" * 24, "rk_live_" + "x" * 24, "whsec_x", ""):
        with pytest.raises(ValueError):
            StripeTestModeGateway(key)
    with pytest.raises(ValueError):
        build_gateway("test", None)
    with pytest.raises(ValueError):
        build_gateway("live", "sk_test_x")
    assert isinstance(build_gateway("fixture", None), FixtureStripe)
    assert Settings().payments_mode == "fixture" and FixtureStripe.synthetic is True
    gateway = StripeTestModeGateway("sk_test_" + "x" * 24)  # constructing performs no network call
    assert gateway.synthetic is False and gateway._client._requestor._options.max_network_retries == 0


def test_decide_never_reactivates_a_refunded_purchase_even_if_provider_view_looks_paid():
    from app.adapters.stripe import ProviderCheckout
    from app.services.billing import PurchaseView, decide
    purchase_id, report_id = uuid4(), uuid4()
    purchase = PurchaseView(purchase_id, report_id, "refunded", "cs_test_1", "pi_1", 1900, "usd")
    provider = ProviderCheckout("cs_test_1", "complete", "paid", "payment", False, 1900, "usd", "pi_1", str(purchase_id),
                                {"purchase_id": str(purchase_id), "report_id": str(report_id)}, 0)
    for event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        decision = decide(purchase, provider, event_type)
        assert decision.state == "refunded" and not decision.grant and decision.revoke

# Checkout request replay stores purchase identity only; provider URLs are never persisted.
def checkout_post(env, key=None):
    return env.client.post(f'/api/v1/reports/{env.slug}/checkout', json={'product':'report_unlock'},
        headers={**env.auth, **({'Idempotency-Key':key} if key is not None else {})})


def test_checkout_idempotency_replays_purchase_and_url_without_storing_url(factory):
    from app.models import RequestKey
    env = make_env(factory)
    first = checkout_post(env,'checkout-attempt')
    second = checkout_post(env,'checkout-attempt')
    assert first.status_code == second.status_code == 201 and first.json() == second.json()
    with factory() as db:
        purchases = list(db.scalars(select(Purchase).where(Purchase.report_id == env.report_id)))
        keys = list(db.scalars(select(RequestKey).where(RequestKey.authority_id == env.session_id)))
    assert len(purchases) == len(keys) == 1
    assert keys[0].response == {'purchase_id':str(purchases[0].id)}
    assert 'checkout_url' not in json.dumps(keys[0].response)


def test_repeated_pending_checkout_reuses_bound_identity_and_parallel_requests(factory):
    env = make_env(factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda key: checkout_post(env,key),['a','b']))
    assert [r.status_code for r in responses] == [201,201]
    assert responses[0].json() == responses[1].json()
    assert checkout_post(env).json() == responses[0].json()
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Purchase).where(Purchase.report_id == env.report_id)) == 1


def test_provider_failure_key_retries_original_purchase_and_provider_io_outside_transaction(factory):
    class Probe(FixtureStripe):
        def create_checkout(self,request):
            with factory.begin() as db:
                # NOWAIT establishes the owner lock is not held during provider I/O.
                db.execute(select(Session.id).where(Session.id == env.session_id).with_for_update(nowait=True))
                assert db.get(Purchase,request.purchase_id) is not None
            return super().create_checkout(request)
    gateway = Probe()
    env = make_env(factory,gateway=gateway)
    gateway.create_failures.append(ProviderUnavailable('synthetic'))
    assert checkout_post(env,'retry').status_code == 503
    assert checkout_post(env,'retry').status_code == 201
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Purchase).where(Purchase.report_id == env.report_id)) == 1


def test_expired_replay_does_not_create_old_provider_identity(factory):
    env = make_env(factory)
    assert checkout_post(env,'old').status_code == 201
    with factory.begin() as db:
        db.execute(update(Purchase).where(Purchase.report_id == env.report_id).values(
            created_at=func.clock_timestamp()-timedelta(hours=24)))
    calls = env.gateway.create_calls
    assert checkout_post(env,'old').status_code == 409
    assert env.gateway.create_calls == calls
    assert checkout_post(env,'new').status_code == 201
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Purchase).where(Purchase.report_id == env.report_id)) == 2


def test_provider_expired_checkout_is_failed_and_new_key_can_replace(factory):
    env = make_env(factory)
    assert checkout_post(env,'expired').status_code == 201
    with factory() as db:
        purchase = db.scalar(select(Purchase).where(Purchase.report_id == env.report_id))
    env.gateway.simulate_override(purchase.checkout_id,status='expired')
    assert checkout_post(env,'expired').status_code == 409
    assert purchase_of(factory,purchase.id).state == 'failed'
    assert checkout_post(env,'fresh').status_code == 201


def test_completed_provider_checkout_waits_for_webhook_without_second_purchase(factory):
    env = make_env(factory)
    assert checkout_post(env,'complete').status_code == 201
    with factory() as db:
        purchase = db.scalar(select(Purchase).where(Purchase.report_id == env.report_id))
    env.gateway.simulate_completion(purchase.checkout_id)
    calls = env.gateway.create_calls
    assert checkout_post(env,'another-click').status_code == 409
    assert env.gateway.create_calls == calls and not active(factory,env.report_id)


def test_deletion_during_provider_create_prevents_checkout_binding(factory):
    class DeleteDuringCreate(FixtureStripe):
        def create_checkout(self,request):
            created = super().create_checkout(request)
            with factory.begin() as db:
                db.execute(update(Session).where(Session.id == env.session_id).values(state='deleting'))
            return created
    env = make_env(factory,gateway=DeleteDuringCreate())
    assert checkout_post(env,'race').status_code == 404
    with factory() as db:
        purchase = db.scalar(select(Purchase).where(Purchase.report_id == env.report_id))
        assert purchase.checkout_id is None


def test_checkout_key_validation_and_conflicting_saved_request(factory):
    from app.models import RequestKey
    env = make_env(factory)
    assert checkout_post(env,'').status_code == 422
    assert checkout_post(env,'x'*256).status_code == 422
    assert env.gateway.create_calls == 0
    assert checkout_post(env,'same-key').status_code == 201
    with factory.begin() as db:
        # Persisted request hash mismatch models a different request under the same authority/key.
        db.execute(update(RequestKey).where(RequestKey.authority_id == env.session_id).values(request_hash='f'*64))
    calls = env.gateway.create_calls
    assert checkout_post(env,'same-key').status_code == 409
    assert env.gateway.create_calls == calls


def _demo_client(factory, env: Env, **settings_kwargs):
    app = create_app(Settings(stripe_webhook_secret=SecretStr(WEBHOOK_SECRET), **settings_kwargs),
                     database_probe=lambda _: True, session_factory=factory, payments=env.gateway)
    return TestClient(app, client=("198.19.250.250", 12345))


def test_demo_free_unlock_grants_labeled_entitlement_without_provider(factory):
    env = make_env(factory)
    client = _demo_client(factory, env, demo_free_unlock=True)
    response = client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"},
                           headers=env.auth)
    assert response.status_code == 201, response.text
    assert response.json() == {"checkout_url": None, "unlocked": True}
    assert env.gateway.create_calls == 0  # no provider call, no charge
    with factory() as db:
        from app.services.billing import has_active_entitlement

        purchase = db.scalar(select(Purchase).where(Purchase.report_id == env.report_id))
        assert purchase.state == "paid"
        assert purchase.checkout_id == f"demo-unlock-{purchase.id}"
        assert purchase.payment_intent_id == f"pi_demo_unlock_{purchase.id}"
        assert has_active_entitlement(db, env.report_id)
    again = client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"},
                        headers=env.auth)
    assert again.status_code == 409  # already entitled: no second demo purchase


def _account_token(client: TestClient) -> str:
    response = client.post("/api/v1/accounts/register",
                           json={"email": f"dealer-{uuid4().hex[:16]}@example.com", "password": "synthetic-pass-1234"})
    assert response.status_code == 201, response.text
    return response.json()["token"]


def test_account_the_inspection_is_saved_to_can_unlock_its_report(factory):
    # Same authority rule as reading the report: owner token, or the account the inspection is saved to.
    env = make_env(factory)
    client = _demo_client(factory, env, demo_free_unlock=True)
    account = _account_token(client)
    claim = client.post("/api/v1/accounts/claim", json={"session_id": str(env.session_id), "owner_token": env.token},
                        headers={"Authorization": "Bearer " + account})
    assert claim.status_code == 200, claim.text
    response = client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"},
                           headers={"Authorization": "Bearer " + account})
    assert response.status_code == 201, response.text
    assert response.json() == {"checkout_url": None, "unlocked": True}
    with factory() as db:
        purchase = db.scalar(select(Purchase).where(Purchase.report_id == env.report_id))
        assert purchase.session_id == env.session_id and purchase.state == "paid"
        assert has_active_entitlement(db, env.report_id)


def test_unlinked_account_and_unknown_token_cannot_unlock(factory):
    env = make_env(factory)
    client = _demo_client(factory, env, demo_free_unlock=True)
    stranger = _account_token(client)  # valid account, but the inspection is not saved to it
    response = client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"},
                           headers={"Authorization": "Bearer " + stranger})
    assert response.status_code == 404, response.text
    unknown = client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"},
                          headers={"Authorization": "Bearer " + secrets.token_urlsafe(32)})
    assert unknown.status_code == 401, unknown.text
    with factory() as db:
        assert db.scalar(select(Purchase).where(Purchase.report_id == env.report_id)) is None


def test_demo_free_unlock_refused_outside_fixture_mode(factory):
    env = make_env(factory)
    client = _demo_client(factory, env, demo_free_unlock=True, payments_mode="test")
    response = client.post(f"/api/v1/reports/{env.slug}/checkout", json={"product": "report_unlock"},
                           headers=env.auth)
    assert response.status_code == 503
    with factory() as db:
        assert db.scalar(select(Purchase).where(Purchase.report_id == env.report_id)) is None
