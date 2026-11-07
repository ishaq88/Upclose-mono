"""Demo-footage intake: synthetic snippets only, no provider/DB calls."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.demo.intake import IntakeError, build_demo_video
from app.pipeline.source_mapping import load_source_config
from tests import media_fixtures as fx

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar(tmp_path: Path, segments: list[dict], provenance="synthetic", permission=False) -> Path:
    path = tmp_path / "sidecar.json"
    path.write_text(json.dumps({
        "provenance": provenance, "permission_cleared": permission, "segments": segments,
    }), encoding="utf-8")
    return path


def _seg(file: str, **over) -> dict:
    return {"file": file, "source_label": "phone-a", "car_label": "car-1",
            "area_label": "front", "start_offset_s": 0, **over}


def _two_clips(tmp_path: Path):
    fx.make_solid(tmp_path / "a.mp4", "red", seconds=1.0)
    fx.make_solid(tmp_path / "b.mp4", "blue", seconds=2.0)
    return [_seg("a.mp4"), _seg("b.mp4", car_label="car-2", area_label="rear")]


def test_stitch_order_manifest_ranges_and_originals_untouched(tmp_path):
    clips = _two_clips(tmp_path)
    before = {name: _sha(tmp_path / name) for name in ("a.mp4", "b.mp4")}
    manifest_path = build_demo_video(_sidecar(tmp_path, clips), tmp_path / "out")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["provenance"] == "synthetic"
    assert "not real cars" in manifest["presentation"]
    assert manifest["is_fresh_live_inference"] is False and manifest["model_requests"] == 0
    segs = manifest["segments"]
    assert [s["source_file"] for s in segs] == ["a.mp4", "b.mp4"]
    assert segs[0]["stitched_start_s"] == 0.0
    assert abs(segs[0]["duration_s"] - 1.0) < 0.35
    assert abs(segs[1]["stitched_start_s"] - segs[0]["duration_s"]) < 0.01
    assert abs(manifest["total_duration_s"] - 3.0) < 0.6
    assert segs[0]["source_sha256"] == before["a.mp4"]
    assert manifest["video_sha256"] == _sha(tmp_path / "out" / "example.mp4")
    assert manifest["total_size_bytes"] == (tmp_path / "out" / "example.mp4").stat().st_size
    assert {name: _sha(tmp_path / name) for name in before} == before


def test_cached_provenance_requires_and_records_permission(tmp_path):
    build_demo_video(_sidecar(tmp_path, _two_clips(tmp_path), provenance="cached", permission=True),
                     tmp_path / "out")
    manifest = json.loads((tmp_path / "out" / "demo-manifest.json").read_text())
    assert manifest["provenance"] == "cached" and manifest["permission_cleared"] is True
    assert "permission-cleared real footage" in manifest["presentation"]


@pytest.mark.parametrize("provenance,permission", [
    ("cached", False), ("synthetic", True), ("live", True), ("real", True),
])
def test_provenance_mislabeling_refused(tmp_path, provenance, permission):
    with pytest.raises(IntakeError):
        build_demo_video(_sidecar(tmp_path, _two_clips(tmp_path),
                                  provenance=provenance, permission=permission), tmp_path / "out")
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").iterdir())


def test_hash_mismatch_on_sidecar_refused(tmp_path):
    fx.make_solid(tmp_path / "a.mp4", "red", seconds=1.0)
    with pytest.raises(IntakeError):
        build_demo_video(_sidecar(tmp_path, [_seg("a.mp4", sha256="0" * 64)]), tmp_path / "out")
    assert not (tmp_path / "out" / "example.mp4").exists()


def test_empty_and_missing_refused_without_partial_output(tmp_path):
    with pytest.raises(IntakeError):
        build_demo_video(_sidecar(tmp_path, []), tmp_path / "out")
    with pytest.raises(IntakeError):
        build_demo_video(_sidecar(tmp_path, [_seg("nope.mp4")]), tmp_path / "out")
    out = tmp_path / "out"
    assert not out.exists() or list(out.iterdir()) == []


def test_unsupported_container_refused(tmp_path):
    fx.make_mpegts(tmp_path / "a.ts")
    with pytest.raises(IntakeError):
        build_demo_video(_sidecar(tmp_path, [_seg("a.ts")]), tmp_path / "out")
    assert not (tmp_path / "out" / "example.mp4").exists()


def test_duration_limits_reused_from_ingest_config(tmp_path):
    fx.make_solid(tmp_path / "a.mp4", "red", seconds=2.0)
    fx.make_solid(tmp_path / "b.mp4", "blue", seconds=2.0)
    limits = load_source_config().model_copy(update={"max_duration_ms": 3000})
    with pytest.raises(IntakeError):  # total 4s > 3s injected ingest limit
        build_demo_video(_sidecar(tmp_path, [_seg("a.mp4"), _seg("b.mp4")]),
                         tmp_path / "out", source_config=limits)
    assert not (tmp_path / "out" / "example.mp4").exists()


def test_size_limit_reused_from_ingest_config(tmp_path):
    fx.make_solid(tmp_path / "a.mp4", "red", seconds=1.0)
    limits = load_source_config().model_copy(update={"max_source_bytes": 1024})
    with pytest.raises(IntakeError):
        build_demo_video(_sidecar(tmp_path, [_seg("a.mp4")]), tmp_path / "out", source_config=limits)
    assert not (tmp_path / "out" / "example.mp4").exists()


def test_start_offset_trims_segment(tmp_path):
    fx.make_solid(tmp_path / "a.mp4", "red", seconds=2.0)
    manifest_path = build_demo_video(
        _sidecar(tmp_path, [_seg("a.mp4", start_offset_s=1.0)]), tmp_path / "out")
    manifest = json.loads(manifest_path.read_text())
    assert abs(manifest["segments"][0]["duration_s"] - 1.0) < 0.35
    assert manifest["segments"][0]["start_offset_s"] == 1.0
