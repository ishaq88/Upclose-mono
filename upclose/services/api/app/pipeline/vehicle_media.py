"""Fail-closed local vehicle masking; no provider, storage or database access."""
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import tempfile
import time
import uuid

from PIL import Image

from app.adapters.meta.types import MediaRef, SegmentObservation
from app.pipeline.media import (
    FrameManifest, MediaConfig, MediaError, MediaReason, build_manifest,
    classify_failure, ensure_plain_file, extract_frames, probe, run_tool, sha256_file,
)

BACKGROUND = (128, 128, 128)
DEFAULT_MAX_OUTPUT_BYTES = 24 * 1024 * 1024
# Same codec profile as the proxy SAM already accepts. Lossless RGB measured 55 MiB for a
# 46-frame 720p close-up (over the inline cap); 18 limits loss on this second-generation encode.
MASKED_VIDEO_CRF = 18
# Source and masked PNGs coexist only for one batch. PNG overhead is bounded by
# allowing twice the RGB raster size per image, with a small fixed allowance.
MAX_BATCH_SCRATCH_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class MaskedVehicleMedia:
    path: Path
    sha256: str
    output_size_bytes: int
    manifest: FrameManifest | None


def _reject(detail: str) -> MediaError:
    return MediaError(MediaReason.INVALID_INPUT, detail)


def _validate_masks(media: MediaRef, observations: Sequence[SegmentObservation]) -> dict[int, SegmentObservation]:
    """Select the inspected vehicle per frame: the largest non-empty mask.

    SAM emits no line for a frame without a visible vehicle, may also segment other
    parked cars, and track ids can change after occlusion. A frame without a selection
    is masked to background, so no damage can be found there; too few frames fails closed.
    """
    if media.rotation_degrees != 0:
        raise _reject("vehicle masking requires upright normalized media")
    known = {frame.frame_index for frame in media.frames}
    by_index: dict[int, SegmentObservation] = {}
    for item in sorted(observations, key=lambda o: (o.frame_index, len(o.track_id), o.track_id)):
        if item.frame_index not in known:
            raise _reject("vehicle observation is not aligned with source frame")
        area = sum(item.mask.pixels)
        current = by_index.get(item.frame_index)
        if area and (current is None or area > sum(current.mask.pixels)):
            by_index[item.frame_index] = item
    if not by_index or len(by_index) * 2 < len(media.frames):
        raise _reject("vehicle is visible in too few frames")
    for frame in media.frames:
        item = by_index.get(frame.frame_index)
        if item is None:
            continue
        if (item.frame_key, item.timestamp_ms, item.frame_width, item.frame_height) != (
            frame.frame_key, frame.timestamp_ms, media.stored_width, media.stored_height,
        ):
            raise _reject("vehicle observation is not aligned with source frame")
        left, top = round(item.bbox.x * item.frame_width), round(item.bbox.y * item.frame_height)
        right = round((item.bbox.x + item.bbox.w) * item.frame_width)
        bottom = round((item.bbox.y + item.bbox.h) * item.frame_height)
        if (item.mask.width, item.mask.height) != (right - left, bottom - top):
            raise _reject("vehicle mask differs from box extent")
    return by_index


def _mask_image(source: Path, destination: Path, item: SegmentObservation | None,
                size: tuple[int, int]) -> None:
    """Keep the selected vehicle's pixels; with no selection the whole frame is background."""
    with Image.open(source) as image:
        if image.size != size or getattr(image, "n_frames", 1) != 1:
            raise _reject("source image geometry differs from vehicle observation")
        image.load()
        mask = Image.new("L", image.size, 0)
        if item is not None:
            local = Image.frombytes("L", (item.mask.width, item.mask.height), bytes(pixel * 255 for pixel in item.mask.pixels))
            mask.paste(local, (round(item.bbox.x * image.width), round(item.bbox.y * image.height)))
        neutral = Image.new("RGB", image.size, BACKGROUND)
        neutral.paste(image.convert("RGB"), (0, 0), mask)
        neutral.save(destination, format="PNG")


def mask_vehicle_media(
    source: Path,
    media_ref: MediaRef,
    observations: Sequence[SegmentObservation],
    scratch: Path,
    config: MediaConfig,
    *,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> MaskedVehicleMedia:
    """Keep only the selected vehicle, retaining original canvas and every frame.

    Video must be normalized and its refs must cover every decoded frame in order.
    Video output is 4:2:0 H.264 like the proxy; still output is lossless PNG. Rational
    timestamps are checked after encoding; unrepresentable timing fails closed.
    The caller owns the returned file; all intermediates/partial outputs are removed.
    """
    src = ensure_plain_file(source)
    if max_output_bytes <= 0 or src.stat().st_size > config.max_source_bytes:
        raise MediaError(MediaReason.RESOURCE_LIMIT, "media byte bound exceeded")
    max_output_bytes = min(max_output_bytes, config.limits.transcode.max_file_bytes)
    width, height = media_ref.stored_width, media_ref.stored_height
    if (max(width, height) > config.max_source_long_edge or min(width, height) > config.max_source_short_edge
            or len(media_ref.frames) > config.max_output_frames):
        raise MediaError(MediaReason.RESOURCE_LIMIT, "masking geometry or frame bound exceeded")
    masks = _validate_masks(media_ref, observations)
    scratch.mkdir(parents=True, exist_ok=True)
    output = scratch / f"vehicle-{uuid.uuid4().hex}.{'png' if media_ref.kind == 'image' else 'mp4'}"
    try:
        with tempfile.TemporaryDirectory(prefix="vehicle-mask-", dir=scratch) as temporary:
            work = Path(temporary)
            manifest = None
            if media_ref.kind == "image":
                _mask_image(src, output, masks[0], (width, height))
            else:
                info = probe(src, config, work)
                if info.rotation.clockwise_degrees or (info.coded_width, info.coded_height) != (width, height):
                    raise _reject("source video geometry differs from media reference")
                original = build_manifest(src, config, work)
                if tuple(frame.frame_index for frame in media_ref.frames) != tuple(range(original.frame_count)):
                    raise _reject("video frame refs do not cover exact source frame order")
                if any(frame.timestamp_ms != round(original.time_seconds(index) * 1000)
                       for index, frame in enumerate(media_ref.frames)):
                    raise _reject("video timestamps differ from source manifest")
                if original.pts[0] != 0:
                    raise _reject("video must have zero-origin timestamps")
                _mask_video_batches(src, output, original, masks, work, config, max_output_bytes)
                manifest = build_manifest(output, config, work)
                if (manifest.width, manifest.height, manifest.frame_count) != (width, height, original.frame_count) or any(
                    manifest.time_fraction(i) != original.time_fraction(i) for i in range(original.frame_count)
                ):
                    raise MediaError(MediaReason.OUTPUT_INVALID, "masked video frame geometry or timing changed")
            size = output.stat().st_size
            if not 0 < size <= max_output_bytes:
                raise MediaError(MediaReason.RESOURCE_LIMIT, "masked output byte bound exceeded")
            return MaskedVehicleMedia(output, sha256_file(output), size, manifest)
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        output.unlink(missing_ok=True)
        raise MediaError(MediaReason.CORRUPT, "vehicle media could not be decoded or written") from exc
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def _mask_video_batches(
    source: Path, output: Path, original: FrameManifest,
    masks: dict[int, SegmentObservation], work: Path, config: MediaConfig,
    max_output_bytes: int,
) -> None:
    # All subprocess calls retain the standard sandbox/rlimits. One shared deadline
    # prevents a many-batch input from multiplying the transcode wall-time budget.
    deadline = time.monotonic() + config.limits.transcode.wall_time_s
    per_frame = original.width * original.height * 12 + 4096
    batch_size = min(config.max_extract_frames, MAX_BATCH_SCRATCH_BYTES // per_frame)
    if batch_size < 1:
        raise MediaError(MediaReason.RESOURCE_LIMIT, "one masking frame exceeds scratch bound")
    chunks: list[str] = ["ffconcat version 1.0"]
    compressed_bytes = 0
    for start in range(0, original.frame_count, batch_size):
        stop = min(start + batch_size, original.frame_count)
        with tempfile.TemporaryDirectory(prefix="batch-", dir=work) as temporary:
            batch = Path(temporary)
            bounded = _remaining_config(config, deadline)
            frames = extract_frames(source, original, range(start, stop), batch, bounded, image_format="png")
            lines = ["ffconcat version 1.0"]
            for index, frame in zip(range(start, stop), frames):
                name = f"masked_{index:06d}.png"
                _mask_image(frame.path, batch / name, masks.get(index), (original.width, original.height))
                frame.path.unlink()
                lines.extend([f"file {name}", f"option framerate {original.time_base_den}/{original.time_base_num}"])
                if index + 1 < stop:
                    duration = original.time_fraction(index + 1) - original.time_fraction(index)
                    lines.append(f"duration {float(duration):.12f}")
            (batch / "frames.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
            chunk = work / f"chunk_{start:06d}.mp4"
            _run_encode([
                "-protocol_whitelist", "file", "-f", "concat", "-safe", "0", "-i", str(batch / "frames.txt"),
                "-an", "-map_metadata", "-1", "-fps_mode", "passthrough", "-vf", "scale=out_color_matrix=bt709",
                "-c:v", "libx264", "-crf", str(MASKED_VIDEO_CRF), "-preset", config.x264_preset,
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-colorspace", "bt709",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-threads", "1",
                "-enc_time_base", f"{original.time_base_num}:{original.time_base_den}",
                "-video_track_timescale", str(original.time_base_den),
            ], chunk, batch, _remaining_config(config, deadline), max_output_bytes)
            compressed_bytes += chunk.stat().st_size
            # Chunk containers carry overhead removed by concatenation; allow bounded
            # overhead per batch, never unbounded decoded files or compressed chunks.
            if compressed_bytes > max_output_bytes + 4096 * ((start // batch_size) + 1):
                raise MediaError(MediaReason.RESOURCE_LIMIT, "masked chunk byte bound exceeded")
            chunks.append(f"file {chunk.name}")
            if stop < original.frame_count:
                duration = original.time_fraction(stop) - original.time_fraction(start)
                chunks.append(f"duration {float(duration):.12f}")
    (work / "chunks.txt").write_text("\n".join(chunks) + "\n", encoding="ascii")
    _run_encode([
        "-protocol_whitelist", "file", "-f", "concat", "-safe", "0", "-i", str(work / "chunks.txt"),
        "-an", "-map_metadata", "-1", "-c:v", "copy", "-video_track_timescale", str(original.time_base_den),
    ], output, work, _remaining_config(config, deadline), max_output_bytes)


def _remaining_config(config: MediaConfig, deadline: float) -> MediaConfig:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaError(MediaReason.TIMEOUT, "vehicle masking exceeded wall-clock limit")
    limits = config.limits.model_copy(update={
        name: getattr(config.limits, name).model_copy(update={
            "wall_time_s": min(remaining, getattr(config.limits, name).wall_time_s),
        }) for name in ("extract", "transcode")
    })
    return config.model_copy(update={"limits": limits})


def _run_encode(args: list[str], output: Path, work: Path, config: MediaConfig, byte_limit: int) -> None:
    run = run_tool([
        "ffmpeg", "-nostdin", "-v", "error", "-threads", str(config.limits.transcode.threads),
        *args, "-f", "mp4", "-y", str(output),
    ], limits=config.limits.transcode.model_copy(update={
        "max_file_bytes": min(byte_limit, config.limits.transcode.max_file_bytes),
    }), cwd=work)
    if run.returncode or run.timed_out:
        raise classify_failure(run, MediaReason.TRANSCODE_FAILED, str(work), str(output))
