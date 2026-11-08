"""Synthetic dashboard evidence through PostgreSQL leases, storage and the report barrier."""
from uuid import UUID

import pytest
from app.contracts.enums import PartState
from app.jobs.queue import Failure
from app.jobs.worker import StageFailure
from app.models import Artifact, Observation, PipelineRun, Report
from app.pipeline.dash_store import DASH_ASSESSMENT
from app.pipeline.vehicle_mask import SyntheticVehicleSegmenter
from app.services.snapshots import build_free_report, build_paid_report
from tests.test_ingest import accept, clip, media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import (Pipe, finalize, fingerprint, isolate, jobs, kill, part, rows,
                                      setup)  # noqa: F401
from tests.test_report_stages import Rig
from tests.test_uploads import bucket, make, session_factory  # noqa: F401


def pending(make, media_dir, scratch_root, *, factory=None):
    h = setup(make, scratch_root)
    pipe = Pipe(h, factory=factory or (lambda _: SyntheticVehicleSegmenter()))
    accept(h, clip(media_dir), part_no=5)
    assert pipe.step()  # ingest -> read_dash queued, dashboard must not drain yet
    assert part(h, 5).state == PartState.PROCESSING
    assert len(jobs(h, "read_dash")) == 1
    assert len(rows(h, Observation, Observation.kind == DASH_ASSESSMENT)) == 0
    return h, pipe


def test_dash_waits_then_freezes_once_and_unblocks_report(make, media_dir, scratch_root):
    h, pipe = pending(make, media_dir, scratch_root)
    finalize(h)
    assert not jobs(h, "claims")
    pipe.reconcile()
    assert len(jobs(h, "read_dash")) == 1  # reconciliation is idempotent
    pipe.drain()
    assert part(h, 5).state == PartState.PROCESSED
    markers = rows(h, Observation, Observation.kind == DASH_ASSESSMENT)
    assert len(markers) == 1 and markers[0].value["state"] == "assessed"
    assert markers[0].value["provenance"] == "synthetic"
    crop = rows(h, Artifact, Artifact.key == markers[0].frame_key)[0]
    assert crop.sha256 == markers[0].value["crop_sha256"] and crop.privacy_state == "pending"
    assert len(jobs(h, "claims")) == 1
    before = fingerprint(h)
    pipe.reconcile()
    assert fingerprint(h) == before
    rig = Rig(h, pipe)
    rig.install(storage=h.storage)
    pipe.drain()
    free, paid = build_free_report(rig.snapshot()), build_paid_report(rig.snapshot())
    assert free.dashboard.model_dump() == {"checked": True, "flag_count": 0}
    assert paid.dashboard.assessment.state == "assessed" and not paid.dashboard.evidence_available
    for leak in ("crop_sha256", "dash_cluster", "lit", "config_hash"):
        assert leak not in free.dashboard.model_dump_json()
    # a dash reading alone is no mechanical pass: the missing OBD snapshot and engine pod cost 25 + 20
    assert free.categories.mechanical.state == "assessed" and float(free.categories.mechanical.score) == 100 - 25 - 20
    slug = rows(h, Report)[0].share_slug
    assert h.http.get(f"/api/v1/reports/{slug}/dashboard", headers=h.headers).status_code == 404


def test_dead_dash_job_recovers_as_unreadable_not_clean(make, media_dir, scratch_root):
    h, pipe = pending(make, media_dir, scratch_root)
    kill(h, jobs(h, "read_dash")[0].id)
    pipe.reconcile()
    assert part(h, 5).state == PartState.PROCESSED
    frozen = rows(h, Observation, Observation.kind == DASH_ASSESSMENT)[0].value
    assert frozen["state"] == "not_readable" and frozen["reason"] == "dash_job_failed"
    assert frozen["deduction"] == "0"


def test_missing_dash_cluster_is_never_clean(make, media_dir, scratch_root):
    from app.pipeline.segment import ScriptedSegmenter
    h, pipe = pending(make, media_dir, scratch_root, factory=lambda _: ScriptedSegmenter())
    pipe.drain()
    frozen = rows(h, Observation, Observation.kind == DASH_ASSESSMENT)[0].value
    assert frozen["state"] == "not_readable" and frozen["reason"] == "cluster_not_found"
    assert not frozen["lit"]


def test_retryable_dash_failure_retries_then_finishes_unreadable(make, media_dir, scratch_root):
    from app.pipeline.segment import ScriptedSegmenter
    h, pipe = pending(make, media_dir, scratch_root,
        factory=lambda _: ScriptedSegmenter(failures={"dash_cluster": "provider_timeout"}))
    lease = pipe.queue.claim("dash-retry", ["read_dash"])
    with pytest.raises(StageFailure) as caught:
        pipe.registry.handler("read_dash")(lease)
    assert caught.value.failure.retryable
    assert not rows(h, Observation, Observation.kind == DASH_ASSESSMENT)
    pipe.queue.fail(lease, Failure("PROVIDER_TIMEOUT", retry_after_seconds=0))
    pipe.drain()
    frozen = rows(h, Observation, Observation.kind == DASH_ASSESSMENT)[0].value
    assert frozen["state"] == "not_readable" and frozen["reason"] == "dash_provider_timeout"
    assert jobs(h, "read_dash")[0].attempts == 3


def test_stale_dash_publication_cannot_publish(make, media_dir, scratch_root):
    h, pipe = pending(make, media_dir, scratch_root)
    lease = pipe.queue.claim("dash-stale", ["read_dash"])
    publication = pipe.registry.handler("read_dash")(lease)
    kill(h, lease.job_id)
    assert not pipe.queue.complete(lease, publication.publish, publication.downstream)
    assert not rows(h, Observation, Observation.kind == DASH_ASSESSMENT)
    assert not [a for a in rows(h, Artifact) if "/dash/" in a.key]


def test_dash_config_drift_is_unreadable_and_does_not_call_provider(make, media_dir, scratch_root):
    h, pipe = pending(make, media_dir, scratch_root)
    with h.db.begin() as db:
        run = db.get(PipelineRun, UUID(jobs(h, "read_dash")[0].subject_id))
        run.config_hashes = {**run.config_hashes, "dash": "b" * 64}
    pipe.drain()
    frozen = rows(h, Observation, Observation.kind == DASH_ASSESSMENT)[0].value
    assert frozen["state"] == "not_readable" and frozen["reason"] == "dash_config_mismatch"
    assert not frozen["crop_sha256"]


def test_dashboard_reader_reserves_before_dispatch_and_settles_usage(make, media_dir, scratch_root):
    from app.adapters.dash_reader import FIXTURE_RESPONSE, accept
    from app.pipeline.dash_handler import DashHandler
    h, pipe = pending(make, media_dir, scratch_root)
    calls = []

    class Budget:
        def reserve(self, request):
            assert request.units.input_tokens == 42 and request.units.output_tokens == 8192
            calls.append("reserve")

        def mark_dispatched(self, attempt, token):
            calls.append("dispatch")
            return True

        def settle(self, attempt, **kwargs):
            assert kwargs["actual_units"].input_tokens == 20
            assert kwargs["actual_units"].output_tokens == 10
            calls.append("settle")

    class Reader:
        requires_budget = True

        def read(self, image, *, grant):
            assert grant(42) is True
            assert calls == ["reserve", "dispatch"]
            calls.append("read")
            return accept(FIXTURE_RESPONSE, provider="fixture", model="fixture", provenance="synthetic",
                input_tokens=20, output_tokens=10)

    lease = pipe.queue.claim("dash-budget", ["read_dash"])
    handler = DashHandler(h.db, h.storage, h.settings, pipe.ctx,
        segmenter_factory=lambda _: SyntheticVehicleSegmenter(), reader=Reader(), budget_service=Budget())
    publication = handler(lease)
    assert calls == ["reserve", "dispatch", "read", "settle"]
    assert pipe.queue.complete(lease, publication.publish, publication.downstream)


def test_dashboard_reading_compares_obd_mil(make, media_dir, scratch_root):
    from tests.test_sources_domain import obd_data
    from tests.test_category_scores import envelope, post
    h, pipe = pending(make, media_dir, scratch_root)
    post(h, envelope("obd_snapshot", obd_data(mil_on=True)))
    pipe.drain()
    frozen = rows(h, Observation, Observation.kind == DASH_ASSESSMENT)[0].value
    assert frozen["state"] == "assessed" and frozen["flags"] == ["mil_not_visible"]
    assert frozen["deduction"] == "0"  # disagreement is a flag, not a deduction
