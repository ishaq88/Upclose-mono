"""Evidence-source ingest route, manifest freeze, report sections and purge (PostgreSQL, synthetic data)."""
from __future__ import annotations

import json
import shutil
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text, update

from app.contracts.enums import SessionState
from app.contracts.reports import FreeReport, PaidReport
from app.jobs.queue import JobQueue
from app.models import EvidenceSource, Report, ReportRevision, Session
from app.pipeline.orchestration import canonical_hash
from app.pipeline.purge import PurgeHandler
from app.report import render_report
from app.services import snapshots
from app.services.deletion import request_deletion
from app.services.snapshots import (SnapshotIntegrityError, build_free_report, build_paid_report, load_manifest,
                                    manifest_payload)
from tests.test_ingest import accept, clip, media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import Pipe, finalize, isolate, rows, setup  # noqa: F401
from tests.test_report_stages import RUST, Rig
from tests.test_sources_domain import envelope, obd_data, rig_data
from tests.test_uploads import bucket, make, session_factory  # noqa: F401

SECRET_BITS = ("P0301", "SYNTHETC00000UPCA", "dominant", "rpm", "Hz", "catalyst", "unverified")


def post(h, body, headers=None, session_id=None):
    return h.http.post(f"/api/v1/sessions/{session_id or h.session_id}/sources",
                       headers=h.headers if headers is None else headers, json=body)


def rig_body(**kwargs):
    return envelope("rig_telemetry", rig_data(seconds=8.0, **kwargs))


def obd_body(**overrides):
    return envelope("obd_snapshot", obd_data(**overrides), device="elm327-emulator")


def source_rows(h):
    return rows(h, EvidenceSource, order=EvidenceSource.kind)


# ---- route ----------------------------------------------------------------------------------------


def test_post_returns_201_with_hash_and_summary_and_stores_the_row(make):
    h = make()
    response = post(h, rig_body(temperature=[{"t_ms": 0, "celsius": 25.0}, {"t_ms": 4000, "celsius": 31.0}]))
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"source_id", "kind", "data_sha256", "summary"} and body["kind"] == "rig_telemetry"
    assert body["summary"]["state"] == "assessed" and body["summary"]["provenance"] == "synthetic"
    assert abs(body["summary"]["dominant_hz"] - 25.0) < 0.1 and body["summary"]["rpm_estimate"] in range(747, 754)
    (row,) = source_rows(h)
    assert str(row.id) == body["source_id"] and row.data_sha256 == body["data_sha256"] == canonical_hash(row.data)
    assert row.device == "grove-beginner-kit" and row.summary == json.loads(json.dumps(row.summary))
    assert len(row.data["accel"]["x_mg"]) == 3200
    obd = post(h, obd_body())
    assert obd.status_code == 201 and obd.json()["summary"]["flags"] == ["mil_on", "trouble_codes", "codes_recently_cleared"]
    assert [row.kind for row in source_rows(h)] == ["obd_snapshot", "rig_telemetry"]


def test_repeat_post_replaces_the_source_for_that_kind(make):
    h = make()
    first = post(h, obd_body()).json()
    second = post(h, obd_body(dtcs=[], mil_on=False))
    assert second.status_code == 201
    assert second.json()["source_id"] == first["source_id"] and second.json()["data_sha256"] != first["data_sha256"]
    (row,) = source_rows(h)
    assert row.data["dtcs"] == [] and row.summary["flag_count"] == 1  # only the recent-clear flag remains


def test_noise_trace_is_accepted_but_not_assessed(make):
    h = make()
    response = post(h, rig_body(noise_only=True))
    assert response.status_code == 201
    assert response.json()["summary"]["state"] == "not_assessed" and response.json()["summary"]["dominant_hz"] is None


def test_auth_and_ownership_follow_session_conventions(make):
    h = make()
    assert h.http.post(f"/api/v1/sessions/{h.session_id}/sources", json=obd_body()).status_code == 401
    assert post(h, obd_body(), headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
    assert post(h, obd_body(), headers=h.other_owner()).status_code == 404
    assert post(h, obd_body(), session_id=uuid4()).status_code == 404
    assert source_rows(h) == []


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(extra=1), lambda b: b.update(kind="engine_audio"),
    lambda b: b["data"].update(vin="BAD"), lambda b: b["data"]["dtcs"].append({"code": "Z9999", "status": "stored"}),
])
def test_invalid_bodies_are_422_and_store_nothing(make, mutate):
    h = make()
    body = obd_body()
    mutate(body)
    response = post(h, body)
    assert response.status_code == 422 and response.headers["content-type"].startswith("application/problem+json")
    mismatched = rig_body()
    mismatched["data"]["accel"]["z_mg"] = mismatched["data"]["accel"]["z_mg"][:-1]
    assert post(h, mismatched).status_code == 422
    assert source_rows(h) == []


def test_409_after_finalize_and_404_for_deleted_sessions(make):
    h = make()
    assert post(h, obd_body()).status_code == 201
    finalize(h)
    late = post(h, obd_body(mil_on=False))
    assert late.status_code == 409 and late.headers["content-type"].startswith("application/problem+json")
    assert source_rows(h)[0].data["mil_on"] is True
    gone = make()
    with gone.db.begin() as db:
        db.execute(update(Session).where(Session.id == gone.session_id).values(state=SessionState.DELETING))
    assert post(gone, obd_body()).status_code == 404
    with gone.db.begin() as db:
        db.execute(update(Session).where(Session.id == gone.session_id).values(state=SessionState.DELETED))
    assert post(gone, obd_body()).status_code == 404 and source_rows(gone) == []


def test_database_rejects_duplicate_kind_and_bad_hash(make):
    h = make()
    post(h, obd_body())
    from sqlalchemy.exc import IntegrityError
    row = source_rows(h)[0]
    with pytest.raises(IntegrityError), h.db.begin() as db:
        db.add(EvidenceSource(session_id=h.session_id, kind="obd_snapshot", provenance="live", device="d",
                              captured_at=row.captured_at, data={}, data_sha256=row.data_sha256, summary={}))
    with pytest.raises(IntegrityError), h.db.begin() as db:
        db.add(EvidenceSource(session_id=h.session_id, kind="rig_telemetry", provenance="live", device="d",
                              captured_at=row.captured_at, data={}, data_sha256="nothex", summary={}))


# ---- manifest freeze, reports, purge (full pipeline) -----------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg/ffprobe not installed")


def build_rig(make, media_dir, scratch_root, bodies) -> Rig:  # noqa: F811
    h = setup(make, scratch_root)
    for body in bodies:
        assert post(h, body).status_code == 201
    pipe = Pipe(h, script=RUST)
    accept(h, clip(media_dir), part_no=1)
    finalize(h)
    pipe.drain()
    rig = Rig(h, pipe)
    rig.install()
    pipe.drain()
    return rig


@pytest.fixture
def with_sources(make, media_dir, scratch_root):  # noqa: F811
    return build_rig(make, media_dir, scratch_root, [rig_body(), obd_body()])


@pytest.fixture
def without_sources(make, media_dir, scratch_root):  # noqa: F811
    return build_rig(make, media_dir, scratch_root, [])


@needs_ffmpeg
def test_manifest_freezes_sources_bound_by_hash(with_sources):
    rig = with_sources
    revision = rig.revision()
    manifest = load_manifest(revision)
    assert [row.kind for row in manifest.sources] == ["obd_snapshot", "rig_telemetry"]
    stored = {row.kind: row for row in source_rows(rig.h)}
    for record in manifest.sources:
        assert record.data_sha256 == stored[record.kind].data_sha256 == canonical_hash(stored[record.kind].data)
        assert record.provenance == "synthetic" and record.summary.provenance == "synthetic"
    assert "accel" not in json.dumps(revision.manifest)  # raw samples stay in the row
    assert canonical_hash(manifest_payload(manifest)) == revision.manifest_hash


@needs_ffmpeg
def test_manifest_without_sources_omits_the_field_and_keeps_its_hash(without_sources):
    revision = without_sources.revision()
    assert "sources" not in revision.manifest
    manifest = load_manifest(revision)
    assert manifest.sources == () and "sources" not in manifest_payload(manifest)
    assert canonical_hash(manifest_payload(manifest)) == revision.manifest_hash


@needs_ffmpeg
def test_tampered_source_data_fails_closed_when_a_manifest_is_built(with_sources):
    rig = with_sources
    with rig.h.db.begin() as db:
        db.execute(text("UPDATE evidence_sources SET data = jsonb_set(data, '{cylinders}', '8') "
                        "WHERE session_id = :s AND kind = 'rig_telemetry'"), {"s": rig.h.session_id})
    with rig.h.db() as db, pytest.raises(SnapshotIntegrityError):
        snapshots._manifest_sources(db, rig.h.session_id)


@needs_ffmpeg
def test_free_report_shows_only_checked_state_and_flag_counts(with_sources, without_sources):
    snapshot = with_sources.snapshot()
    free = build_free_report(snapshot)
    checks = {row.kind: row for row in free.additional_checks}
    assert checks["obd_snapshot"].checked and checks["obd_snapshot"].flag_count == 3
    assert checks["rig_telemetry"].checked and checks["rig_telemetry"].flag_count == 0
    dumped = free.model_dump_json()
    html = render_report(free)
    for leak in ("P0301", "SYNTHETC00000UPCA", "dominant_hz", "rpm_estimate", "elm327", "catalyst",
                 "grove-beginner-kit", "data_sha256"):
        assert leak not in dumped and leak not in html, leak
    assert "Additional checks" in html and "Checked" in html and "3 flags raised" in html
    # The HTTP free view is the same DTO.
    slug = rows(with_sources.h, Report)[0].share_slug
    api = with_sources.h.http.get(f"/api/v1/reports/{slug}", headers=with_sources.h.headers)
    assert api.status_code == 200 and api.json()["scope"] == "free"
    assert "P0301" not in api.text and "SYNTHETC" not in api.text and api.json()["additional_checks"][0].keys() == {"kind", "checked", "flag_count"}
    page = with_sources.h.http.get(f"/r/{slug}", headers=with_sources.h.headers)
    assert "P0301" not in page.text and "Additional checks" in page.text
    # No sources attached: both kinds read "Not checked", never a pass.
    empty = build_free_report(without_sources.snapshot())
    assert [(row.kind, row.checked, row.flag_count) for row in empty.additional_checks] == [
        ("rig_telemetry", False, 0), ("obd_snapshot", False, 0), ("engine_audio", False, 0)]
    empty_html = render_report(empty)
    assert empty_html.count("Not checked") == 3 and "Checked<" not in empty_html.replace("Not checked", "")


@needs_ffmpeg
def test_paid_report_carries_full_summaries_provenance_and_unchecked_kinds(with_sources, without_sources):
    paid = build_paid_report(with_sources.snapshot())
    PaidReport.model_validate(paid.model_dump(mode="json"))
    html = render_report(paid)
    for expected in ("Additional checks", "Simulated bench data", "P0301", "SYNTHETC00000UPCA", "Engine speed (estimate)",
                     "uncalibrated estimates", "Source device grove-beginner-kit", "catalyst", "Not yet complete".lower()):
        assert expected.lower() in html.lower(), expected
    assert "<script" not in html.lower() and "style=" not in html
    empty = build_paid_report(without_sources.snapshot())
    assert [(row.kind, row.checked, row.source) for row in empty.additional_checks] == [
        ("rig_telemetry", False, None), ("obd_snapshot", False, None), ("engine_audio", False, None)]
    empty_html = render_report(empty)
    assert empty_html.count("Not checked") == 3 and "it is not a pass" in empty_html


def _pure_paid(checks):
    from decimal import Decimal
    from app.contracts.enums import CoverageState, ReportState, Zone
    from app.contracts.scoring import RepairSummary, ScoreResult, ZoneScore
    return PaidReport(slug="s", revision_id=uuid4(), state=ReportState.READY, coverage=(),
        scores=ScoreResult(headline=None, zones=(ZoneScore(zone=Zone.DRIVER_SIDE, state=CoverageState.ASSESSED, score=Decimal("80")),), config_hash="a" * 64),
        findings=(), repairs=RepairSummary(items=(), subtotal_low_cents=0, subtotal_high_cents=0, unestimated_count=0, table_version="v1"),
        claims=(), narration=(), additional_checks=tuple(checks))


def test_renderer_labels_every_provenance_and_never_passes_unassessed_data():
    from datetime import datetime, timezone
    from app.contracts.sources import ObdSnapshotData, PaidSourceCheck, RigTelemetryData, SourceRecord
    from app.domain.sources import summarize_obd, summarize_rig
    from app.services.sources import load_sources
    config = load_sources()
    badges = {"synthetic": "Simulated bench data", "cached": "Replayed recording", "live": "Live"}
    for provenance, badge in badges.items():
        summary = summarize_obd(ObdSnapshotData.model_validate(obd_data()), provenance, config)
        check = PaidSourceCheck(kind="obd_snapshot", checked=True, source=SourceRecord(kind="obd_snapshot",
            provenance=provenance, device="elm327-emulator", captured_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
            data_sha256="a" * 64, summary=summary))
        html = render_report(_pure_paid([check]))
        assert f'<span class="badge">{badge}</span>' in html
        assert all(other not in html for name, other in badges.items() if name != provenance and other != "Live")
    noise = summarize_rig(RigTelemetryData.model_validate(rig_data(noise_only=True)), "cached", config)
    check = PaidSourceCheck(kind="rig_telemetry", checked=False, source=SourceRecord(kind="rig_telemetry",
        provenance="cached", device="grove-beginner-kit", captured_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
        data_sha256="b" * 64, summary=noise))
    html = render_report(_pure_paid([check]))
    assert "Not checked" in html and "No clear dominant frequency" in html and "This is not a pass" in html
    assert "Replayed recording" in html


def test_source_record_rejects_a_summary_that_disagrees_with_its_envelope():
    from datetime import datetime, timezone
    from pydantic import ValidationError
    from app.contracts.sources import ObdSnapshotData, SourceRecord
    from app.domain.sources import summarize_obd
    from app.services.sources import load_sources
    summary = summarize_obd(ObdSnapshotData.model_validate(obd_data()), "synthetic", load_sources())
    with pytest.raises(ValidationError):
        SourceRecord(kind="rig_telemetry", provenance="synthetic", device="d", captured_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
                     data_sha256="a" * 64, summary=summary)
    with pytest.raises(ValidationError):
        SourceRecord(kind="obd_snapshot", provenance="live", device="d", captured_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
                     data_sha256="a" * 64, summary=summary)


@needs_ffmpeg
def test_purge_removes_source_rows_with_the_session(with_sources):
    h = with_sources.h
    assert len(source_rows(h)) == 2
    assert request_deletion(h.db, h.headers["Authorization"].split()[1], h.session_id).state == "deleting"
    queue = JobQueue(h.db)
    lease = queue.claim("purge", ["purge"])
    assert lease is not None
    assert queue.complete(lease, PurgeHandler(h.db, h.storage)(lease).publish)  # ready-revision trigger allows deletion
    assert source_rows(h) == []
    with h.db() as db:
        assert db.scalar(select(func.count()).select_from(EvidenceSource).where(EvidenceSource.session_id == h.session_id)) == 0
        assert db.get(Session, h.session_id).state == SessionState.DELETED


# ---- GET /sources and model verdict -----------------------------------------------------


def get_sources(h, headers=None, session_id=None):
    return h.http.get(f"/api/v1/sessions/{session_id or h.session_id}/sources",
                      headers=h.headers if headers is None else headers)


def verdict_body(name="misfire", **kwargs):
    from tests.test_sources_domain import with_verdict
    return envelope("rig_telemetry", with_verdict(name, **kwargs))


def test_get_sources_lists_by_kind_without_raw_data_and_survives_finalize(make):
    h = make()
    assert get_sources(h).json() == {"sources": []}
    post(h, rig_body())
    post(h, obd_body())
    listing = get_sources(h)
    assert listing.status_code == 200, listing.text
    items = listing.json()["sources"]
    assert [row["kind"] for row in items] == ["obd_snapshot", "rig_telemetry"]
    for row, stored in zip(items, source_rows(h)):
        assert set(row) == {"source_id", "kind", "provenance", "device", "captured_at", "data_sha256", "summary"}
        assert row["data_sha256"] == stored.data_sha256 and row["source_id"] == str(stored.id)
    assert "x_mg" not in listing.text and '"data"' not in listing.text and "SYNTHETC" in listing.text
    finalize(h)
    assert get_sources(h).json() == listing.json()


def test_get_sources_auth_and_ownership(make):
    h = make()
    path = f"/api/v1/sessions/{h.session_id}/sources"
    assert h.http.get(path).status_code == 401
    assert get_sources(h, headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
    assert get_sources(h, headers=h.other_owner()).status_code == 404
    assert get_sources(h, session_id=uuid4()).status_code == 404
    with h.db.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id).values(state=SessionState.DELETING))
    assert get_sources(h).status_code == 404


def test_verdict_is_stored_hashed_and_flagged(make):
    h = make()
    response = post(h, verdict_body())
    assert response.status_code == 201, response.text
    summary = response.json()["summary"]
    assert "anomaly_suspected" in summary["flags"] and summary["model_verdict"]["schema"] == "upclose-anomaly-verdict/1"
    (row,) = source_rows(h)
    assert row.data["model_verdict"]["verdict"] == "anomaly_suspected" and row.data_sha256 == canonical_hash(row.data)
    assert get_sources(h).json()["sources"][0]["summary"]["model_verdict"]["anomalous_runs"][0]["t_end_s"] == 40.0


def test_recording_without_verdict_stores_no_verdict_key_and_the_same_hash(make):
    h = make()
    body = rig_body()
    response = post(h, body)
    (row,) = source_rows(h)
    assert "model_verdict" not in row.data and response.json()["summary"]["model_verdict"] is None
    from app.contracts.sources import RigTelemetrySource
    expected = RigTelemetrySource.model_validate(body).data.model_dump(mode="json")
    expected.pop("model_verdict")
    assert row.data_sha256 == canonical_hash(expected)


def test_inconsistent_verdicts_are_422_over_http(make):
    h = make()
    body = verdict_body()
    body["data"]["model_verdict"]["verdict"] = "no_anomaly_detected"
    assert post(h, body).status_code == 422
    body = verdict_body()
    body["data"]["model_verdict"]["anomalous_fraction"] = 0.5
    assert post(h, body).status_code == 422
    body = verdict_body(seconds=10.0)
    assert post(h, body).status_code == 422
    assert source_rows(h) == []


@pytest.fixture
def with_verdict_rig(make, media_dir, scratch_root):  # noqa: F811
    return build_rig(make, media_dir, scratch_root, [verdict_body(), obd_body()])


@needs_ffmpeg
def test_verdict_reaches_the_paid_report_but_not_the_free_view(with_verdict_rig, with_sources):
    rig = with_verdict_rig
    revision = rig.revision()
    manifest = load_manifest(revision)
    rig_source = next(row for row in manifest.sources if row.kind == "rig_telemetry")
    assert rig_source.summary.model_verdict is not None and "anomaly_suspected" in rig_source.summary.flags
    assert canonical_hash(manifest_payload(manifest)) == revision.manifest_hash
    paid_html = render_report(build_paid_report(rig.snapshot()))
    for expected in ("Engine anomaly model", "Anomaly suspected",
                     "unlike this engine&#39;s own baseline between 0–40 s", "60 s of this engine&#39;s own recording",
                     "not a diagnosis", "cycle-to-cycle irregularity"):
        assert expected in paid_html, expected
    free = build_free_report(rig.snapshot())
    html, dumped = render_report(free), free.model_dump_json()
    for leak in ("unlike", "baseline", "vib_", "firing", "anomalous_runs", "model_verdict", "cycle-to-cycle"):
        assert leak not in html and leak not in dumped, leak
    assert {row.kind: row.flag_count for row in free.additional_checks} == {"rig_telemetry": 1, "obd_snapshot": 3, "engine_audio": 0}
    # A recording frozen without a verdict has no verdict key anywhere in its manifest.
    assert "model_verdict" not in json.dumps(with_sources.revision().manifest)
