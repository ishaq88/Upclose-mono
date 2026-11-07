"""Bounded smoke checks: offline only, mocked segmenter/budget, no DB or network."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from app.adapters.meta.parser import ProviderFailure
from app.adapters.meta.types import MediaRef, ProviderUsage, SegmentResult
from app.evaluation.smoke import SmokeConfig, SmokeError, assert_receipt_sanitized, run_smoke
from app.jobs.queue import Lease

SECRET = "smoke-test-secret-must-never-appear"


def media() -> MediaRef:
    return MediaRef(media_key="media/test/frame.jpg", kind="image", stored_width=4, stored_height=3,
                    frames=[{"frame_index": 0, "timestamp_ms": 0, "frame_key": "media/test/frame.jpg"}])


def lease() -> Lease:
    now = datetime.now(timezone.utc)
    return Lease(job_id=uuid4(), session_id=uuid4(), token=uuid4(), attempt=1, stage="segment",
                 subject_kind="concept_run", subject_id="smoke", lease_until=now + timedelta(minutes=2),
                 deadline=now + timedelta(minutes=5))


class CountingSegmenter:
    requires_budget = True

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events, self.fail, self.calls = events, fail, 0

    def segment(self, media_ref, prompt_id, request_id):  # noqa: ANN001, ANN202
        self.calls += 1
        self.events.append("segment")
        if self.fail:
            raise ProviderFailure("provider_timeout")
        return SegmentResult(request_id=request_id, prompt_id=prompt_id, provenance="live",
                             observations=(), usage=ProviderUsage(images_processed=1))


class FakeBudget:
    def __init__(self, events: list[str], *, dispatch: bool = True) -> None:
        self.events, self.dispatch = events, dispatch

    def reserve(self, request):  # noqa: ANN001, ANN202
        self.events.append("reserve")

    def mark_dispatched(self, attempt_id, lease_token):  # noqa: ANN001, ANN202
        self.events.append("dispatch")
        return self.dispatch

    def settle(self, attempt_id, **kwargs):  # noqa: ANN001
        self.events.append("settle")

    def mark_unknown(self, attempt_id, **kwargs):  # noqa: ANN001
        self.events.append("unknown")


def test_default_fixture_makes_zero_provider_or_budget_calls():
    events: list[str] = []
    segmenter = CountingSegmenter(events)
    receipt = run_smoke(SmokeConfig(), segmenter=segmenter, budget_service=FakeBudget(events),  # type: ignore[arg-type]
                        lease=lease(), media=media())
    assert segmenter.calls == 0 and events == []
    assert receipt["outcome"] == "dry_run" and receipt["provenance"] == "synthetic"
    assert receipt["counts"]["requests_attempted"] == 0
    assert receipt["budget"]["dispatch_path"] == "none"


def test_live_without_opt_in_fails_closed_with_zero_calls():
    events: list[str] = []
    segmenter = CountingSegmenter(events)
    with pytest.raises(SmokeError, match="live_not_opted_in"):
        run_smoke(SmokeConfig(mode="live"), segmenter=segmenter, budget_service=FakeBudget(events),  # type: ignore[arg-type]
                  lease=lease(), media=media(), allow_live=False)
    assert segmenter.calls == 0 and events == []


def test_live_without_budget_reservation_fails_closed_with_zero_calls():
    events: list[str] = []
    segmenter = CountingSegmenter(events)
    with pytest.raises(SmokeError, match="budget_not_reserved"):
        run_smoke(SmokeConfig(mode="live"), segmenter=segmenter, budget_service=None,
                  lease=lease(), media=media(), allow_live=True)
    assert segmenter.calls == 0
    # Budget present but dispatch refused: reservation path runs, transport never does.
    receipt = run_smoke(SmokeConfig(mode="live"), segmenter=segmenter,
                        budget_service=FakeBudget(events, dispatch=False),  # type: ignore[arg-type]
                        lease=lease(), media=media(), allow_live=True)
    assert segmenter.calls == 0 and events == ["reserve", "dispatch"]
    assert receipt["outcome"] == "failed" and receipt["failures"] == ["budget_not_reserved"]


def test_live_success_routes_reserve_dispatch_segment_settle_in_order():
    events: list[str] = []
    segmenter = CountingSegmenter(events)
    receipt = run_smoke(SmokeConfig(mode="live", max_requests=2), segmenter=segmenter,
                        budget_service=FakeBudget(events), lease=lease(), media=media(), allow_live=True)  # type: ignore[arg-type]
    assert events == ["reserve", "dispatch", "segment", "settle"] * 2
    assert receipt["outcome"] == "succeeded" and receipt["provenance"] == "live"
    assert receipt["counts"] == {"planned_requests": 2, "requests_attempted": 2,
                                  "requests_succeeded": 2, "requests_failed": 0, "observations": 0}
    assert receipt["usage"]["images_processed"] == 2 and receipt["usage"]["usage_unknown"] is False


@pytest.mark.parametrize("changes", [{"max_requests": 0}, {"max_requests": 4},
                                     {"timeout_seconds": 0}, {"timeout_seconds": 121}])
def test_hard_request_and_time_caps_are_enforced(changes):
    with pytest.raises(SmokeError):
        SmokeConfig(mode="live", **changes)


def test_time_budget_stops_before_next_request():
    events: list[str] = []
    ticks = iter([0.0, 0.0, 0.0, 0.1, 100.0])
    receipt = run_smoke(SmokeConfig(mode="live", max_requests=3, timeout_seconds=30),
                        segmenter=CountingSegmenter(events), budget_service=FakeBudget(events),  # type: ignore[arg-type]
                        lease=lease(), media=media(), allow_live=True, clock=lambda: next(ticks))
    assert receipt["counts"]["requests_attempted"] == 1
    assert receipt["failures"] == ["time_budget_exhausted"]


def test_receipt_contains_no_secret_like_fields():
    events: list[str] = []
    receipt = run_smoke(SmokeConfig(mode="live"), segmenter=CountingSegmenter(events),
                        budget_service=FakeBudget(events), lease=lease(), media=media(), allow_live=True)  # type: ignore[arg-type]
    serialized = json.dumps(receipt)
    for forbidden in (SECRET, "Bearer", "api_key", "authorization", "lease_token",
                      "media_key", "base64", "signed"):
        assert forbidden not in serialized
    with pytest.raises(SmokeError, match="receipt_sanitization_failed"):
        assert_receipt_sanitized({"nested": {"api_key": SECRET}})
    with pytest.raises(SmokeError, match="receipt_sanitization_failed"):
        assert_receipt_sanitized({"note": "Bearer " + SECRET})


def test_cli_defaults_to_fixture_and_live_without_opt_in_fails_closed(tmp_path: Path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "sam_smoke.py"
    output = tmp_path / "receipt.json"
    result = subprocess.run([sys.executable, str(script), "--output", str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(output.read_text())
    assert receipt["mode"] == "fixture" and receipt["outcome"] == "dry_run"
    assert SECRET not in output.read_text()
    output.unlink()
    result = subprocess.run([sys.executable, str(script), "--live", "--output", str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 2
    receipt = json.loads(output.read_text())
    assert receipt["outcome"] == "failed_closed" and receipt["failures"] == ["live_not_opted_in"]
