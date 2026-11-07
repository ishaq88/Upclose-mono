"""Synthetic dataset-intake checks only; no live media, provider, or network call."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation.intake import IntakeManifest, manifest_violations, validate_dataset_intake

H = "b" * 64
RECEIPT = "c" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image(root: Path, ident="img-1", *, car="car-1", data=b"fake-jpeg-1", labels=None,
          vehicle=True, track="track-1", frame_index=0, media_file=None, **over):
    rel = media_file or f"{car}/{ident}.jpg"
    if not Path(rel).is_absolute() and ".." not in Path(rel).parts:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if labels is None:
        labels = [{"id": f"label-{ident}", "concept": "rust", "component_id": "hood",
                   "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "severity": 3}]
    row = {
        "id": ident, "car_id": car, "frame_id": f"frame-{ident}",
        "timestamp_ms": 1000, "frame_index": frame_index, "media_file": rel,
        "media_sha256": sha(data), "vehicle_present": vehicle,
        "vehicle_track_id": track if vehicle else None, "split": "heldout",
        "part_no": 1, "permission": "consent", "permission_cleared": True,
        "permission_receipt_sha256": RECEIPT, "reviewer": "reviewer-1",
        "blur": "clear", "lighting": "daylight", "reviewed_concepts": ["rust"],
        "rust_category": "positive" if labels else "clean",
        "labels": labels,
    }
    row.update(over)
    return row


def manifest(images, walks=None, provenance="live"):
    return {"schema_version": "upclose-dataset-intake-v1", "provenance": provenance,
            "images": images, "walkarounds": walks or []}


def validate(root, data):
    return validate_dataset_intake(IntakeManifest.model_validate(data), root)


def test_valid_minimal_dataset_passes(tmp_path):
    receipt = validate(tmp_path, manifest([image(tmp_path)]))
    assert receipt["valid"] and receipt["violations"] == []
    assert receipt["counts"] == {"images": 1, "walkarounds": 0, "cars": 1,
                                  "labels": 1, "media_files_checked": 1}
    assert receipt["gates"]["dataset_intake"] == "passed"
    assert receipt["gates"]["live_quality"] == "unverified"
    assert receipt["provenance_verified"] is False


def test_clean_no_vehicle_and_walkaround_pass(tmp_path):
    img = image(tmp_path, vehicle=False, track=None, labels=[])
    img["rust_category"] = "clean"
    video = b"fake-video"
    (tmp_path / "car-1").mkdir(exist_ok=True)
    (tmp_path / "car-1/walk.mp4").write_bytes(video)
    walk = {"id": "walk-1", "car_id": "car-1", "split": "heldout", "complete": False,
            "image_ids": ["img-1"], "reviewer": "reviewer-1",
            "source_video_file": "car-1/walk.mp4", "source_video_sha256": sha(video),
            "completeness_receipt_sha256": RECEIPT}
    # Same track-less clean image cannot also be a positive walkaround example;
    # use it only as the walkaround frame with its declared clean labels.
    receipt = validate(tmp_path, manifest([img], [walk]))
    assert receipt["valid"], receipt["violations"]


@pytest.mark.parametrize("mutate,code", [
    (lambda r, d: (r / d["images"][0]["media_file"]).unlink(), "media_missing:img-1"),
    (lambda r, d: (r / d["images"][0]["media_file"]).write_bytes(b"tampered"), "media_hash_mismatch:img-1"),
    (lambda r, d: (r / d["images"][0]["media_file"]).write_bytes(b""), "media_empty:img-1"),
])
def test_media_file_violations_fail_closed(tmp_path, mutate, code):
    data = manifest([image(tmp_path)])
    manifest_obj = IntakeManifest.model_validate(data)
    mutate(tmp_path, data)
    receipt = validate_dataset_intake(manifest_obj, tmp_path)
    assert not receipt["valid"] and code in receipt["violations"]


def test_missing_media_root_fails_closed(tmp_path):
    data = manifest([image(tmp_path)])
    receipt = validate_dataset_intake(
        IntakeManifest.model_validate(data), tmp_path / "nope")
    assert receipt["violations"] == ["media_root_missing"]


@pytest.mark.parametrize("field", ["permission", "permission_cleared", "permission_receipt_sha256",
                                   "reviewer", "vehicle_present", "timestamp_ms", "frame_index"])
def test_required_permission_and_reference_fields(tmp_path, field):
    data = manifest([image(tmp_path)])
    del data["images"][0][field]
    with pytest.raises(ValidationError) as exc:
        IntakeManifest.model_validate(data)
    assert any(field in r for r in manifest_violations(exc.value))


@pytest.mark.parametrize("over,needle", [
    ({"permission_cleared": False}, "permission_not_cleared"),
    ({"permission": "synthetic"}, "manifest_invalid"),  # live provenance + synthetic permission
    ({"vehicle_present": True, "vehicle_track_id": None}, "manifest_invalid"),
    ({"timestamp_ms": -1}, "manifest_invalid"),
    ({"frame_index": -1}, "manifest_invalid"),
    ({"media_file": "../escape.jpg"}, "manifest_invalid"),
    ({"media_file": "/abs/path.jpg"}, "manifest_invalid"),
    ({"media_file": "car-1/clip.exe"}, "manifest_invalid"),
    ({"labels": [{"id": "l", "concept": "rust", "component_id": "hood",
                  "bbox": {"x": 0.9, "y": 0.1, "w": 0.2, "h": 0.2}, "severity": 3}]}, "manifest_invalid"),
    ({"labels": [{"id": "l", "concept": "dent", "component_id": "hood",
                  "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "severity": 3}]}, "manifest_invalid"),
    ({"labels": [{"id": "l", "concept": "not_a_concept", "component_id": "hood",
                  "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "severity": 3}]}, "manifest_invalid"),
    ({"labels": [{"id": "l", "concept": "rust", "component_id": "hood",
                  "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "severity": 9}]}, "manifest_invalid"),
])
def test_manifest_violations_fail_with_reason(tmp_path, over, needle):
    row = image(tmp_path, **over)
    with pytest.raises(ValidationError) as exc:
        IntakeManifest.model_validate(manifest([row]))
    reasons = manifest_violations(exc.value)
    assert reasons and any(needle in r for r in reasons), reasons
    assert "fake-jpeg" not in " ".join(reasons)  # reasons never echo media/input values


def test_no_vehicle_with_damage_labels_rejected(tmp_path):
    row = image(tmp_path, vehicle=False, track=None)  # default labels still present
    with pytest.raises(ValidationError):
        IntakeManifest.model_validate(manifest([row]))


def test_duplicate_cross_reference_and_leakage_rejected(tmp_path):
    cases = [
        [image(tmp_path, "a"), image(tmp_path, "a", data=b"other")],  # duplicate id
        [image(tmp_path, "a"), image(tmp_path, "b")],  # same media bytes/track frame reuse
        [image(tmp_path, "a"), image(tmp_path, "b", car="car-2", data=b"x2")],  # track spans cars
        [image(tmp_path, "a", split="tuning"), image(tmp_path, "b", data=b"x3", frame_index=1)],  # split leakage, same car
        [image(tmp_path, "a"), image(tmp_path, "b", data=b"x4", media_file="car-1/a.jpg")],  # shared media file
    ]
    for rows in cases:
        with pytest.raises(ValidationError):
            IntakeManifest.model_validate(manifest(rows))


def test_walkaround_cross_car_reference_rejected(tmp_path):
    row = image(tmp_path)
    walk = {"id": "w", "car_id": "other-car", "split": "heldout", "complete": False,
            "image_ids": ["img-1"], "reviewer": "r-1", "source_video_file": "car-1/w.mp4",
            "source_video_sha256": H, "completeness_receipt_sha256": RECEIPT}
    (tmp_path / "car-1/w.mp4").write_bytes(b"v")
    walk["source_video_sha256"] = sha(b"v")
    with pytest.raises(ValidationError):
        IntakeManifest.model_validate(manifest([row], [walk]))


def test_validator_makes_zero_network_calls(tmp_path, monkeypatch):
    import socket
    def blocked(*a, **k):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    receipt = validate(tmp_path, manifest([image(tmp_path)]))
    assert receipt["valid"]


def test_cli_valid_and_invalid(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    data = manifest([image(root)])
    manifest_path, out = tmp_path / "intake.json", tmp_path / "receipt.json"
    manifest_path.write_text(json.dumps(data))
    cmd = [sys.executable, "scripts/validate_dataset_intake.py", "--manifest", str(manifest_path),
           "--media-root", str(root), "--output", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parents[1])
    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text())["valid"] is True
    (root / data["images"][0]["media_file"]).write_bytes(b"tampered")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=Path(__file__).parents[1])
    assert result.returncode == 2 and "media_hash_mismatch:img-1" in result.stderr
    assert json.loads(out.read_text())["valid"] is False
