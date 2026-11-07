"""Stage-1 live semantics with synthetic inputs; no external provider requests."""
from uuid import uuid4

import pytest

from app.adapters.meta import FrameRef, MaskRaster, MediaRef, ProviderUsage, SegmentObservation, SegmentResult
from app.contracts.evidence import BoundingBox
from app.pipeline.segment import SegmentHandler


def test_vehicle_coverage_accepts_typed_box(monkeypatch):
    from types import SimpleNamespace

    # Exercise the normalized provider boundary, not an untyped dict substitute.
    media = MediaRef(media_key="synthetic/proxy", kind="video", stored_width=10, stored_height=10,
                     frames=(FrameRef(frame_index=0, timestamp_ms=0, frame_key="synthetic/frame"),))
    result = SegmentResult(request_id=uuid4(), prompt_id="vehicle", provenance="synthetic",
        usage=ProviderUsage(), observations=(SegmentObservation(track_id="1", frame_index=0,
            timestamp_ms=0, frame_key="synthetic/frame", frame_width=10, frame_height=10,
            bbox=BoundingBox(x=0.2, y=0.3, w=0.2, h=0.2),
            mask=MaskRaster(width=2, height=2, pixels=b"\x01\x01\x01\x01"), confidence=None),))
    handler = object.__new__(SegmentHandler)
    handler._vehicle_cache = {}
    monkeypatch.setattr(handler, "_run_request", lambda *args, **kwargs: result)
    plan = SimpleNamespace(mode="video", part_no=1, run_id=uuid4())
    actual = handler._vehicle_coverage(None, plan, None, media, uuid4())
    assert actual is not None
    assert {index for index, value in enumerate(actual[0]) if value} == {32, 33, 42, 43}


def test_pending_live_empty_result_cannot_be_assessed(make, media_dir, scratch_root):
    from app.contracts.enums import CoverageState
    from app.pipeline.orchestration import live_orchestration_context
    from app.pipeline.verify import LivePendingVerifier
    from tests.test_orchestration import Pipe, coverage, setup, upload

    h = setup(make, scratch_root)
    pipe = Pipe(h, ctx=live_orchestration_context(), verifier=LivePendingVerifier())
    upload(h, media_dir, 1)
    pipe.drain()
    assert coverage(h)
    assert all(row.state == CoverageState.PARTIAL for row in coverage(h).values())
    assert all("verification_unavailable" in row.reason for row in coverage(h).values())
    free, paid = frozen_reports(h, pipe)
    assert free.part1_score is None and free.findings == ()
    assert paid.scores.headline is None
    assert all(zone.score is None for zone in paid.scores.zones)


def frozen_reports(h, pipe):
    from app.models import ReportRevision
    from app.pipeline.report_stages import install_report_handlers
    from app.services.snapshots import build_reports, load_snapshot
    from tests.test_orchestration import finalize, rows

    install_report_handlers(pipe.registry, h.db, h.settings)
    finalize(h)
    pipe.drain()
    revision_id = rows(h, ReportRevision)[0].id
    with h.db() as db:
        return build_reports(load_snapshot(db, db.get(ReportRevision, revision_id)))


@pytest.mark.parametrize("vehicle", ["inside", "outside", "empty", "failed"])
def test_containment_preserves_candidate_for_validated_verdict(make, media_dir, scratch_root, monkeypatch, vehicle):
    from app.adapters.meta import ProviderFailure
    from app.contracts.enums import FindingStatus
    from app.models import Observation
    from app.pipeline.orchestration import VERIFICATION_PROVENANCE, live_orchestration_context
    from app.pipeline.verify import LivePendingVerifier
    from tests.test_orchestration import Pipe, findings, rows, setup, upload

    class SyntheticMasks:
        requires_budget = True  # exercise the containment branch; dispatch below is a test fake

        def segment(self, media, prompt_id, request_id):
            observations = ()
            if prompt_id == "vehicle" and vehicle == "failed":
                raise ProviderFailure("provider_timeout")
            if prompt_id == "rust_a" or (prompt_id == "vehicle" and vehicle != "empty"):
                left = 0.5 if prompt_id == "vehicle" and vehicle == "outside" else 0.1
                box = BoundingBox(x=left, y=left, w=0.2, h=0.2)
                width, height = media.display_size
                mw = round((box.x + box.w) * width) - round(box.x * width)
                mh = round((box.y + box.h) * height) - round(box.y * height)
                frame = media.frames[0]
                observations = (SegmentObservation(track_id="1", frame_index=frame.frame_index,
                    frame_key=frame.frame_key, timestamp_ms=frame.timestamp_ms, frame_width=width, frame_height=height,
                    bbox=box, mask=MaskRaster(width=mw, height=mh, pixels=b"\x01" * (mw * mh)), confidence=None),)
            return SegmentResult(request_id=request_id, prompt_id=prompt_id, provenance="synthetic",
                                 observations=observations, usage=ProviderUsage())

    h = setup(make, scratch_root)
    pipe = Pipe(h, ctx=live_orchestration_context(), verifier=LivePendingVerifier(), factory=lambda _: SyntheticMasks())
    handler = pipe.registry.handler("segment")
    monkeypatch.setattr(handler, "_run_request",
        lambda lease, plan, segmenter, media, request_id, prompt_id=None:
            segmenter.segment(media, prompt_id or plan.prompt_id, request_id))
    upload(h, media_dir, 1)
    pipe.drain()
    found = findings(h)
    assert len(found) == 1
    assert found[0].status == FindingStatus.UNVERIFIED and found[0].severity is None
    assert found[0].mask_key is not None
    assert len(rows(h, Observation, Observation.kind == VERIFICATION_PROVENANCE)) == 1
    if vehicle == "outside":
        assert "containment is provisional" in found[0].evidence_note
    free, paid = frozen_reports(h, pipe)
    assert free.part1_score is None
    assert len(paid.candidates) == 1 and paid.findings == ()
    assert all(zone.score is None for zone in paid.scores.zones)


# Reuse isolated PostgreSQL/Floci/ffmpeg fixtures, with no paid provider access.
from tests.test_ingest import media_dir, scratch_root  # noqa: E402,F401
from tests.test_orchestration import isolate  # noqa: E402,F401
from tests.test_uploads import bucket, make, session_factory  # noqa: E402,F401
