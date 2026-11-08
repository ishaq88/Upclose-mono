"""Category scores, advisory, VIN check, vibration chart and narration audio through the real report chain.

Fixture providers only; every value is synthetic. Needs PostgreSQL, ffmpeg and the Floci emulator like the other
chain tests."""
from __future__ import annotations

import json
from decimal import Decimal
import shutil
from pathlib import Path

import pytest

from app.models import Report
from app.report import render_report
from app.services.snapshots import build_free_report, build_paid_report
from tests.test_engine_audio import attach_clip, wav_bytes
from tests.test_ingest import accept, clip, media_dir, scratch_root  # noqa: F401
from tests.test_mechanical import VERDICTS
from tests.test_orchestration import Pipe, finalize, rows, setup  # noqa: F401
from tests.test_report_access import grant
from tests.test_report_stages import RUST, Rig
from tests.test_sources_domain import obd_data, rig_data
from tests.test_uploads import bucket, make, session_factory  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                reason="ffmpeg/ffprobe not installed")


def post(h, body: dict):
    response = h.http.post(f"/api/v1/sessions/{h.session_id}/sources", headers=h.headers, json=body)
    assert response.status_code == 201, response.text


def envelope(kind: str, data: dict) -> dict:
    return {"kind": kind, "provenance": "synthetic", "device": "bench", "captured_at": "2026-11-07T21:14:03Z", "data": data}


@pytest.fixture
def rig(make, media_dir, scratch_root):  # noqa: F811
    h = setup(make, scratch_root)
    pod = rig_data(seconds=45.0)
    pod["model_verdict"] = json.loads((VERDICTS / "misfire.json").read_text())
    post(h, envelope("rig_telemetry", pod))
    post(h, envelope("obd_snapshot", obd_data(vin="1HGCM82633A004352", mil_on=True)))
    pipe = Pipe(h, script=RUST)
    path = clip(media_dir)
    for part_no in (1, 2, 3, 4):  # four assessed zones give a fractional headline
        accept(h, path, part_no=part_no)
    finalize(h)
    pipe.drain()
    built = Rig(h, pipe)
    built.install(storage=h.storage)
    pipe.drain()
    return built


def test_free_view_shows_categories_and_make_but_nothing_paid(rig):
    free = build_free_report(rig.snapshot())
    assert free.categories.mechanical.state == "assessed"
    assert float(free.categories.mechanical.score) == 100 - 25 - 15 - 10 - 10 - 10  # anomaly, stored code, MIL, cleared codes, dashboard not read
    assert free.categories.overall.basis == "blend" and free.categories.overall.score is not None
    html, dumped = render_report(free), free.model_dump_json()
    for leak in ("P0301", "1HGCM82633A004352", "anomaly", "mechanical_breakdown", "advisory", "Questions for the seller",
                 "vibration-chart", "<audio", "How the Mechanical score"):
        assert leak not in html and leak not in dumped, leak
    assert "Mechanical checks" in html and "Additional checks" not in html and "Scores by category" in html


def test_paid_report_carries_breakdown_advisory_vin_chart_and_narration(rig):
    h = rig.h
    paid = build_paid_report(rig.snapshot())
    assert [row.code for row in paid.mechanical_breakdown.deductions] == ["anomaly_suspected", "trouble_codes", "mil_on",
                                                                          "codes_recently_cleared"]
    assert paid.advisory is not None and paid.advisory.label == "AI advisory, not a finding"
    assert paid.vehicle_check is not None and paid.vehicle_check.vin == "1HGCM82633A004352"
    assert paid.vibration is not None and paid.vibration.series_source == "computed" and paid.vibration.points
    assert paid.narration_audio is not None and paid.narration_audio.provenance == "synthetic"
    slug = rows(h, Report)[0].share_slug
    grant(rig)
    page = h.http.get(f"/r/{slug}", headers=h.headers)
    for expected in ("Scores by category", "Mechanical checks", "How the Mechanical score was reached", "Vehicle identity",
                     "AI advisory, not a finding", "Synthetic sample advisory", "vibration-chart", f'src="/api/v1/reports/{slug}/narration"',
                     "Synthetic sample audio", "computed from the frozen samples"):
        assert expected in page.text, expected
    audio = h.http.get(f"/api/v1/reports/{slug}/narration", headers=h.headers)
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav" and audio.content[:4] == b"RIFF"
    assert "no-store" in audio.headers["cache-control"]
    assert h.http.get(f"/api/v1/reports/{slug}/narration").status_code == 404
    assert h.http.get(f"/api/v1/reports/{slug}/narration", headers=h.other_owner()).status_code == 404
    api = h.http.get(f"/api/v1/reports/{slug}", headers=h.headers).json()
    assert api["session_id"] == str(h.session_id)  # owner reads carry the session id
    assert h.http.get(f"/api/v1/reports/{slug}").status_code == 404


def test_free_route_has_no_narration_and_no_session_id_for_other_authority(rig):
    h = rig.h
    slug = rows(h, Report)[0].share_slug
    assert h.http.get(f"/api/v1/reports/{slug}/narration", headers=h.headers).status_code == 404  # owner, no entitlement
    free = h.http.get(f"/api/v1/reports/{slug}", headers=h.headers).json()
    assert free["scope"] == "free" and "advisory" not in free and "narration_audio" not in free
    assert free["vehicle_check"]["match"] == "not_checked" and "vin" not in free["vehicle_check"]


def test_report_with_kits_and_a_fractional_headline_reaches_ready(rig):
    """Regression: the stored score is one-decimal JSON, so replay must compare stored forms, not live Decimals.
    A fractional headline plus kit sources used to kill the narrate stage with INVALID_INPUT."""
    from app.contracts.enums import ReportState
    from app.services.snapshots import compute_score, load_report_config, score_payload
    snapshot = rig.snapshot()
    assert snapshot.state == ReportState.READY
    replayed = compute_score(snapshot.manifest, snapshot.manifest_hash, load_report_config())
    exact = replayed.scores.headline
    assert exact is not None and exact != exact.quantize(Decimal("0.1"))  # fractional beyond the stored precision
    assert {row.kind for row in snapshot.manifest.sources} == {"obd_snapshot", "rig_telemetry"}
    assert snapshot.score != replayed  # the old in-memory comparison would have rejected this honest score
    assert score_payload(snapshot.score) == score_payload(replayed)
