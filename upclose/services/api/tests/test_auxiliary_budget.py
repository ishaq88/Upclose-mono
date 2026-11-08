"""Priced accounting for advisory/narration/voice-guide calls: lease, deletion, deadline, unknown-charge behavior.

Fake money on an isolated PostgreSQL database; no provider is ever contacted (prepared requests are stand-ins).
"""
from dataclasses import dataclass, replace
from datetime import timedelta
import os
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.jobs.queue import Lease
from app.models import BudgetBucket, Job, ModelCall, Session
from app.services.auxiliary_budget import AuxiliaryDispatcher
from app.services.budgets import BudgetConfig, BudgetService


@pytest.fixture(scope="module")
def factory():
    url = os.environ.get("BUDGET_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires isolated BUDGET_TEST_DATABASE_URL PostgreSQL database")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing budget tests outside explicitly named upclose_test_* PostgreSQL database")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env={**os.environ, "DATABASE_URL": url})
    engine = create_engine(url, connect_args={"connect_timeout": 3}, pool_size=5)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def db(factory):
    with factory.begin() as connection:
        connection.execute(text("TRUNCATE sessions, budget_buckets CASCADE"))
    return factory


def live(**changes) -> BudgetConfig:
    return BudgetConfig(model_mode="live", verified_standard_access=True, standard_access_receipt="synthetic-test-only",
                        elevenlabs_access_receipt="synthetic-test-only", **changes)


def make_lease(db, owner_id=None) -> Lease:
    with db.begin() as connection:
        now = connection.scalar(select(func.clock_timestamp()))
        if owner_id is None:
            owner = Session(token_hash=uuid4().hex * 2, consent_version="synthetic-v1", consent_at=now)
            connection.add(owner)
            connection.flush()
            owner_id = owner.id
        job = Job(session_id=owner_id, subject_kind="session", subject_id=str(owner_id), stage="narrate",
                  input_hash="0" * 64, config_hash="0" * 64, idempotency_key=uuid4().hex * 2, state="running",
                  attempts=1, lease_token=uuid4(), lease_until=now + timedelta(minutes=2), attempt_started_at=now,
                  worker_id="synthetic-test")
        connection.add(job)
        connection.flush()
        return Lease(job.id, owner_id, job.lease_token, 1, "narrate", "session", str(owner_id),
                     now + timedelta(minutes=2), now + timedelta(minutes=5))


@dataclass(frozen=True)
class Units:
    input_tokens: int = 0
    output_tokens: int = 0
    characters: int = 0


def voice(key="k1", characters=2500, kind="narration", **changes):
    return SimpleNamespace(**{"kind": kind, "provider": "elevenlabs", "model": "eleven_v4", "logical_key": key,
                              "units": Units(characters=characters), **changes})


def advisory(key="a1", **changes):
    return SimpleNamespace(**{"kind": "advisory", "provider": "meta", "model": "muse-spark-1.3", "logical_key": key,
                              "units": Units(input_tokens=16384, output_tokens=2048), **changes})


def evidence(outcome="billed", **changes):
    return SimpleNamespace(**{"outcome": outcome, "input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
                              "characters": None, "provider_request_id": None, "http_status": None, "failure_code": None,
                              "latency_ms": 12, "body_sha256": "0" * 64, **changes})


def rows(db):
    with db() as connection:
        return (list(connection.scalars(select(ModelCall).order_by(ModelCall.created_at))),
                list(connection.scalars(select(BudgetBucket).order_by(BudgetBucket.scope_key))))


def test_voice_grant_reserves_twenty_cents_and_keeps_the_unknown_charge_after_success(db):
    lease = make_lease(db)
    dispatcher = AuxiliaryDispatcher(BudgetService(db, live()), lease)
    prepared = voice()
    assert dispatcher.grant(prepared) is True
    calls, buckets = rows(db)
    assert calls[0].state == "unknown" and calls[0].model == "elevenlabs/eleven_v4" and calls[0].reserved_cost == 200000
    assert calls[0].price_version == "elevenlabs-docs-2026-11-08"
    dispatcher.record(prepared, evidence("billed", characters=2500))
    calls, buckets = rows(db)
    assert calls[0].state == "unknown" and calls[0].actual_cost is None  # no verified billing evidence: never settled
    assert calls[0].requested_units["failure"]["code"] == "billing_unverified"
    assert [b.reserved_cents for b in buckets] == [20, 20] and [b.spent_cents for b in buckets] == [0, 0]


def test_a_retry_with_a_new_lease_never_buys_the_same_request_twice(db):
    first = make_lease(db)
    assert AuxiliaryDispatcher(BudgetService(db, live()), first).grant(voice()) is True
    retry = make_lease(db, first.session_id)
    again = AuxiliaryDispatcher(BudgetService(db, live()), retry)
    assert again.grant(voice()) is False and again.denied_reason == "already_attempted"
    assert len(rows(db)[0]) == 1
    assert [b.reserved_cents for b in rows(db)[1]] == [20, 20]
    assert again.grant(voice(key="a-different-request", characters=100)) is True  # a different request is its own row


def test_token_provider_settles_from_reported_usage_even_when_the_answer_was_invalid(db):
    lease = make_lease(db)
    dispatcher = AuxiliaryDispatcher(BudgetService(db, live()), lease)
    prepared = advisory()
    assert dispatcher.grant(prepared) is True
    dispatcher.record(prepared, evidence("billed_invalid", input_tokens=1000, cached_input_tokens=100, output_tokens=100,
                                         provider_request_id="resp-synthetic"))
    calls, buckets = rows(db)
    assert calls[0].state == "succeeded" and calls[0].actual_cost == 1565 and calls[0].provider_request_id == "resp-synthetic"
    assert [b.reserved_cents for b in buckets] == [0, 0] and [b.spent_cents for b in buckets] == [1, 1]


@pytest.mark.parametrize("ev", [evidence("unknown", http_status=504, failure_code="timeout"),
                                evidence("billed"),  # tokens missing: unknown, never zero
                                evidence("billed", input_tokens=1, output_tokens=None)])
def test_unknown_or_unmeasured_usage_stays_reserved(db, ev):
    lease = make_lease(db)
    dispatcher = AuxiliaryDispatcher(BudgetService(db, live()), lease)
    prepared = advisory()
    assert dispatcher.grant(prepared) is True
    dispatcher.record(prepared, ev)
    calls, buckets = rows(db)
    assert calls[0].state == "unknown" and calls[0].actual_cost is None
    assert [b.spent_cents for b in buckets] == [0, 0] and all(b.reserved_cents > 0 for b in buckets)


def test_close_marks_a_granted_but_unrecorded_call_unknown_and_ignores_stranger_records(db):
    lease = make_lease(db)
    dispatcher = AuxiliaryDispatcher(BudgetService(db, live()), lease)
    dispatcher.record(voice(), evidence())  # never granted: no row, no error
    assert rows(db)[0] == []
    assert dispatcher.grant(voice()) is True
    dispatcher.close()
    assert rows(db)[0][0].requested_units["failure"]["code"] == "unrecorded"


def test_lease_deletion_and_deadline_are_enforced_before_any_transport(db):
    lease = make_lease(db)
    budget = BudgetService(db, live())
    assert AuxiliaryDispatcher(budget, replace(lease, token=uuid4())).grant(voice(key="stolen")) is False
    expired = replace(lease, deadline=lease.deadline - timedelta(hours=1))
    denied = AuxiliaryDispatcher(budget, expired)
    assert denied.grant(voice(key="late")) is False and denied.denied_reason == "deadline_expired"
    with db.begin() as connection:
        connection.execute(update(Session).where(Session.id == lease.session_id).values(state="deleting"))
    deleted = AuxiliaryDispatcher(budget, lease)
    assert deleted.grant(voice(key="gone")) is False and deleted.denied_reason == "job_not_eligible"
    assert rows(db) == ([], [])


def test_exhausted_budget_denies_without_dispatch(db):
    lease = make_lease(db)
    dispatcher = AuxiliaryDispatcher(BudgetService(db, live(session_limit_cents=30)), lease)
    assert dispatcher.grant(voice(key="one")) is True
    assert dispatcher.grant(voice(key="two")) is False and dispatcher.denied_reason == "budget_exhausted"
    assert len(rows(db)[0]) == 1


def test_not_live_missing_budget_missing_receipt_and_unapproved_models_never_grant(db):
    lease = make_lease(db)
    assert AuxiliaryDispatcher(None, lease).grant(voice()) is False
    assert AuxiliaryDispatcher(BudgetService(db, BudgetConfig(model_mode="fixture")), lease).grant(voice()) is False
    no_receipt = BudgetConfig(model_mode="live", verified_standard_access=True, standard_access_receipt="synthetic-test-only")
    refused = AuxiliaryDispatcher(BudgetService(db, no_receipt), lease)
    assert refused.grant(voice()) is False and refused.denied_reason == "provider_access_unverified"
    live_budget = BudgetService(db, live())
    for bad in (voice(provider="gemini", model="gemini-3.8-flash"), voice(model="eleven_invented"),
                advisory(model="muse-invented"), voice(provider="elevenlabs", kind="mystery")):
        assert AuxiliaryDispatcher(live_budget, lease).grant(bad) is False
    assert rows(db) == ([], [])


@pytest.mark.parametrize("prepared", [voice(characters=2501), voice(characters=0), advisory(units=Units(input_tokens=16385, output_tokens=2048)),
                                      advisory(units=Units(input_tokens=1, output_tokens=2049)),
                                      advisory(units=Units(input_tokens=1, output_tokens=1, characters=1)),
                                      voice(units=Units(input_tokens=5, characters=5))])
def test_caps_are_enforced_again_at_grant_time(db, prepared):
    assert AuxiliaryDispatcher(BudgetService(db, live()), make_lease(db)).grant(prepared) is False
    assert rows(db) == ([], [])


def test_a_modified_prepared_body_is_refused_by_its_own_verification(db):
    def tampered():
        raise ValueError("body changed after pricing")

    assert AuxiliaryDispatcher(BudgetService(db, live()), make_lease(db)).grant(voice(verify=tampered)) is False
    assert rows(db) == ([], [])


# ---- voice-guide prewarm job (worker pays, API serves cache only) ---------------------------------------------------

from app.jobs.queue import enqueue  # noqa: E402
from app.jobs.worker import StageFailure  # noqa: E402
from app.pipeline.voice_prewarm import VoicePrewarmHandler, prewarm_spec  # noqa: E402

LIVE_VOICE_ENV = {"VOICE_PROVIDER": "elevenlabs", "ELEVENLABS_VOICE_ID": "synthetic-voice", "ELEVENLABS_MODEL_ID": "eleven_v4",
                  "VOICE_PREWARM": "true"}


class FakeGuide:
    """Two catalog lines. A line is paid only when the grant says so; nothing else can reach a transport."""

    def __init__(self, cached=(), fail=False):
        self.cached, self.fail, self.sent, self.results = set(cached), fail, [], {}

    def prewarm(self, *, grant, record):
        for line, characters in (("intro", 120), ("rear", 80)):
            if line in self.cached:
                self.results[line] = "cached"
                continue
            prepared = voice(key=f"guide-{line}", characters=characters, kind="voice_guide")
            if not grant(prepared):
                self.results[line] = "denied"
                continue
            self.sent.append(line)
            if self.fail:
                raise RuntimeError("transport exploded after the grant")
            record(prepared, evidence("billed", characters=characters))
            self.results[line] = "synthesized"
        return self.results


def test_prewarm_spec_exists_only_for_a_live_voice():
    assert prewarm_spec(uuid4(), {"VOICE_PROVIDER": "fixture"}) is None
    assert prewarm_spec(uuid4(), {}) is None
    assert prewarm_spec(uuid4(), {k: v for k, v in LIVE_VOICE_ENV.items() if k != "VOICE_PREWARM"}) is None
    sid = uuid4()
    spec = prewarm_spec(sid, LIVE_VOICE_ENV)
    assert spec.stage == "voice_prewarm" and spec.subject_kind == "session" and spec.subject_id == str(sid)
    assert spec == prewarm_spec(sid, LIVE_VOICE_ENV) and spec.input_hash != prewarm_spec(uuid4(), LIVE_VOICE_ENV).input_hash


def test_prewarm_is_idempotent_per_session_and_refused_for_a_deleting_session(db):
    lease = make_lease(db)
    spec = prewarm_spec(lease.session_id, LIVE_VOICE_ENV)
    with db.begin() as connection:
        first = enqueue(connection, spec)
        assert enqueue(connection, spec) == first
    with db.begin() as connection:
        connection.execute(update(Session).where(Session.id == lease.session_id).values(state="deleting"))
    with pytest.raises(ValueError), db.begin() as connection:
        enqueue(connection, prewarm_spec(lease.session_id, {**LIVE_VOICE_ENV, "ELEVENLABS_VOICE_ID": "other"}))


def test_prewarm_pays_only_missing_lines_through_grants_and_keeps_unknown_charges(db):
    lease = make_lease(db)
    guide = FakeGuide(cached={"rear"})
    handler = VoicePrewarmHandler(db, BudgetService(db, live()), lambda: guide)
    assert handler(lease).publish is None
    assert guide.sent == ["intro"] and guide.results == {"intro": "synthesized", "rear": "cached"}
    calls, buckets = rows(db)
    assert len(calls) == 1 and calls[0].state == "unknown" and calls[0].reserved_cost == 9600  # 120 chars * 80 micro-USD
    assert [b.reserved_cents for b in buckets] == [1, 1]
    handler(replace(lease, token=lease.token))  # same lease replay: the attempted line is not bought twice
    assert len(rows(db)[0]) == 1 and guide.sent == ["intro"]


def test_prewarm_without_connected_budget_is_a_noop_and_never_fails_the_job(db):
    lease = make_lease(db)
    guide = FakeGuide()
    for budget in (None, BudgetService(db, BudgetConfig(model_mode="fixture"))):
        assert VoicePrewarmHandler(db, budget, lambda: guide)(lease).publish is None
    assert guide.sent == [] and rows(db) == ([], [])


def test_prewarm_transport_crash_after_grant_keeps_the_charge_unknown_and_swallows_the_error(db):
    lease = make_lease(db)
    guide = FakeGuide(fail=True)
    VoicePrewarmHandler(db, BudgetService(db, live()), lambda: guide)(lease)
    calls, _ = rows(db)
    assert guide.sent == ["intro"] and len(calls) == 1 and calls[0].state == "unknown"
    assert calls[0].requested_units["failure"]["code"] == "unrecorded"


def test_prewarm_for_a_deleted_session_or_wrong_subject_does_nothing_or_fails_terminally(db):
    lease = make_lease(db)
    guide = FakeGuide()
    with pytest.raises(StageFailure):
        VoicePrewarmHandler(db, BudgetService(db, live()), lambda: guide)(replace(lease, subject_id="someone-else"))
    with db.begin() as connection:
        connection.execute(update(Session).where(Session.id == lease.session_id).values(state="deleting"))
    VoicePrewarmHandler(db, BudgetService(db, live()), lambda: guide)(lease)
    assert guide.sent == [] and rows(db) == ([], [])
