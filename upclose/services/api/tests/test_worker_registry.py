import runpy

import pytest

import app.jobs.worker as worker
from app.jobs.worker import build_registry
from app.settings import Settings


def test_module_entry_point_runs_canonical_module(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: as `__main__`, a second module copy rejected handlers' Publication
    # results as INVALID_INPUT, killing every real ingest job.
    calls: list[str] = []
    monkeypatch.setattr(worker, "main", lambda: calls.append("canonical"))
    runpy.run_module("app.jobs.worker", run_name="__main__")
    assert calls == ["canonical"]


def test_worker_installs_only_implemented_stages() -> None:
    # Construction must not contact storage, Stripe or the database.
    registry = build_registry(Settings(s3_internal_endpoint="invalid"), factory=object())
    assert set(registry.stages) == {"ingest", "source_ingest", "audio_ingest", "vehicle_mask", "segment", "verify", "claims", "score", "narrate", "render", "billing_event", "purge"}


def test_worker_refuses_live_stripe_keys() -> None:
    settings = Settings(payments_mode="test", stripe_secret_key="sk_live_not_a_real_key")
    try:
        build_registry(settings, factory=object())
    except ValueError:
        return
    raise AssertionError("live Stripe key must be refused")
