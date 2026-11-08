"""Reduced vehicle-mask input: keyframe selection, nearest-keyframe mapping, mask upsampling.

The vehicle mask is requested on a derived video (fewer frames and/or a smaller long edge) and
then expressed on every original proxy frame, so downstream masking, manifests and damage
sampling keep seeing the full proxy. Pure functions; no provider, storage or database access.
"""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence

from PIL import Image

from app.adapters.meta.types import MaskRaster, MediaRef, SegmentObservation
from app.pipeline.media import FrameManifest, target_size


def mask_keyframe_indices(manifest: FrameManifest, sample_fps: float | None) -> tuple[int, ...]:
    """Proxy frame indices sent for masking: the frame nearest each 1/sample_fps tick.

    Same cadence rule as part sampling (`plan_frame_indices`); null, or a rate at or above the
    proxy's own, keeps every frame. Ascending and unique.
    """
    if sample_fps is None:
        return tuple(range(manifest.frame_count))
    duration = manifest.duration_seconds
    targets = [step / sample_fps for step in range(max(1, round(duration * sample_fps)))]
    return tuple(sorted({manifest.nearest_index(target) for target in targets}))


def nearest_keyframe_positions(manifest: FrameManifest, keyframes: Sequence[int]) -> tuple[int, ...]:
    """For every proxy frame, the position (in `keyframes`) of the keyframe nearest in time.

    Ties go to the earlier keyframe. A keyframe maps to itself.
    """
    if not keyframes or list(keyframes) != sorted(set(keyframes)):
        raise ValueError("keyframes must be non-empty, ascending and unique")
    if keyframes[0] < 0 or keyframes[-1] >= manifest.frame_count:
        raise ValueError("keyframe outside the manifest")
    key_pts = [manifest.pts[i] for i in keyframes]
    out = []
    for pts in manifest.pts:
        at = bisect_left(key_pts, pts)
        if at == 0:
            out.append(0)
        elif at == len(key_pts):
            out.append(len(key_pts) - 1)
        else:
            out.append(at - 1 if pts - key_pts[at - 1] <= key_pts[at] - pts else at)
    return tuple(out)


def derived_size(manifest: FrameManifest, long_edge: int | None) -> tuple[int, int]:
    """Even size of the masking video: the proxy size, or its long edge limited (never upscaled)."""
    if long_edge is None:
        return manifest.width, manifest.height
    return target_size(manifest.width, manifest.height, long_edge)


def _upsample(item: SegmentObservation, width: int, height: int) -> tuple[MaskRaster, dict[str, float]]:
    """Resize a box-local binary raster onto the same box expressed in a larger frame.

    Bilinear on 0/255 then a 50% threshold: smoother than nearest-neighbour blocks and still binary.
    """
    left, top = round(item.bbox.x * width), round(item.bbox.y * height)
    right, bottom = round((item.bbox.x + item.bbox.w) * width), round((item.bbox.y + item.bbox.h) * height)
    box_w, box_h = max(1, right - left), max(1, bottom - top)
    source = Image.frombytes("L", (item.mask.width, item.mask.height), item.mask.pixels.translate(_TO_255))
    resized = source.resize((box_w, box_h), Image.BILINEAR).point(_THRESHOLD)
    return (MaskRaster(width=box_w, height=box_h, pixels=resized.tobytes().translate(_TO_01)),
            {"x": left / width, "y": top / height, "w": box_w / width, "h": box_h / height})


_TO_255 = bytes([0, 255] + [255] * 254)
_TO_01 = bytes([0] + [1] * 255)
_THRESHOLD = [0] * 128 + [255] * 128


def expand_to_proxy_frames(
    proxy: MediaRef, derived: MediaRef, keyframes: Sequence[int], positions: Sequence[int],
    observations: Sequence[SegmentObservation],
) -> tuple[SegmentObservation, ...]:
    """Re-express keyframe-video observations on every proxy frame.

    `observations` are indexed by position in the derived video (0..len(keyframes)-1). Each proxy
    frame takes the observations of its nearest keyframe, resized to the proxy's frame size when the
    derived video was smaller. A keyframe without a vehicle gives its neighbours none, exactly as a
    frame SAM did not report does in the full-rate path.
    """
    if len(keyframes) != len(derived.frames) or len(positions) != len(proxy.frames):
        raise ValueError("keyframe mapping does not match the media")
    by_position: dict[int, list[SegmentObservation]] = {}
    for item in observations:
        if not 0 <= item.frame_index < len(keyframes):
            raise ValueError("observation outside the derived video")
        by_position.setdefault(item.frame_index, []).append(item)
    width, height = proxy.stored_width, proxy.stored_height
    same_size = (derived.stored_width, derived.stored_height) == (width, height)
    converted: dict[int, list[SegmentObservation]] = {}
    for position, items in by_position.items():
        if same_size:
            converted[position] = items
            continue
        scaled = []
        for item in items:
            raster, bbox = _upsample(item, width, height)
            scaled.append(item.model_copy(update={"frame_width": width, "frame_height": height,
                                                  "bbox": item.bbox.model_copy(update=bbox), "mask": raster}))
        converted[position] = scaled
    out = []
    for frame, position in zip(proxy.frames, positions):
        for item in converted.get(position, ()):
            out.append(item.model_copy(update={"frame_index": frame.frame_index, "frame_key": frame.frame_key,
                                               "timestamp_ms": frame.timestamp_ms}))
    return tuple(out)
