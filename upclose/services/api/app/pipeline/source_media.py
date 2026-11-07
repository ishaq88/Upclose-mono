"""Extract declared ranges with exact source-frame identity and bounded local decoding.

Selection is by decoded frame index. No time-based seek, fps interpolation, or nearest-frame
substitution occurs after selection. Output frame count and relative timestamps must agree
with the selected source frames before provenance can be published.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from app.contracts.video import VideoRange
from app.pipeline.media import (
    FrameManifest, MediaConfig, MediaError, MediaReason, NormalizeResult, ProbeResult,
    _finish_normalize, _normalize_args, classify_failure, ensure_plain_file,
    run_tool, target_size,
)


@dataclass(frozen=True, slots=True)
class SourceRangeResult:
    normalized: NormalizeResult
    source_frame_indices: tuple[int, ...]
    frame_timestamps_ms: tuple[int, ...]


def source_frame_times(manifest: FrameManifest) -> tuple[Fraction, ...]:
    """Original video presentation timestamps, before proxy timestamp normalization.

    Do not subtract the first video frame: an audio/video start offset is real source
    timing and must not shift a user's marked range. Negative source timelines need
    separate edit-list handling and fail closed in this bounded implementation.
    """
    if manifest.time_fraction(0) < 0:
        raise MediaError(MediaReason.INVALID_INPUT, "negative source timeline is unsupported")
    return tuple(manifest.time_fraction(index) for index in range(manifest.frame_count))


def select_range_frames(manifest: FrameManifest, segment: VideoRange,
                        sample_fps: float | None) -> tuple[int, ...]:
    times = source_frame_times(manifest)
    start, end = Fraction(segment.start_ms, 1000), Fraction(segment.end_ms, 1000)
    candidates = tuple(index for index, value in enumerate(times) if start <= value < end)
    if not candidates:
        raise MediaError(MediaReason.INVALID_INPUT, "source range contains no video frames")
    if sample_fps is None:
        return candidates
    # Choose actual source frames on/after each cadence point. Never duplicate a frame;
    # a low-fps input naturally stays low-fps. Source selection remains wholly in range.
    step = Fraction(1, 1) / Fraction(str(sample_fps))
    selected: list[int] = []
    next_time = times[candidates[0]]
    for index in candidates:
        if times[index] >= next_time:
            selected.append(index)
            while next_time <= times[index]:
                next_time += step
    return tuple(selected)


def normalize_source_range(source: Path, scratch: Path, media: MediaConfig,
                           info: ProbeResult, manifest: FrameManifest,
                           segment: VideoRange, source_sha256: str) -> SourceRangeResult:
    """Use the existing guarded normalization command with explicit source frame selection."""
    src = ensure_plain_file(source)
    frame_plan = media.plan_for(segment.part_no)
    indices = select_range_frames(manifest, segment, frame_plan.proxy_fps(info.frame_rate))
    if len(indices) > media.max_output_frames:
        raise MediaError(MediaReason.RESOURCE_LIMIT, "range contains too many frames")
    out = scratch / "proxy.mp4"
    width, height = target_size(info.display_width, info.display_height, media.proxy_long_edge)
    args = _normalize_args(media, src, out, info, width, height, None)
    filter_position = args.index("-vf") + 1
    selection = "select='" + "+".join(f"eq(n,{index})" for index in indices) + "'"
    args[filter_position] = f"{selection},setpts=PTS-STARTPTS," + args[filter_position]
    # Keep the filter/encoder time base precise; default encoder time bases may round
    # variable-rate frame times before they reach the output manifest.
    args[args.index("-c:v"):args.index("-c:v")] = ["-enc_time_base", "-1"]
    try:
        run = run_tool(args, limits=media.limits.transcode, cwd=scratch)
        if run.returncode != 0 or run.timed_out:
            raise classify_failure(run, MediaReason.TRANSCODE_FAILED, str(src), str(out))
        normalized = _finish_normalize(out, scratch, media, info, width, height, source_sha256)
        times = source_frame_times(manifest)
        output = normalized.manifest
        if output.frame_count != len(indices):
            raise MediaError(MediaReason.OUTPUT_INVALID, "range frame count changed during normalization")
        for position, index in enumerate(indices):
            actual = output.time_fraction(position) - output.time_fraction(0)
            expected = times[index] - times[indices[0]]
            # Time-base conversion may round one output tick; it cannot silently
            # substitute another frame or lose whole source-frame intervals.
            tolerance = Fraction(output.time_base_num, output.time_base_den)
            if abs(actual - expected) > tolerance:
                raise MediaError(MediaReason.OUTPUT_INVALID, "range frame timing changed during normalization")
        timestamps = tuple(int(times[index] * 1000) for index in indices)
        if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
            raise MediaError(MediaReason.OUTPUT_INVALID, "source time resolution is unsupported")
        return SourceRangeResult(normalized, indices, timestamps)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
