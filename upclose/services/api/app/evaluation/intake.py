"""Dataset intake validation: manifest + on-disk media, before any evaluation run.

Extends the strict `DatasetManifest` contract with the drop-in checks that the
offline manifest alone cannot make: referenced files exist under a media root,
their bytes hash to the declared SHA-256, a vehicle-presence/track declaration
agrees with the damage labels, and permission is explicitly cleared. Every
violation fails closed with a specific reason code; nothing is coerced and no
provider, network, storage, or database call is ever made.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from app.contracts.enums import Concept
from .metrics import input_hash
from .schema import (
    Box, DatasetManifest, Hash, Identifier, LabeledImage, Provenance, Split,
    StrictModel,
)

INTAKE_SCHEMA_VERSION = "upclose-dataset-intake-v1"
INTAKE_RECEIPT_VERSION = "upclose-dataset-intake-receipt-v1"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MEDIA_BYTES = 1024 * 1024 * 1024
ALLOWED_MEDIA_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".mkv"})

MediaRelPath = Annotated[str, Field(min_length=1, max_length=512)]
TimestampMs = Annotated[int, Field(strict=True, ge=0, le=86_400_000)]  # up to 24 h of footage
FrameIndex = Annotated[int, Field(strict=True, ge=0, le=100_000_000)]


def _check_relpath(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or ".." in path.parts or not path.name:
        raise ValueError("media_file must be a relative path inside the media root")
    if path.suffix.lower() not in ALLOWED_MEDIA_SUFFIXES:
        raise ValueError("media_file has an unsupported media suffix")
    return value


class IntakeImage(StrictModel):
    id: Identifier
    car_id: Identifier
    frame_id: Identifier
    timestamp_ms: TimestampMs
    frame_index: FrameIndex
    media_file: MediaRelPath
    media_sha256: Hash
    vehicle_present: Annotated[bool, Field(strict=True)]
    vehicle_track_id: Identifier | None = None
    split: Split
    part_no: Annotated[int, Field(strict=True, ge=1, le=8)]
    permission: Literal["synthetic", "license", "consent"]
    permission_cleared: Annotated[bool, Field(strict=True)]
    permission_receipt_sha256: Hash
    reviewer: Identifier
    blur: Literal["clear", "blurry", "mixed"]
    lighting: Literal["daylight", "artificial", "low", "mixed"]
    reviewed_concepts: Annotated[tuple[Concept, ...], Field(min_length=1, max_length=7)]
    rust_category: Literal["positive", "clean", "confounder", "not_reviewed"]
    labels: Annotated[tuple[Box, ...], Field(max_length=200)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        _check_relpath(self.media_file)
        if self.vehicle_present and self.vehicle_track_id is None:
            raise ValueError("vehicle_track_id required when vehicle_present")
        if not self.vehicle_present and self.vehicle_track_id is not None:
            raise ValueError("vehicle_track_id forbidden when no vehicle is present")
        if not self.vehicle_present and self.labels:
            raise ValueError("damage labels require a present vehicle")
        # Reuse the labeled-image contract (duplicate/unreviewed/rust rules).
        LabeledImage.model_validate(self._as_labeled())
        return self

    def _as_labeled(self) -> dict:
        return {
            "id": self.id, "car_id": self.car_id, "frame_id": self.frame_id,
            "media_sha256": self.media_sha256, "split": self.split,
            "part_no": self.part_no, "permission": self.permission,
            "permission_receipt_sha256": self.permission_receipt_sha256,
            "reviewer": self.reviewer, "blur": self.blur, "lighting": self.lighting,
            "reviewed_concepts": self.reviewed_concepts,
            "rust_category": self.rust_category, "labels": self.labels,
        }


class IntakeWalkaround(StrictModel):
    id: Identifier
    car_id: Identifier
    split: Split
    complete: Annotated[bool, Field(strict=True)]
    image_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=10000)]
    reviewer: Identifier
    source_video_file: MediaRelPath
    source_video_sha256: Hash
    completeness_receipt_sha256: Hash

    @model_validator(mode="after")
    def coherent(self) -> Self:
        _check_relpath(self.source_video_file)
        return self

    def _as_walkaround(self) -> dict:
        return {
            "id": self.id, "car_id": self.car_id, "split": self.split,
            "complete": self.complete, "image_ids": self.image_ids,
            "reviewer": self.reviewer,
            "source_video_sha256": self.source_video_sha256,
            "completeness_receipt_sha256": self.completeness_receipt_sha256,
        }


class IntakeManifest(StrictModel):
    schema_version: Literal["upclose-dataset-intake-v1"]
    provenance: Provenance
    images: Annotated[tuple[IntakeImage, ...], Field(min_length=1, max_length=10000)]
    walkarounds: Annotated[tuple[IntakeWalkaround, ...], Field(max_length=1000)] = ()

    @model_validator(mode="after")
    def cross_checks(self) -> Self:
        # Manifest-wide identities/splits/leakage via the evaluation contract.
        DatasetManifest.model_validate(self.as_dataset())
        for image in self.images:
            if not image.permission_cleared:
                raise ValueError(f"permission_not_cleared:{image.id}")
            if self.provenance != "synthetic" and image.permission == "synthetic":
                raise ValueError("non-synthetic provenance requires licensed or consented media declaration")
        files = [i.media_file for i in self.images] + [w.source_video_file for w in self.walkarounds]
        if len(set(files)) != len(files):
            raise ValueError("media file referenced by more than one item")
        tracks: dict[str, str] = {}
        for image in self.images:
            track = image.vehicle_track_id
            if track is None:
                continue
            if tracks.setdefault(track, image.car_id) != image.car_id:
                raise ValueError(f"vehicle track {track} spans multiple cars")
        seen_frames: set[tuple[str, int]] = set()
        for image in self.images:
            key = (image.car_id, image.frame_index)
            if key in seen_frames:
                raise ValueError("duplicate car/frame_index reference")
            seen_frames.add(key)
        return self

    def as_dataset(self) -> dict:
        return {
            "schema_version": "upclose-dataset-v1", "provenance": self.provenance,
            "images": [i._as_labeled() for i in self.images],
            "walkarounds": [w._as_walkaround() for w in self.walkarounds],
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_media(media_root: Path, rel: str, expected: str, item_id: str,
                 kind: str, violations: list[str]) -> None:
    path = (media_root / rel).resolve()
    if media_root not in path.parents and path != media_root:
        violations.append(f"{kind}_path_escape:{item_id}")
        return
    if not path.is_file():
        violations.append(f"{kind}_missing:{item_id}")
        return
    try:
        if path.stat().st_size == 0:
            violations.append(f"{kind}_empty:{item_id}")
            return
        if path.stat().st_size > MAX_MEDIA_BYTES:
            violations.append(f"{kind}_too_large:{item_id}")
            return
        if _file_sha256(path) != expected:
            violations.append(f"{kind}_hash_mismatch:{item_id}")
    except OSError:
        violations.append(f"{kind}_unreadable:{item_id}")


def validate_dataset_intake(manifest: IntakeManifest, media_root: Path) -> dict:
    """Check every referenced file exists with matching bytes. Fails closed."""
    root = media_root.resolve()
    violations: list[str] = []
    if not root.is_dir():
        violations.append("media_root_missing")
    else:
        for image in manifest.images:
            _check_media(root, image.media_file, image.media_sha256, image.id, "media", violations)
        for walk in manifest.walkarounds:
            _check_media(root, walk.source_video_file, walk.source_video_sha256,
                         walk.id, "walkaround_media", violations)
    violations.sort()
    return {
        "schema_version": INTAKE_RECEIPT_VERSION,
        "intake_schema_version": INTAKE_SCHEMA_VERSION,
        "valid": not violations,
        "declared_provenance": manifest.provenance,
        "provenance_verified": False,
        "counts": {
            "images": len(manifest.images),
            "walkarounds": len(manifest.walkarounds),
            "cars": len({i.car_id for i in manifest.images}),
            "labels": sum(len(i.labels) for i in manifest.images),
            "media_files_checked": len(manifest.images) + len(manifest.walkarounds),
        },
        "violations": violations,
        "gates": {
            "dataset_intake": "passed" if not violations else "failed",
            "live_quality": "unverified", "live_feasibility": "unverified",
            "standard_access": "unverified",
        },
        "limitations": [
            "Intake checks declared manifest fields and on-disk bytes only; it does not "
            "verify permission truth, vehicle/damage truth, or provider access.",
            "A passing intake permits an offline evaluation run; it is not a live gate.",
        ],
    }


def manifest_violations(error: ValidationError) -> list[str]:
    """Specific, value-free reasons from a validation error (never raw input)."""
    import re
    reasons = []
    for err in error.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "manifest"
        reason = f"manifest_invalid:{loc}:{err['type']}"
        ctx_error = (err.get("ctx") or {}).get("error")
        if isinstance(ctx_error, ValueError):
            message = str(ctx_error)[:128]
            if re.fullmatch(r"[A-Za-z0-9_:.\- ]+", message):  # our own static messages only
                reason = f"{reason}:{message}"
        reasons.append(reason)
    return sorted(reasons)


__all__ = [
    "INTAKE_RECEIPT_VERSION", "INTAKE_SCHEMA_VERSION", "IntakeManifest",
    "manifest_violations", "validate_dataset_intake", "input_hash",
]
