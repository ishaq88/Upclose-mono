"""Synthetic source → assessment → frozen report → deletion, through real PostgreSQL jobs."""
import json

from sqlalchemy import select

from app.contracts.enums import CoverageState, ReportState, SessionState
from app.models import Part, ReportRevision, Session, VideoUpload
from app.pipeline.purge import PurgeHandler
from app.pipeline.report_stages import install_report_handlers
from app.pipeline.source_video import register_source_video_handlers
from app.report import render_report
from app.services.snapshots import build_free_report, build_paid_report, load_snapshot, manifest_payload
from tests.test_orchestration import Pipe, RUST_AND_DENT, finalize, isolate  # noqa: F401
from tests.test_source_video import accept_source, clip, make_video  # noqa: F401
from tests.test_uploads import bucket, session_factory  # noqa: F401


def prepare(h):
    pipe = Pipe(h, script=RUST_AND_DENT)
    register_source_video_handlers(pipe.registry, h.db, h.storage, h.settings)
    install_report_handlers(pipe.registry, h.db, h.settings)
    return pipe


def snapshot(h):
    with h.db() as db:
        revision = db.scalar(select(ReportRevision).where(ReportRevision.session_id == h.session_id))
        assert revision.state == ReportState.READY
        return load_snapshot(db, revision)


def test_unclassified_video_finishes_with_unassessed_report_and_frozen_source(make_video, tmp_path):
    h = make_video()
    pipe = prepare(h)
    source_id = accept_source(h, clip(tmp_path))
    pipe.drain()
    slug = finalize(h)
    pipe.drain()
    snap = snapshot(h)
    assert snap.manifest.provenance == "synthetic"
    assert snap.manifest.source_video.source_upload_id == source_id
    assert snap.manifest.source_video.reason == "VIDEO_MAPPING_UNAVAILABLE"
    assert snap.manifest.source_video.source_sha256 == h.row(VideoUpload, source_id).raw_sha256
    free, paid = build_free_report(snap), build_paid_report(snap)
    assert free.part1_score is None and paid.scores.headline is None
    assert not free.findings and not paid.findings
    assert all(item.state == CoverageState.NOT_ASSESSED and item.reason == "video_zone_unconfirmed"
               for item in paid.coverage)
    response = h.http.get(f"/api/v1/reports/{slug}", headers=h.headers)
    assert response.status_code == 200
    assert "source_sha256" not in response.text and "source_upload_id" not in response.text
    assert "This area was not identified" in render_report(free)


def test_marked_video_scopes_timestamps_freezes_provenance_and_purges_all_media(make_video, tmp_path):
    h = make_video()
    pipe = prepare(h)
    source_id = accept_source(h, clip(tmp_path, seconds=6), [
        {"part_no": 1, "start_ms": 1000, "end_ms": 3000},
        {"part_no": 2, "start_ms": 3000, "end_ms": 5000},
    ])
    pipe.drain()
    progress = h.http.get(f"/api/v1/sessions/{h.session_id}/progress", headers=h.headers)
    assert progress.status_code == 200
    verdict = progress.json()["free_verdict"]
    assert verdict["part1_score"] is None
    assert verdict["coverage"][0]["reason"] == "video_zone_unverified"
    assert verdict["findings"] and all(1000 <= row["source_timestamp_ms"] < 3000 for row in verdict["findings"])
    slug = finalize(h)
    pipe.drain()
    snap = snapshot(h)
    free, paid = build_free_report(snap), build_paid_report(snap)
    assert free.findings and any(row.zone.value == "passenger_side" for row in paid.findings)
    assert free.part1_score is None and paid.scores.headline is None
    for row in paid.findings:
        assert row.mapping_basis == "user_declared"
        assert (1000 <= row.source_timestamp_ms < 3000) if row.zone.value == "driver_side" else (3000 <= row.source_timestamp_ms < 5000)
    hidden = {str(row.id) for row in paid.findings if row.zone.value == "passenger_side"}
    free_json = free.model_dump_json()
    free_html = render_report(free)
    assert all(value not in free_json and value not in free_html and value not in progress.text for value in hidden)
    assert "Source video" in free_html and "area coverage unverified" in free_html
    assert "raw/" not in free_html and "frame_timestamps_ms" not in free_json
    frozen = json.dumps(manifest_payload(snap.manifest), sort_keys=True)
    with h.db.begin() as db:
        part = db.scalar(select(Part).where(Part.session_id == h.session_id, Part.part_no == 1))
        part.quality = {"usable_for_scoring": True}  # later mutable input cannot alter a ready report
    assert json.dumps(manifest_payload(snapshot(h).manifest), sort_keys=True) == frozen
    assert build_free_report(snapshot(h)) == free
    # An orphan multipart and all source/derived objects must be removed with the session.
    h.storage.create_multipart(f"media/{h.session_id}/source-orphan", "video/mp4")
    deletion = h.http.post(f"/api/v1/sessions/{h.session_id}/delete-media", headers=h.headers, json={})
    assert deletion.status_code == 202
    assert h.http.get(f"/api/v1/reports/{slug}", headers=h.headers).status_code == 410
    pipe.registry.register("purge", PurgeHandler(h.db, h.storage))
    pipe.drain()
    assert h.row(Session, h.session_id).state == SessionState.DELETED
    with h.db() as db:
        assert not db.scalars(select(VideoUpload).where(VideoUpload.session_id == h.session_id)).all()
    for kind in ("raw", "media", "demo"):
        assert not h.storage.list_keys(f"{kind}/{h.session_id}/")
        assert not h.storage.list_multipart(f"{kind}/{h.session_id}/")
