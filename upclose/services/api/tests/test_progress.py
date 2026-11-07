"""Owner progress endpoint (T05b): Part-1 free verdict, free restriction, report state, auth rules."""
from __future__ import annotations

import json
import shutil
from collections import Counter

import pytest
from sqlalchemy import func, select

from app.contracts.api import ProgressResponse
from app.contracts.enums import PartState, ReportState
from app.jobs.queue import Failure
from app.models import Finding, Report, ReportRevision
from app.pipeline.segment import ScriptedSegmenter
from app.pipeline.verify import FixtureVerifier
from tests import media_fixtures as fx
from tests.test_ingest import accept, clip, media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import (Pipe, findings, finalize, isolate, jobs, part, rows, setup, track,  # noqa: F401
                                      upload)
from tests.test_uploads import Harness, bucket, make, session_factory  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")


def progress(h: Harness, headers=None):
    return h.http.get(f"/api/v1/sessions/{h.session_id}/progress", headers=h.headers if headers is None else headers)


def by_part(scripts: dict[int, dict]):
    return lambda part_no: ScriptedSegmenter(scripts.get(part_no, {}))


def severity_by_part(candidate, context, rubric) -> dict:
    return {"finding_id": str(candidate.id), "verdict": "confirmed", "concept": candidate.concept.value,
            "severity": 4 if candidate.part_no == 2 else 2, "evidence_note": "synthetic", "flags": ["high_risk_test"]
            if candidate.part_no == 2 else []}


PART1 = {"rust_a": track((0.3, 0.10, 0.10, 0.20, 0.20, 0.9))}
PART2 = {"rust_a": track((0.3, 0.10, 0.10, 0.20, 0.20, 0.9)), "dent": track((0.6, 0.50, 0.50, 0.20, 0.20, 0.9))}


def test_part_one_free_verdict_exists_before_finalize_and_creates_no_report(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, factory=by_part({1: PART1}))
    upload(h, media_dir, 1)
    before = progress(h)
    assert before.status_code == 200 and before.json()["free_verdict"] is None  # not processed yet
    assert before.json()["parts"][0]["state"] == "uploaded"
    pipe.drain()
    response = progress(h)
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    body = ProgressResponse.model_validate(response.json())
    assert body.state.value == "capturing" and body.report is None
    verdict = body.free_verdict
    # The verdict carries no report identity; before finalize there is no report at all.
    assert verdict is not None and not hasattr(verdict, "slug") and not hasattr(verdict, "revision_id")
    assert float(verdict.part1_score) == 94.0  # one severity-2 rust finding: 100 - 2 * 3
    finding, = verdict.findings
    assert (finding.concept.value, finding.severity, finding.zone.value, finding.evidence_available) == ("rust", 2, "driver_side", False)
    assert [(c.zone.value, c.state.value) for c in verdict.coverage] == [("driver_side", "assessed")]
    assert "provisional" in verdict.part1_summary.lower() and verdict.locked_zones == ()
    assert rows(h, Report) == [] and rows(h, ReportRevision) == []  # no report shell or revision before finalize
    assert [p["state"] for p in body.model_dump(mode="json")["parts"]][0] == "processed"


def test_progress_exposes_active_upload_and_failed_part_reason(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, script={})
    upload_id = h.begin(1000, part_no=2)
    parts = progress(h).json()["parts"]
    assert parts[1]["active_upload_id"] == str(upload_id) and parts[1]["state"] == "uploading"
    accept(h, fx.make_garbage(media_dir / "bad.mp4").path, part_no=3)
    pipe.drain()
    failed = progress(h).json()["parts"][2]
    assert failed["state"] == "failed" and failed["reason"].startswith("MEDIA_") and failed["active_upload_id"] is None


def test_owner_progress_before_purchase_contains_no_other_zone_evidence(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    pipe = Pipe(h, factory=by_part({1: PART1, 2: PART2}), verifier=FixtureVerifier({"rust": severity_by_part, "dent": severity_by_part}))
    upload(h, media_dir, 1, 2)
    pipe.drain()
    assert part(h, 2).state == PartState.PROCESSED
    other = [f for f in findings(h) if f.part_id == part(h, 2).id]
    mine = [f for f in findings(h) if f.part_id == part(h, 1).id]
    assert len(other) == 2 and len(mine) == 1
    for state in ("capturing", "finalized"):
        if state == "finalized":
            finalize(h)
        text = progress(h).text
        body = json.loads(text)
        for finding in other:
            assert str(finding.id) not in text  # no other-zone finding IDs
        assert [item["id"] for item in body["free_verdict"]["findings"]] == [str(mine[0].id)]
        assert {item["severity"] for item in body["free_verdict"]["findings"]} == {2}
        assert "high_risk_test" not in text and "claims" not in body["free_verdict"] and "narration" not in text
        assert body["free_verdict"]["locked_zones"] == [{"zone": "passenger_side", "finding_count": 2, "locked": True}]
        # the whole payload is exactly the free shape: no other-zone scores, costs or pins anywhere
        assert set(body) == {"state", "parts", "free_verdict", "report"}
        assert set(body["free_verdict"]) == {"coverage", "part1_score", "findings", "locked_zones", "part1_summary"}
        assert not any(key in text for key in ("repairs", "headline", "scores", "low_cents", "frame_key", "mask_key"))
        assert {c["zone"] for c in body["free_verdict"]["coverage"]} == {"driver_side"}


def test_progress_follows_owner_auth_rules(make, media_dir, scratch_root) -> None:
    h = setup(make, scratch_root)
    url = f"/api/v1/sessions/{h.session_id}/progress"
    assert h.http.get(url).status_code == 401
    assert h.http.get(url, headers={"Authorization": "Bearer " + "x" * 30}).status_code == 401
    other = h.other_owner()
    assert h.http.get(url, headers=other).status_code == 404  # authenticated, but not this owner's session
    assert h.http.get(f"/api/v1/sessions/{h.session_id.__class__(int=1)}/progress", headers=h.headers).status_code == 404
    ok = h.http.get(url, headers=h.headers)
    assert ok.status_code == 200 and ok.json()["free_verdict"] is None and ok.json()["report"] is None
    assert ok.headers["cache-control"] == "private, no-store" and ok.headers["referrer-policy"] == "no-referrer"


def test_end_to_end_upload_ingest_segment_verify_processed_then_report_state(make, media_dir, scratch_root) -> None:
    from app.pipeline.segment import default_segmenter_factory

    h = setup(make, scratch_root)
    pipe = Pipe(h, factory=default_segmenter_factory)
    upload(h, media_dir, 1)  # accepted upload through the real API routes
    assert Counter(j.stage for j in jobs(h)) == Counter({"ingest": 1})
    pipe.drain()
    assert Counter((j.stage, j.state) for j in jobs(h)) == Counter(
        {("ingest", "succeeded"): 1, ("segment", "succeeded"): 5, ("verify", "succeeded"): 1})
    body = progress(h).json()
    assert body["parts"][0]["state"] == "processed" and body["report"] is None
    verdict = body["free_verdict"]
    assert verdict["part1_score"] == 94.0 and len(verdict["findings"]) == 1  # the deduped synthetic rust patch
    slug = finalize(h)
    after = progress(h).json()
    assert after["state"] == "finalized" and after["report"] == {"slug": slug, "state": "building", "unlocked": False}
    assert "slug" not in after["free_verdict"] and after["free_verdict"]["part1_score"] == 94.0
    assert len(jobs(h, "claims")) == 1
    # A dead report-stage job reaches a terminal revision state visible to the owner.
    lease = pipe.queue.claim("orch-test", ["claims"])
    assert pipe.queue.fail(lease, Failure("INVALID_INPUT", retryable=False))
    pipe.reconcile()
    assert progress(h).json()["report"] == {"slug": slug, "state": "failed", "unlocked": False}
