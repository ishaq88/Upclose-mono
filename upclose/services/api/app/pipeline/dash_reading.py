"""Dash reading: pick the best instrument-cluster frame with SAM, crop it, have a reader
read the lit warning lights, and assess.

Boundaries: SAM runs through an injected `sam_call(media, prompt_id, request_id)` (core binds
`run_sam_request` with budget dispatch); the reader is an injected DashReader. Images are
passed as bytes and the crop is returned for core to persist (see `persist_crop`). A frame
that is too blurry, a missing cluster or too few frames is not_readable, never clean.
"""
from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image, ImageFilter, ImageStat

from app.adapters.dash_reader import DashReader, ReaderOutput
from app.adapters.meta import MediaRef, ProviderFailure, SegmentObservation, SegmentResult
from app.contracts.evidence import BoundingBox
from app.domain.dash_lights import DashAssessment, DashConfig, assess

SamCall = Callable[[MediaRef, str, UUID], SegmentResult]
MAX_IMAGE_PIXELS = 16_000_000


@dataclass(frozen=True)
class DashStill:
    media: MediaRef  # kind "image", one frame
    image: bytes


@dataclass(frozen=True)
class Candidate:
    still_index: int
    frame_key: str
    bbox: BoundingBox
    area_fraction: Decimal  # mask pixels as a fraction of the whole frame
    sharpness: Decimal  # Laplacian variance of the cluster box
    score: Decimal


@dataclass(frozen=True)
class DashCrop:
    png: bytes
    sha256: str
    width: int
    height: int
    source_frame_key: str
    source_bbox: BoundingBox  # the crop region inside the source frame, with margin
    cluster_bbox: BoundingBox  # the SAM cluster box without margin


@dataclass(frozen=True)
class FrameSelection:
    crop: DashCrop | None
    reason: str | None
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class DashRun:
    assessment: DashAssessment
    selection: FrameSelection
    reader: ReaderOutput | None


# ---- pure helpers ------------------------------------------------------------------------------

_LAPLACIAN = ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=128)


def laplacian_variance(gray: Image.Image) -> float:
    """Variance of the 4-neighbour Laplacian of an 8-bit luma crop (local copy: the shared
    quality module imports POSIX-only process limits and cannot load on Windows)."""
    if gray.width < 3 or gray.height < 3:
        return 0.0
    filtered = gray.convert("L").filter(_LAPLACIAN)  # PIL leaves the 1px border unfiltered
    return float(ImageStat.Stat(filtered.crop((1, 1, gray.width - 1, gray.height - 1))).var[0])

def mask_area_fraction(obs: SegmentObservation) -> Decimal:
    """Mask pixels as a fraction of the full frame (the raster is box-local)."""
    set_pixels = sum(obs.mask.pixels)
    return Decimal(set_pixels) / Decimal(obs.frame_width * obs.frame_height)


def best_observation(observations: Sequence[SegmentObservation]) -> SegmentObservation | None:
    if not observations:
        return None
    return max(observations, key=lambda o: (mask_area_fraction(o), o.confidence or 0.0))


def score_candidate(area_fraction: Decimal, sharpness: Decimal, config: DashConfig) -> Decimal:
    f = config.frames
    area_part = min(Decimal(1), area_fraction / f.area_target_fraction)
    sharp_part = min(Decimal(1), sharpness / f.sharpness_reference)
    return f.area_weight * area_part + f.sharpness_weight * sharp_part


def choose(candidates: Sequence[Candidate]) -> Candidate | None:
    """Highest score; ties go to the earliest still."""
    return max(candidates, key=lambda c: (c.score, -c.still_index), default=None)


def crop_box(bbox: BoundingBox, margin: Decimal) -> BoundingBox:
    m = float(margin)
    x0, y0 = max(0.0, bbox.x - bbox.w * m), max(0.0, bbox.y - bbox.h * m)
    x1, y1 = min(1.0, bbox.x + bbox.w * (1 + m)), min(1.0, bbox.y + bbox.h * (1 + m))
    return BoundingBox(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def _pixel_box(image: Image.Image, bbox: BoundingBox) -> tuple[int, int, int, int]:
    left, top = int(bbox.x * image.width), int(bbox.y * image.height)
    right = min(image.width, max(left + 1, round((bbox.x + bbox.w) * image.width)))
    bottom = min(image.height, max(top + 1, round((bbox.y + bbox.h) * image.height)))
    return left, top, right, bottom


def _open(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as picture:
        if picture.width * picture.height > MAX_IMAGE_PIXELS or getattr(picture, "n_frames", 1) != 1:
            raise ValueError("image bounds")
        picture.load()
        return picture.convert("RGB")


# ---- frame selection ---------------------------------------------------------------------------

def select_cluster(stills: Sequence[DashStill], sam_call: SamCall, config: DashConfig) -> FrameSelection:
    """Run the cluster prompt on each still, pick the best frame, crop it with a margin."""
    f = config.frames
    if len(stills) < f.min_frames:
        return FrameSelection(None, "too_few_frames")
    prompt_id = config.sam_prompt.prompt_id
    candidates: list[Candidate] = []
    images: dict[int, Image.Image] = {}
    first_retryable: ProviderFailure | None = None
    usable = 0
    for index, still in enumerate(stills):
        try:
            image = _open(still.image)
            usable += 1
        except (OSError, ValueError):
            continue
        try:
            result = sam_call(still.media, prompt_id, uuid4())
        except ProviderFailure as error:
            if error.retryable and first_retryable is None:
                first_retryable = error
            continue
        obs = best_observation(result.observations)
        if obs is None:
            continue
        area = mask_area_fraction(obs)
        if area < f.min_cluster_area_fraction:
            continue
        sharp = Decimal(str(laplacian_variance(image.crop(_pixel_box(image, obs.bbox)).convert("L"))))
        images[index] = image
        candidates.append(Candidate(index, still.media.media_key, obs.bbox, area, sharp,
                                    score_candidate(area, sharp, config)))
    if not candidates:
        if first_retryable is not None:
            raise first_retryable  # transport trouble is a retry, not an inspection result
        return FrameSelection(None, "too_few_frames" if usable < f.min_frames else "cluster_not_found")
    best = choose(candidates)
    assert best is not None
    if best.sharpness < f.min_sharpness:
        return FrameSelection(None, "cluster_blurry", tuple(candidates))
    region = crop_box(best.bbox, f.crop_margin_fraction)
    image = images[best.still_index]
    crop = image.crop(_pixel_box(image, region))
    out = io.BytesIO()
    crop.save(out, format="PNG", optimize=False)
    png = out.getvalue()
    return FrameSelection(DashCrop(png, hashlib.sha256(png).hexdigest(), crop.width, crop.height,
                                   best.frame_key, region, best.bbox), None, tuple(candidates))


def persist_crop(storage: object, key: str, crop: DashCrop) -> None:
    """Store the crop through ObjectStorage.put_file, like other evidence frames."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "crop.png"
        path.write_bytes(crop.png)
        storage.put_file(key, path, "image/png")  # type: ignore[attr-defined]


# ---- full run ----------------------------------------------------------------------------------

def run_dash_reading(stills: Sequence[DashStill], sam_call: SamCall, reader: DashReader,
                     config: DashConfig, *, obd_mil_on: bool | None = None,
                     grant=None) -> DashRun:  # type: ignore[no-untyped-def]
    selection = select_cluster(stills, sam_call, config)
    if selection.crop is None:
        return DashRun(assess(None, config, unreadable_reason=selection.reason), selection, None)
    output = reader.read(selection.crop.png, grant=grant)  # transport failures propagate for retry
    if output.reading is None:
        return DashRun(assess(None, config, unreadable_reason=output.reject_code or "reader_failed"),
                       selection, output)
    return DashRun(assess(output.reading, config, obd_mil_on=obd_mil_on), selection, output)
