"""HTTP/queue/budget foundation wiring with synthetic records and fake charges.

No user media, provider transport, real provider usage, or assessment quality is
exercised here. Every test uses a new HTTP owner and synthetic concept-run graph.
"""
import os
import subprocess
import sys
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.jobs.queue import JobQueue, JobSpec, enqueue
from app.main import create_app
from app.models import BudgetBucket, ConceptRun, Job, ModelCall, Part, PipelineRun, Report, ReportRevision
from app.services.budgets import (
    PRICE_VERSION, BudgetConfig, BudgetError, BudgetService, ReservationRequest, UsageUnits,
)
from app.settings import Settings


@pytest.fixture(scope="module")
def foundation_factory():
    url = os.environ.get("FOUNDATION_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires isolated FOUNDATION_TEST_DATABASE_URL or MODEL_TEST_DATABASE_URL")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing foundation checks outside explicitly named upclose_test_* PostgreSQL database")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True,
                   env={**os.environ, "DATABASE_URL": url})
    engine = create_engine(url, connect_args={"connect_timeout": 3})
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def create_synthetic_owner_and_job(factory):
    settings = Settings(model_mode="fixture")
    app = create_app(settings, database_probe=lambda _: True, session_factory=factory)
    # New valid test source IP prevents interference with other rate-limit tests.
    address = f"198.18.{uuid4().int % 256}.{uuid4().int % 254 + 1}"
    client = TestClient(app, client=(address, 12345))
    response = client.post("/api/v1/sessions", json={"purpose": "buyer", "consent": True,
                          "consent_version": settings.consent_version, "listing_text": "Synthetic foundation fixture; no vehicle assessment."})
    assert response.status_code == 201, response.text
    owner = response.json()
    session_id = UUID(owner["session_id"])
    headers = {"Authorization": "Bearer " + owner["session_token"]}
    response = client.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["parts"]) == 8
    with factory.begin() as db:
        part = db.scalar(select(Part).where(Part.session_id == session_id, Part.part_no == 1))
        run = PipelineRun(session_id=session_id, part_id=part.id, subject_key=f"synthetic-foundation:{uuid4()}",
                          input_hash="0" * 64, config_hash="0" * 64, config_hashes={"provenance": "synthetic"})
        db.add(run)
        db.flush()
        concept = ConceptRun(session_id=session_id, run_id=run.id, part_id=part.id,
                             concept="rust", prompt_id="synthetic-foundation-rust")
        db.add(concept)
        db.flush()
        concept_id = concept.id
        spec = JobSpec(session_id=session_id, subject_kind="concept_run", subject_id=str(concept_id),
                       stage="segment", input_hash="0" * 64, config_hash="0" * 64)
        job_id = enqueue(db, spec)
        assert enqueue(db, spec) == job_id
    return client, session_id, headers, concept_id, job_id


def fake_budget_service(factory):
    # Explicit trusted fake-money configuration only: not a live-access receipt.
    return BudgetService(factory, BudgetConfig(model_mode="live", verified_standard_access=True,
                         standard_access_receipt="synthetic-foundation-test-only-no-provider-access"))


def reservation(lease):
    return ReservationRequest(session_id=lease.session_id, job_id=lease.job_id, attempt_id=uuid4(),
                              lease_token=lease.token, deadline_at=lease.deadline, model="sam-3.1",
                              price_version=PRICE_VERSION, units=UsageUnits(images=1))


def test_http_owner_queue_fake_ledger_and_idempotent_finalize(foundation_factory):
    factory = foundation_factory
    client, session_id, headers, concept_id, job_id = create_synthetic_owner_and_job(factory)
    queue = JobQueue(factory)
    lease = queue.claim("synthetic-foundation-worker", stages=["segment"])
    assert lease is not None and lease.job_id == job_id
    req = reservation(lease)
    # The application default does not reserve paid spend or grant transport.
    fixture = BudgetService(factory).reserve(req)
    assert fixture.state == "fixture" and fixture.call_id is None and not fixture.may_send
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ModelCall).where(ModelCall.session_id == session_id)) == 0
    budget = fake_budget_service(factory)
    reserved = budget.reserve(req)
    assert not reserved.may_send
    assert budget.mark_dispatched(req.attempt_id, lease.token)
    budget.settle(req.attempt_id, actual_units=UsageUnits(images=1), actual_micro_usd=2500,
                  outcome="succeeded", evidence_code="synthetic-fixture-billing-not-real-usage")

    def publish(db):
        concept = db.get(ConceptRun, concept_id)
        concept.state = "succeeded"  # Synthetic empty fixture only, no real inference.
        concept.provider_call_id = reserved.call_id

    assert queue.complete(lease, publish=publish)
    assert not queue.complete(lease, publish=publish)
    first = client.post(f"/api/v1/sessions/{session_id}/finalize", json={}, headers=headers)
    second = client.post(f"/api/v1/sessions/{session_id}/finalize", json={}, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    with factory() as db:
        report = db.scalar(select(Report).where(Report.session_id == session_id))
        assert report.share_slug == first.json()["report_slug"]
        assert db.scalar(select(func.count()).select_from(Report).where(Report.session_id == session_id)) == 1
        assert db.scalar(select(func.count()).select_from(ReportRevision).where(ReportRevision.session_id == session_id)) == 1
        assert db.get(ConceptRun, concept_id).state == "succeeded"
        assert db.get(Job, job_id).state == "succeeded"
        bucket = db.get(BudgetBucket, f"session:{session_id}")
        assert bucket.reserved_cents == 0 and bucket.spent_cents == 1
    client.close()


def test_actual_reclaim_fences_old_dispatch_publish_and_preserves_unknown_charge(foundation_factory):
    factory = foundation_factory
    client, session_id, headers, concept_id, job_id = create_synthetic_owner_and_job(factory)
    queue = JobQueue(factory)
    first_lease = queue.claim("synthetic-foundation-old-worker", stages=["segment"])
    assert first_lease is not None and first_lease.job_id == job_id
    budget = fake_budget_service(factory)
    first = reservation(first_lease)
    budget.reserve(first)
    assert budget.mark_dispatched(first.attempt_id, first_lease.token)
    budget.mark_unknown(first.attempt_id)
    with factory.begin() as db:
        db.execute(update(Job).where(Job.id == job_id).values(lease_until=func.clock_timestamp() - text("interval '1 second'")))
    replacement = queue.claim("synthetic-foundation-new-worker", stages=["segment"])
    assert replacement is not None and replacement.job_id == job_id
    assert replacement.attempt == 2 and replacement.token != first_lease.token
    with pytest.raises(BudgetError, match="lease_lost"):
        budget.mark_dispatched(first.attempt_id, first_lease.token)
    stale_published = []
    assert not queue.complete(first_lease, publish=lambda _: stale_published.append(True))
    assert not stale_published
    second = reservation(replacement)
    second_call = budget.reserve(second)
    assert budget.mark_dispatched(second.attempt_id, replacement.token)
    budget.settle(second.attempt_id, actual_units=UsageUnits(images=1), actual_micro_usd=2500,
                  outcome="succeeded", evidence_code="synthetic-second-attempt-billing")

    def publish(db):
        concept = db.get(ConceptRun, concept_id)
        concept.state = "succeeded"
        concept.provider_call_id = second_call.call_id

    assert queue.complete(replacement, publish=publish)
    with factory() as db:
        calls = {call.attempt_id: call for call in db.scalars(select(ModelCall).where(ModelCall.session_id == session_id))}
        assert len(calls) == 2
        assert calls[first.attempt_id].state == "unknown" and calls[first.attempt_id].actual_cost is None
        assert calls[second.attempt_id].state == "succeeded" and calls[second.attempt_id].actual_cost == 2500
        bucket = db.get(BudgetBucket, f"session:{session_id}")
        assert bucket.reserved_cents == 1 and bucket.spent_cents == 1
        assert db.get(ConceptRun, concept_id).provider_call_id == second_call.call_id
    # Verified late billing can reconcile the old charge after the old lease died.
    budget.settle(first.attempt_id, actual_units=UsageUnits(images=1), actual_micro_usd=2500,
                  outcome="reconciled", evidence_code="synthetic-late-billing-evidence")
    assert client.post(f"/api/v1/sessions/{session_id}/finalize", json={}, headers=headers).status_code == 202
    with factory() as db:
        bucket = db.get(BudgetBucket, f"session:{session_id}")
        assert bucket.reserved_cents == 0 and bucket.spent_cents == 2
    client.close()
