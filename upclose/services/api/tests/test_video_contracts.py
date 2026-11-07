"""Single-video API validation and synthetic report rendering, without provider calls."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.contracts.api import CreateSessionRequest
from app.contracts.video import CreateVideoUploadRequest, VideoRange
from app.report import render_report
from app.services.snapshots import ManifestSourceFrames
from tests.test_report_render import paid_report, free_from


def test_capture_mode_defaults_to_guided_and_explicit_single_video_is_supported():
    body = {"purpose": "buyer", "consent": True, "consent_version": "test"}
    assert CreateSessionRequest.model_validate(body).capture_mode == "guided"
    assert CreateSessionRequest.model_validate({**body, "capture_mode": "single_video"}).capture_mode == "single_video"
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate({**body, "capture_mode": "youtube"})


@pytest.mark.parametrize("section", [
    {"part_no": 9, "start_ms": 0, "end_ms": 1000},
    {"part_no": 1, "start_ms": True, "end_ms": 1000},
    {"part_no": 1, "start_ms": 1000, "end_ms": 1000},
    {"part_no": 1, "start_ms": 2000, "end_ms": 1000},
    {"part_no": 1, "start_ms": 0, "end_ms": 60001},
    {"part_no": 1, "start_ms": 170000, "end_ms": 180001},
])
def test_marked_ranges_reject_invalid_identity_time_and_duration(section):
    with pytest.raises(ValidationError):
        VideoRange.model_validate(section)


def test_source_accepts_no_marked_ranges_but_rejects_overlap_or_duplicate_zones():
    body = {"content_type": "video/mp4", "content_length": 1024}
    assert CreateVideoUploadRequest.model_validate(body).segments == ()
    first = {"part_no": 1, "start_ms": 0, "end_ms": 1000}
    for second in [
        {"part_no": 1, "start_ms": 1000, "end_ms": 2000},
        {"part_no": 2, "start_ms": 500, "end_ms": 2000},
    ]:
        with pytest.raises(ValidationError):
            CreateVideoUploadRequest.model_validate({**body, "segments": [first, second]})
    assert len(CreateVideoUploadRequest.model_validate({**body, "segments": [first,
        {"part_no": 2, "start_ms": 1000, "end_ms": 2000}]}).segments) == 2


@pytest.mark.parametrize("times", [[999], [2000], [1000, 1000], [1100, 1000], [], [True]])
def test_frozen_source_timeline_rejects_wrong_or_ambiguous_frames(times):
    with pytest.raises(ValidationError):
        ManifestSourceFrames(source_upload_id=uuid4(), source_sha256="a" * 64,
            start_ms=1000, end_ms=2000, mapping_basis="user_declared", frame_timestamps_ms=times)


def test_report_renders_only_timestamps_in_the_scoped_dto_without_media_fallback():
    report = paid_report()
    marked = tuple(row.model_copy(update={"source_timestamp_ms": 12345 if row.zone.value == "driver_side" else 98765,
                                         "mapping_basis": "user_declared"}) for row in report.findings)
    paid = report.model_copy(update={"findings": marked})
    free_html = render_report(free_from(paid))
    paid_html = render_report(paid)
    assert "Source video 0:12.345" in free_html
    assert "1:38.765" not in free_html
    assert "Source video 1:38.765" in paid_html
    assert "area coverage unverified" in free_html
    assert "Evidence image withheld" in free_html
    assert "<video" not in free_html and "raw/" not in free_html
