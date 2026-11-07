"""Real PostgreSQL constraint checks; never run against a non-test database."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, insert, literal, select, text, update
from sqlalchemy.engine import Connection, Engine, make_url
from sqlalchemy.exc import IntegrityError, DataError
from sqlalchemy.orm import Session as ORMSession

from app.models import (Base, Session, Part, Upload, PipelineRun, ConceptRun, Job,
    Report, ReportRevision, Artifact, Finding, Coverage, Claim, ClaimEvidence,
    RevisionFinding, Purchase, Entitlement, StripeEvent, ModelCall, BudgetBucket, RequestKey)

H = "a" * 64
NOW = datetime.now(timezone.utc)


def migrate(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True)


def clear_vehicle_mask_jobs(engine: Engine) -> None:
    # The f7b1 downgrade intentionally refuses while vehicle-mask jobs remain
    # (see test_vehicle_mask_pipeline.isolate). Earlier pipeline tests can
    # leave committed mask jobs in the shared disposable test database, so
    # migration round-trip tests clear them before downgrading past f7b1.
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM jobs WHERE stage = 'vehicle_mask'"))


@pytest.fixture(scope="session")
def model_engine() -> Engine:
    url = os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MODEL_TEST_DATABASE_URL must name an isolated PostgreSQL test database")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing schema checks outside an explicitly named upclose_test_* PostgreSQL database")
    # Migration subprocess uses the same explicitly validated test database.
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    migrate("upgrade", "head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture
def db(model_engine: Engine):
    with model_engine.connect() as connection:
        transaction = connection.begin()
        yield connection
        transaction.rollback()


def row(db: Connection, model_type: type, **values):
    return db.execute(insert(model_type).values(**values).returning(model_type.__table__)).mappings().one()


def seed(db: Connection) -> dict:
    suffix = uuid4().hex
    session = row(db, Session, token_hash=suffix * 2, consent_version="test-v1", consent_at=NOW)
    part = row(db, Part, session_id=session["id"], part_no=1, zone="driver_side")
    run = row(db, PipelineRun, session_id=session["id"], part_id=part["id"], subject_key=suffix,
              input_hash=H, config_hash=H, config_hashes={"rubric": H})
    concept = row(db, ConceptRun, session_id=session["id"], run_id=run["id"], part_id=part["id"],
                  concept="rust", prompt_id="rust-v1")
    frame = row(db, Artifact, session_id=session["id"], kind="frame", key=f"{suffix}/frame.jpg", sha256=H)
    finding = row(db, Finding, session_id=session["id"], run_id=run["id"], part_id=part["id"],
                  concept_run_id=concept["id"], track_id="track-1", frame_key=frame["key"],
                  frame_index=0, frame_width=1280, frame_height=720,
                  bbox={"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, status="verified",
                  concept="rust", severity=2, evidence_note="Synthetic fixture")
    report = row(db, Report, session_id=session["id"], share_slug=suffix)
    revision = row(db, ReportRevision, session_id=session["id"], report_id=report["id"], revision_no=1)
    row(db, RevisionFinding, session_id=session["id"], revision_id=revision["id"], finding_id=finding["id"])
    claim = row(db, Claim, session_id=session["id"], report_revision_id=revision["id"], claim_index=0,
                text="Visible rust", verdict="contradicted", rationale="Synthetic finding")
    purchase = row(db, Purchase, session_id=session["id"], report_id=report["id"])
    job = row(db, Job, session_id=session["id"], subject_kind="part_run", subject_id=str(run["id"]),
              stage="ingest", input_hash=H, config_hash=H, idempotency_key=suffix * 2)
    return dict(session=session, part=part, run=run, concept=concept, frame=frame, finding=finding,
                report=report, revision=revision, claim=claim, purchase=purchase, job=job)


def rejects(db: Connection, statement) -> None:
    with pytest.raises((IntegrityError, DataError)):
        with db.begin_nested():
            db.execute(statement)


def test_postgres_version_and_migration_roundtrip(model_engine: Engine):
    with model_engine.connect() as connection:
        assert connection.scalar(text("SHOW server_version_num")).startswith("16")
    clear_vehicle_mask_jobs(model_engine)
    migrate("downgrade", "base")
    with model_engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
    migrate("upgrade", "head")
    with model_engine.connect() as connection:
        assert set(Base.metadata.tables) <= set(inspect(connection).get_table_names())
    migrate("check")


@pytest.mark.parametrize("field,value", [
    ("token_hash", "not-a-hash"), ("token_hash", None), ("purpose", "seller"),
    ("state", "imaginary"), ("listing_version", 0), ("listing_text", "x" * 10001),
    ("consent_version", ""), ("consent_at", None),
])
def test_session_constraints(db: Connection, field: str, value):
    s = seed(db)["session"]
    rejects(db, update(Session).where(Session.id == s["id"]).values(**{field: literal(value) if field in {"state", "purpose", "concept", "component_id"} else value}))


@pytest.mark.parametrize("field,value", [
    ("part_no", 0), ("part_no", 9), ("zone", "passenger_side"), ("state", "imaginary"),
    ("state", "failed"), ("raw_sha256", "bad"),
])
def test_part_constraints(db: Connection, field: str, value):
    p = seed(db)["part"]
    rejects(db, update(Part).where(Part.id == p["id"]).values(**{field: literal(value) if field in {"state", "purpose", "concept", "component_id"} else value}))


@pytest.mark.parametrize("field,value", [
    ("severity", 0), ("severity", 6), ("severity", None), ("status", "candidate"),
    ("confidence", -0.1), ("confidence", 1.1), ("frame_index", -1), ("frame_width", 0),
    ("bbox", {}), ("bbox", {"x": 0.9, "y": 0, "w": 0.2, "h": 1}),
    ("bbox", {"x": 0, "y": 0, "w": 0, "h": 1}),
    ("bbox", {"x": "bad", "y": 0, "w": 1, "h": 1}),
    ("concept", "brake_health"), ("component_id", "invented"), ("flags", {}),
])
def test_finding_constraints(db: Connection, field: str, value):
    f = seed(db)["finding"]
    rejects(db, update(Finding).where(Finding.id == f["id"]).values(**{field: literal(value) if field in {"state", "purpose", "concept", "component_id"} else value}))


@pytest.mark.parametrize("which", ["part", "run", "concept", "frame", "revision", "claim_frame", "purchase", "entitlement", "model_call", "active_upload"])
def test_cross_session_references_rejected(db: Connection, which: str):
    a, b = seed(db), seed(db)
    if which == "part":
        statement = update(PipelineRun).where(PipelineRun.id == a["run"]["id"]).values(part_id=b["part"]["id"])
    elif which == "run":
        statement = update(ConceptRun).where(ConceptRun.id == a["concept"]["id"]).values(run_id=b["run"]["id"])
    elif which == "concept":
        statement = update(Finding).where(Finding.id == a["finding"]["id"]).values(concept_run_id=b["concept"]["id"])
    elif which == "frame":
        statement = update(Finding).where(Finding.id == a["finding"]["id"]).values(frame_key=b["frame"]["key"])
    elif which == "revision":
        statement = update(Report).where(Report.id == a["report"]["id"]).values(current_revision_id=b["revision"]["id"])
    elif which == "claim_frame":
        statement = insert(ClaimEvidence).values(claim_id=a["claim"]["id"], finding_id=a["finding"]["id"],
            report_revision_id=a["revision"]["id"], session_id=a["session"]["id"], frame_key=b["frame"]["key"])
    elif which == "purchase":
        statement = update(Purchase).where(Purchase.id == a["purchase"]["id"]).values(report_id=b["report"]["id"])
    elif which == "entitlement":
        statement = insert(Entitlement).values(session_id=a["session"]["id"], report_id=a["report"]["id"], source_purchase_id=b["purchase"]["id"])
    elif which == "model_call":
        statement = insert(ModelCall).values(session_id=a["session"]["id"], job_id=b["job"]["id"],
            attempt_id=uuid4(), model="fixture", price_version="synthetic", requested_units={}, reserved_cost=0)
    else:
        upload = row(db, Upload, session_id=b["session"]["id"], part_id=b["part"]["id"], object_key=uuid4().hex,
                     provider_upload_id=uuid4().hex, expected_bytes=1, content_type="video/mp4")
        statement = update(Part).where(Part.id == a["part"]["id"]).values(active_upload_id=upload["id"])
    rejects(db, statement)


@pytest.mark.parametrize("which", ["part", "run", "concept", "finding", "report", "revision", "purchase", "job", "claim", "request_key", "stripe_event", "budget"])
def test_duplicate_natural_keys_rejected(db: Connection, which: str):
    s = seed(db)
    model = {"part": Part, "run": PipelineRun, "concept": ConceptRun, "finding": Finding,
             "report": Report, "revision": ReportRevision, "purchase": Purchase, "job": Job, "claim": Claim}.get(which)
    if model:
        values = dict(s[which])
        values["id"] = uuid4()
        if which == "purchase":
            db.execute(update(Purchase).where(Purchase.id == s["purchase"]["id"]).values(checkout_id="cs_test_duplicate"))
            values["checkout_id"] = "cs_test_duplicate"
        statement = insert(model).values(**values)
    elif which == "request_key":
        values = dict(authority_id=s["session"]["id"], route="/finalize", key="key", request_hash=H,
                      response={"state": "finalized"}, expires_at=NOW)
        row(db, RequestKey, **values)
        statement = insert(RequestKey).values(**values)
    elif which == "stripe_event":
        row(db, StripeEvent, event_id="evt_test", event_type="ignored", payload={})
        statement = insert(StripeEvent).values(event_id="evt_test", event_type="ignored", payload={})
    else:
        row(db, BudgetBucket, scope_key="day:test", limit_cents=0)
        statement = insert(BudgetBucket).values(scope_key="day:test", limit_cents=0)
    rejects(db, statement)


def test_upload_partial_unique_and_nullable_provider_ids(db: Connection):
    s = seed(db)
    values = dict(session_id=s["session"]["id"], part_id=s["part"]["id"], object_key=uuid4().hex,
                  provider_upload_id=uuid4().hex, expected_bytes=8, content_type="video/mp4")
    first = row(db, Upload, **values)
    rejects(db, insert(Upload).values(**{**values, "object_key": uuid4().hex, "provider_upload_id": uuid4().hex}))
    db.execute(update(Upload).where(Upload.id == first["id"]).values(state="aborted"))
    row(db, Upload, **{**values, "object_key": uuid4().hex, "provider_upload_id": uuid4().hex})
    # Pending purchases must be insertable before Checkout creates a provider ID.
    row(db, Purchase, session_id=s["session"]["id"], report_id=s["report"]["id"])


@pytest.mark.parametrize("field", ["limit_cents", "reserved_cents", "spent_cents"])
def test_negative_budget_rejected(db: Connection, field: str):
    rejects(db, insert(BudgetBucket).values(**{"scope_key": "test", "limit_cents": 1, field: -1}))


@pytest.mark.parametrize("values", [{"expected_amount": -1}, {"expected_amount": 0}, {"currency": "eur"}, {"state": "paid"}])
def test_purchase_money_and_payment_state(db: Connection, values: dict):
    s = seed(db)
    rejects(db, update(Purchase).where(Purchase.id == s["purchase"]["id"]).values(**values))


def test_same_session_other_frame_cannot_support_claim(db: Connection):
    s = seed(db)
    frame = row(db, Artifact, session_id=s["session"]["id"], kind="frame", key=uuid4().hex, sha256=H)
    values = dict(claim_id=s["claim"]["id"], finding_id=s["finding"]["id"], report_revision_id=s["revision"]["id"], session_id=s["session"]["id"])
    rejects(db, insert(ClaimEvidence).values(**values, frame_key=frame["key"]))
    row(db, ClaimEvidence, **values, frame_key=s["frame"]["key"])


def test_model_call_units_are_integer_micro_usd(db: Connection):
    s = seed(db)
    values = dict(session_id=s["session"]["id"], job_id=s["job"]["id"], attempt_id=uuid4(),
                  model="fixture", price_version="synthetic", requested_units={}, reserved_cost=100)
    rejects(db, insert(ModelCall).values(**{**values, "reserved_cost": -1}))
    row(db, ModelCall, **values)
    rejects(db, insert(ModelCall).values(**values))


def test_no_secret_idempotency_response(db: Connection):
    s = seed(db)
    rejects(db, insert(RequestKey).values(authority_id=s["session"]["id"], route="/create", key="test",
        request_hash=H, response={"session_token": "must-not-persist"}, expires_at=NOW))


def test_ready_snapshot_requires_manifest_and_raw_never_approved(db: Connection):
    s = seed(db)
    rejects(db, update(ReportRevision).where(ReportRevision.id == s["revision"]["id"]).values(state="ready"))
    rejects(db, update(Artifact).where(Artifact.id == s["frame"]["id"]).values(privacy_state="approved"))


def test_orm_relationship_uses_canonical_zone(db: Connection):
    s = seed(db)
    with ORMSession(bind=db) as orm:
        finding = orm.get(Finding, s["finding"]["id"])
        assert finding.zone.value == "driver_side"
        assert finding.part_no == 1
        assert len(orm.get(Session, s["session"]["id"]).parts) == 1


def test_string_job_subject_accepts_stripe_event(db: Connection):
    s = seed(db)
    db.execute(update(Job).where(Job.id == s["job"]["id"]).values(subject_kind="stripe_event", subject_id="evt_synthetic_123", stage="billing_event"))
    assert db.scalar(select(Job.subject_id).where(Job.id == s["job"]["id"])) == "evt_synthetic_123"



def test_pipeline_identity_ignores_config_json_order_and_null_part(db: Connection):
    s = seed(db)
    values = dict(session_id=s["session"]["id"], part_id=None, subject_key="session:" + uuid4().hex,
                  input_hash=H, config_hash=H, config_hashes={"a": H, "b": "b" * 64})
    row(db, PipelineRun, **values)
    rejects(db, insert(PipelineRun).values(**{**values, "config_hash": "b" * 64,
                                            "config_hashes": {"b": "b" * 64, "a": H}}))


def test_lease_required_and_actual_cost_nonnegative(db: Connection):
    s = seed(db)
    rejects(db, update(Job).where(Job.id == s["job"]["id"]).values(state="running"))
    rejects(db, update(Job).where(Job.id == s["job"]["id"]).values(attempts=4))
    values = dict(session_id=s["session"]["id"], job_id=s["job"]["id"], attempt_id=uuid4(),
                  model="fixture", price_version="synthetic", requested_units={}, reserved_cost=0)
    rejects(db, insert(ModelCall).values(**values, actual_cost=-1))
    rejects(db, insert(ModelCall).values(**values, latency_ms=-1))


def test_raw_key_must_belong_to_same_part_upload(db: Connection):
    a, b = seed(db), seed(db)
    upload = row(db, Upload, session_id=b["session"]["id"], part_id=b["part"]["id"], object_key=uuid4().hex,
                 provider_upload_id=uuid4().hex, expected_bytes=1, content_type="video/mp4")
    rejects(db, update(Part).where(Part.id == a["part"]["id"]).values(raw_key=upload["object_key"]))
    db.execute(update(Part).where(Part.id == b["part"]["id"]).values(raw_key=upload["object_key"]))


def test_only_billing_jobs_allow_unresolved_session(db: Connection):
    values = dict(subject_kind="stripe_event", subject_id="evt_unresolved_test", stage="billing_event",
                  input_hash=H, config_hash=H, idempotency_key="c" * 64)
    row(db, Job, **values)
    rejects(db, insert(Job).values(**{**values, "subject_kind": "part_run", "idempotency_key": "d" * 64}))
    rejects(db, insert(Job).values(**{**values, "stage": "ingest", "idempotency_key": "e" * 64}))


def test_running_job_requires_attempt_start_clock(db: Connection):
    s = seed(db)
    values = dict(state="running", worker_id="synthetic-worker", lease_until=NOW, lease_token=uuid4())
    rejects(db, update(Job).where(Job.id == s["job"]["id"]).values(**values))
    db.execute(update(Job).where(Job.id == s["job"]["id"]).values(**values, attempt_started_at=NOW))
