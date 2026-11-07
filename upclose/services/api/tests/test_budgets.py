"""Fake-money checks on isolated PostgreSQL, including real competing transactions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
import subprocess
import sys
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models import BudgetBucket, Job, ModelCall, Session
from app.routers.operator import create_operator_router
from app.services.budgets import (
    PRICE_VERSION, BudgetConfig, BudgetError, BudgetService, PriceRates,
    ReservationRequest, UsageUnits, ceiling_cents, documented_prices, price_usage,
)


@pytest.fixture(scope="module")
def factory():
    url = os.environ.get("BUDGET_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires isolated BUDGET_TEST_DATABASE_URL PostgreSQL database")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing budget tests outside explicitly named upclose_test_* PostgreSQL database")
    environment = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=environment)
    engine = create_engine(url, connect_args={"connect_timeout": 3}, pool_size=10)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def db(factory):
    # Dedicated test DB was checked above. No production database truncation.
    with factory.begin() as connection:
        connection.execute(text("TRUNCATE sessions, budget_buckets CASCADE"))
    return factory


def live(**changes) -> BudgetConfig:
    return BudgetConfig(model_mode="live", verified_standard_access=True,
                        standard_access_receipt="synthetic-test-only-not-account-access", **changes)


def request(db, *, owner=None, units=None, **changes) -> ReservationRequest:
    with db.begin() as connection:
        now = connection.scalar(select(func.clock_timestamp()))
        if owner is None:
            owner = Session(token_hash=uuid4().hex * 2, consent_version="synthetic-v1", consent_at=now)
            connection.add(owner)
            connection.flush()
        job = Job(session_id=owner.id, subject_kind="session", subject_id=str(owner.id), stage="segment",
                  input_hash="0" * 64, config_hash="0" * 64, idempotency_key=uuid4().hex * 2,
                  state="running", attempts=1, lease_token=uuid4(), lease_until=now + timedelta(minutes=2),
                  attempt_started_at=now, worker_id="synthetic-test")
        connection.add(job)
        connection.flush()
        result = ReservationRequest(owner.id, job.id, uuid4(), job.lease_token, now + timedelta(minutes=5),
                                    "sam-3.1", PRICE_VERSION, units or UsageUnits(images=1))
    return replace(result, **changes)


def ledger(db):
    with db() as connection:
        return list(connection.scalars(select(ModelCall))), list(connection.scalars(select(BudgetBucket).order_by(BudgetBucket.scope_key)))


@pytest.mark.parametrize("micro,cents", [(0, 0), (1, 1), (9999, 1), (10000, 1), (10001, 2)])
def test_per_call_cent_ceiling(micro, cents):
    assert ceiling_cents(micro) == cents


def test_prices_count_each_prompt_minimum_and_cached_tokens():
    rates = documented_prices()
    assert price_usage("sam-3.1", UsageUnits(images=8), rates) == 20000
    assert price_usage("sam-3.1", UsageUnits(frames=1), rates) == 10000
    assert price_usage("sam-3.1", UsageUnits(frames=80), rates) == 16000
    assert 5 * price_usage("sam-3.1", UsageUnits(frames=120), rates) == 120000
    assert price_usage("muse-spark-1.3", UsageUnits(input_tokens=1000, cached_input_tokens=100, output_tokens=100), rates) == 1565
    assert price_usage("muse-spark-1.3", UsageUnits(input_tokens=1), rates) == 2


@pytest.mark.parametrize("rate", [float("nan"), Decimal("NaN"), Decimal("Infinity"), Decimal(-1), 1])
def test_bad_rates_rejected(rate):
    with pytest.raises(BudgetError, match="price_unverified"):
        PriceRates(frame=rate)


def test_unknown_and_missing_rates_fail_closed():
    with pytest.raises(BudgetError, match="model_unverified"):
        price_usage("invented", UsageUnits(images=1), documented_prices())
    with pytest.raises(BudgetError, match="price_unverified"):
        price_usage("sam-3.1", UsageUnits(images=1), {"sam-3.1": PriceRates(image=Decimal(2500))})
    with pytest.raises(BudgetError):
        PriceRates(minimum_frames=-1)
    with pytest.raises(BudgetError):
        UsageUnits(frames=True)


def test_reservation_replay_drift_and_dispatch_permit(db):
    service = BudgetService(db, live())
    req = request(db)
    created = service.reserve(req)
    assert created.reserved_micro_usd == 2500 and created.reserved_cents == 1
    assert created.may_send is False  # reservation is never transport authority
    assert service.reserve(req).call_id == created.call_id
    assert service.mark_dispatched(req.attempt_id, req.lease_token) is True
    assert service.mark_dispatched(req.attempt_id, req.lease_token) is False
    assert service.reserve(req).may_send is False
    with pytest.raises(BudgetError, match="attempt_conflict"):
        service.reserve(replace(req, units=UsageUnits(images=2)))
    calls, buckets = ledger(db)
    assert len(calls) == 1 and calls[0].state == "unknown"
    assert all(bucket.reserved_cents == 1 for bucket in buckets)


@pytest.mark.parametrize("mode", ["fixture", "replay"])
def test_fixture_replay_never_reserve_or_authorize(db, mode):
    req = request(db)
    service = BudgetService(db, BudgetConfig(model_mode=mode))
    assert service.reserve(req).reserved_micro_usd == 0
    assert service.mark_dispatched(req.attempt_id, req.lease_token) is False
    assert ledger(db) == ([], [])


@pytest.mark.parametrize("change,code", [
    ({"model": "unknown-model"}, "model_unverified"),
    ({"price_version": "unknown"}, "price_unverified"),
])
def test_reservation_missing_inputs_block_without_ledger(db, change, code):
    service = BudgetService(db, live())
    with pytest.raises(BudgetError, match=code):
        service.reserve(request(db, **change))
    assert ledger(db) == ([], [])


def test_access_missing_blocks_before_reservation(db):
    service = BudgetService(db, BudgetConfig(model_mode="live"))
    with pytest.raises(BudgetError, match="standard_access_unverified"):
        service.reserve(request(db))
    assert ledger(db) == ([], [])


def test_non_standard_demo_ack_reserves_and_dispatches_without_standard_receipt(db):
    # Demo track only: explicit acknowledgement substitutes for a Standard receipt; caps still bind.
    service = BudgetService(db, BudgetConfig(model_mode="live", allow_non_standard_demo=True,
                                             session_limit_cents=500, daily_limit_cents=5000))
    req = request(db)
    created = service.reserve(req)
    assert created.reserved_micro_usd == 2500 and created.may_send is False
    assert service.mark_dispatched(req.attempt_id, req.lease_token) is True
    service.settle(req.attempt_id, actual_units=UsageUnits(images=1), actual_micro_usd=2500,
                   outcome="succeeded", evidence_code="synthetic-demo-billing")
    assert service.operator_totals()["actual_micro_usd"] == 2500


@pytest.mark.parametrize("change,code", [
    ({"lease_token": uuid4()}, "lease_lost"),
    ({"deadline_at": datetime(2020, 1, 1, tzinfo=timezone.utc)}, "deadline_expired"),
])
def test_reserve_requires_current_lease_deadline(db, change, code):
    with pytest.raises(BudgetError, match=code):
        BudgetService(db, live()).reserve(request(db, **change))


def test_persisted_stage_deadline_cannot_be_extended_by_caller(db):
    req = request(db, deadline_at=datetime.now(timezone.utc) + timedelta(days=1))
    with db.begin() as connection:
        connection.execute(update(Job).where(Job.id == req.job_id).values(attempt_started_at=func.clock_timestamp() - text("interval '601 seconds'")))
    with pytest.raises(BudgetError, match="deadline_expired"):
        BudgetService(db, live()).reserve(req)


@pytest.mark.parametrize("change", ["expired_lease", "stale_token", "deleted", "deadline"])
def test_dispatch_rechecks_lease_and_deletion(db, change):
    service = BudgetService(db, live())
    req = request(db)
    service.reserve(req)
    with db.begin() as connection:
        if change == "expired_lease":
            connection.execute(update(Job).where(Job.id == req.job_id).values(lease_until=func.clock_timestamp() - text("interval '1 second'")))
        elif change == "stale_token":
            connection.execute(update(Job).where(Job.id == req.job_id).values(lease_token=uuid4()))
        elif change == "deadline":
            connection.execute(update(Job).where(Job.id == req.job_id).values(attempt_started_at=func.clock_timestamp() - text("interval '601 seconds'")))
        else:
            connection.execute(update(Session).where(Session.id == req.session_id).values(state="deleting"))
    with pytest.raises(BudgetError):
        service.mark_dispatched(req.attempt_id, req.lease_token)
    assert all(bucket.reserved_cents == 1 for bucket in ledger(db)[1])


def test_timeout_holds_reservation_and_retry_charged_separately(db):
    service = BudgetService(db, live(session_limit_cents=2))
    req = request(db)
    service.reserve(req)
    service.mark_dispatched(req.attempt_id, req.lease_token)
    service.mark_unknown(req.attempt_id)
    retry = replace(req, attempt_id=uuid4())
    service.reserve(retry)
    service.mark_dispatched(retry.attempt_id, retry.lease_token)
    with pytest.raises(BudgetError, match="budget_exhausted"):
        service.reserve(replace(req, attempt_id=uuid4()))
    assert len(ledger(db)[0]) == 2
    assert all(bucket.reserved_cents == 2 for bucket in ledger(db)[1])
    assert service.operator_totals()["unknown_micro_usd"] == 5000


def test_unknown_failure_reason_persisted_sanitized_and_still_unknown(db):
    service = BudgetService(db, live())
    req = request(db)
    service.reserve(req)
    service.mark_dispatched(req.attempt_id, req.lease_token)
    service.mark_unknown(req.attempt_id, failure_code="provider_timeout", http_status=503)
    calls, buckets = ledger(db)
    assert calls[0].state == "unknown"  # never free, never settled
    assert calls[0].actual_cost is None
    assert calls[0].requested_units["failure"] == {"code": "provider_timeout", "http_status": 503, "http_status_class": "5xx"}
    assert all(bucket.reserved_cents == 1 for bucket in buckets)
    assert service.operator_totals()["unknown_micro_usd"] == 2500
    # Unsafe code/status inputs are sanitized, never persisted verbatim.
    service.mark_unknown(req.attempt_id, failure_code="Bearer payload-secret", http_status=999)
    calls, _ = ledger(db)
    assert calls[0].requested_units["failure"] == {"code": "provider_failed", "http_status": None, "http_status_class": None}
    assert "payload-secret" not in str(calls[0].requested_units)
    # Unknown without a failure (e.g. missing usage) writes no failure record.
    req2 = request(db)
    service.reserve(req2)
    service.mark_dispatched(req2.attempt_id, req2.lease_token)
    service.mark_unknown(req2.attempt_id)
    calls, _ = ledger(db)
    by_attempt = {c.attempt_id: c for c in calls}
    assert "failure" not in by_attempt[req2.attempt_id].requested_units
    assert by_attempt[req2.attempt_id].state == "unknown"


def test_settlement_exact_idempotent_conflicting_and_after_deletion(db):
    service = BudgetService(db, live())
    req = request(db, units=UsageUnits(frames=120))
    service.reserve(req)
    service.mark_dispatched(req.attempt_id, req.lease_token)
    with db.begin() as connection:
        connection.execute(update(Session).where(Session.id == req.session_id).values(state="deleting"))
        connection.execute(update(Job).where(Job.id == req.job_id).values(lease_until=func.clock_timestamp() - text("interval '1 second'")))
    evidence = dict(actual_units=UsageUnits(frames=80), actual_micro_usd=16000, outcome="succeeded", evidence_code="synthetic-billing", provider_request_id="synthetic-request", latency_ms=2)
    service.settle(req.attempt_id, **evidence)
    service.settle(req.attempt_id, **evidence)
    with pytest.raises(BudgetError, match="settlement_conflict"):
        service.settle(req.attempt_id, **{**evidence, "actual_micro_usd": 18000})
    calls, buckets = ledger(db)
    assert calls[0].actual_cost == 16000
    assert all(bucket.reserved_cents == 0 and bucket.spent_cents == 2 for bucket in buckets)
    assert service.operator_totals()["actual_micro_usd"] == 16000


def test_unexpected_charge_above_reservation_is_recorded_honestly(db):
    service = BudgetService(db, live(session_limit_cents=1))
    req = request(db)
    service.reserve(req)
    service.settle(req.attempt_id, actual_units=None, actual_micro_usd=20001, outcome="reconciled", evidence_code="synthetic-verified-adjustment")
    assert all(bucket.spent_cents == 3 and bucket.reserved_cents == 0 for bucket in ledger(db)[1])
    with pytest.raises(BudgetError, match="budget_exhausted"):
        service.reserve(replace(req, attempt_id=uuid4()))


def test_daily_scope_is_original_day_for_late_settlement(db):
    service = BudgetService(db, live())
    req = request(db)
    service.reserve(req)
    # Simulate a persisted yesterday scope; settlement must not invent today's.
    with db.begin() as connection:
        call = connection.scalar(select(ModelCall))
        data = {**call.requested_units, "budget": {**call.requested_units["budget"]}}
        scopes = data["budget"]["scopes"]
        old = next(scope for scope in scopes if scope.startswith("daily:"))
        previous_day = "daily:2020-01-01"
        connection.execute(update(BudgetBucket).where(BudgetBucket.scope_key == old).values(scope_key=previous_day))
        data["budget"]["scopes"] = sorted(previous_day if scope == old else scope for scope in scopes)
        call.requested_units = data
    service.settle(req.attempt_id, actual_units=None, actual_micro_usd=2500, outcome="reconciled", evidence_code="synthetic-day-boundary")
    assert next(bucket for bucket in ledger(db)[1] if bucket.scope_key == previous_day).spent_cents == 1


def test_lower_config_cap_fails_closed_for_existing_bucket(db):
    req = request(db)
    BudgetService(db, live(session_limit_cents=10)).reserve(req)
    with pytest.raises(BudgetError, match="budget_exhausted"):
        BudgetService(db, live(session_limit_cents=1)).reserve(replace(req, attempt_id=uuid4()))


def test_dispatch_blocks_if_config_cap_lowered_since_reservation(db):
    req = request(db)
    BudgetService(db, live()).reserve(req)
    with pytest.raises(BudgetError, match="budget_exhausted"):
        BudgetService(db, live(session_limit_cents=0)).mark_dispatched(req.attempt_id, req.lease_token)
    assert ledger(db)[0][0].state == "reserved"


def test_concurrent_settlement_releases_and_charges_once(db):
    req = request(db)
    service = BudgetService(db, live())
    service.reserve(req)
    evidence = dict(actual_units=UsageUnits(images=1), actual_micro_usd=2500, outcome="succeeded", evidence_code="synthetic-billing")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: service.settle(req.attempt_id, **evidence), range(4)))
    assert all(bucket.reserved_cents == 0 and bucket.spent_cents == 1 for bucket in ledger(db)[1])


def test_dispatch_price_access_changes_fail_closed(db):
    req = request(db)
    BudgetService(db, live()).reserve(req)
    changed_rates = {**documented_prices(), "sam-3.1": PriceRates(image=Decimal(3000), frame=Decimal(200), minimum_frames=50)}
    with pytest.raises(BudgetError, match="price_unverified"):
        BudgetService(db, live(prices=changed_rates)).mark_dispatched(req.attempt_id, req.lease_token)
    with pytest.raises(BudgetError, match="standard_access_unverified"):
        BudgetService(db, BudgetConfig(model_mode="live")).mark_dispatched(req.attempt_id, req.lease_token)


@pytest.mark.parametrize("scope", ["session", "daily"])
def test_concurrent_transactions_cannot_exceed_either_budget(db, scope):
    initial = request(db)
    with db() as connection:
        owner = connection.get(Session, initial.session_id)
    requests = [initial]
    for _ in range(5):
        requests.append(request(db, owner=owner if scope == "session" else None))
    config = live(session_limit_cents=2 if scope == "session" else 100, daily_limit_cents=2 if scope == "daily" else 100)
    barrier = Barrier(len(requests))

    def reserve(req):
        barrier.wait(timeout=10)
        try:
            return BudgetService(db, config).reserve(req)
        except BudgetError as error:
            assert error.code == "budget_exhausted"
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, requests))
    assert sum(result is not None for result in results) == 2
    assert len(ledger(db)[0]) == 2
    assert all(bucket.reserved_cents <= bucket.limit_cents for bucket in ledger(db)[1])


def test_concurrent_same_attempt_dispatch_grants_once(db):
    service = BudgetService(db, live())
    req = request(db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.reserve(req), range(4)))
    assert len({result.call_id for result in results}) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        grants = list(pool.map(lambda _: service.mark_dispatched(req.attempt_id, req.lease_token), range(4)))
    assert grants.count(True) == 1


@pytest.mark.parametrize("token,authorization,status", [(None, None, 404), ("operator-secret", None, 401), ("operator-secret", "Bearer wrong", 401), ("operator-secret", "Bearer operator-secret", 200)])
def test_operator_scope_authorization_and_safe_totals(db, token, authorization, status):
    req = request(db)
    BudgetService(db, live()).reserve(req)
    app = FastAPI()
    app.include_router(create_operator_router(db, token))
    with TestClient(app) as client:
        response = client.get("/api/v1/operator/budgets", headers={"Authorization": authorization} if authorization else {})
    assert response.status_code == status
    assert response.headers["cache-control"] == "private, no-store"
    assert "operator-secret" not in response.text
    assert "requested_units" not in response.text
    if status != 200:
        assert response.headers["content-type"] == "application/problem+json"
    else:
        assert response.json()["reserved_micro_usd"] == 2500
        assert response.json()["actual_micro_usd"] == 0


def test_budget_service_does_not_advance_queue_retry_counter(db):
    req = request(db)
    with db.begin() as connection:
        connection.execute(update(Job).where(Job.id == req.job_id).values(attempts=3))
    service = BudgetService(db, live())
    service.reserve(req)
    service.mark_dispatched(req.attempt_id, req.lease_token)
    with db() as connection:
        assert connection.get(Job, req.job_id).attempts == 3


def test_unclaimed_zero_attempt_job_is_ineligible(db):
    req = request(db)
    with db.begin() as connection:
        connection.execute(update(Job).where(Job.id == req.job_id).values(attempts=0))
    with pytest.raises(BudgetError, match="attempt_limit"):
        BudgetService(db, live()).reserve(req)


def test_midnight_requires_new_current_day_reservation_before_dispatch(db):
    req = request(db)
    service = BudgetService(db, live())
    service.reserve(req)
    with db.begin() as connection:
        call = connection.scalar(select(ModelCall))
        data = {**call.requested_units, "budget": {**call.requested_units["budget"]}}
        scopes = data["budget"]["scopes"]
        old = next(scope for scope in scopes if scope.startswith("daily:"))
        connection.execute(update(BudgetBucket).where(BudgetBucket.scope_key == old).values(scope_key="daily:2020-01-01"))
        data["budget"]["scopes"] = sorted("daily:2020-01-01" if scope == old else scope for scope in scopes)
        call.requested_units = data
    with pytest.raises(BudgetError, match="reservation_day_expired"):
        service.mark_dispatched(req.attempt_id, req.lease_token)
    assert ledger(db)[0][0].state == "reserved"
    assert all(bucket.reserved_cents == 1 for bucket in ledger(db)[1])
    service.settle(req.attempt_id, actual_units=None, actual_micro_usd=0, outcome="reconciled", evidence_code="synthetic-proven-never-dispatched")
    assert all(bucket.reserved_cents == 0 for bucket in ledger(db)[1])
