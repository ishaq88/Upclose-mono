"""Demo-footage intake: stitch ordered snippets into one example video (offline).

Input is a JSON sidecar naming ordered snippet files with per-snippet labels.
Output is one consistently re-encoded MP4 plus a demo manifest recording
per-segment hashes, labels, time ranges in the stitched video, and the
provenance declared in the sidecar. Provenance is recorded verbatim and
never upgraded: `synthetic` stays synthetic, and real footage is only
accepted as `cached` when the sidecar asserts permission was cleared.

Limits are reused, not invented: the single-video ingest limits from
`config/source-video.v1.json` (180 s / 200 MiB) layered on the media config
(containers, codecs, x264 preset/CRF, 1280 proxy long edge). Every snippet
and the stitched total must fit. All validation fails closed and no partial
output is left behind. No network, database, or provider calls are made.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.pipeline.media import (
    MediaConfig,
    MediaError,
    MediaReason,
    _file_url,
    _tool,
    classify_failure,
    load_media_config,
    probe,
    run_tool,
    sha256_file,
    target_size,
)
from app.pipeline.source_mapping import SourceConfig, load_source_config, source_media_config

ALLOWED_PROVENANCE = frozenset({"synthetic", "cached"})
_EXTENSION_TO_CONTAINER = {".mp4": "mp4", ".mov": "mov", ".webm": "webm", ".mkv": "matroska"}
MANIFEST_NAME = "demo-manifest.json"
VIDEO_NAME = "example.mp4"
_DURATION_TOLERANCE_S = 1.0


class IntakeError(ValueError):
    """Sidecar or footage cannot be accepted; nothing was published."""


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    file: str
    source_label: str
    car_label: str
    area_label: str
    start_offset_s: float = 0.0
    duration_s: float | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Sidecar:
    provenance: str
    permission_cleared: bool
    segments: tuple[SegmentSpec, ...]


def _label(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise IntakeError(f"segment field {field!r} must be a non-empty string")
    return value.strip()


def parse_sidecar(path: Path) -> Sidecar:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise IntakeError("sidecar is missing or is not valid JSON") from exc
    if not isinstance(data, dict):
        raise IntakeError("sidecar must be a JSON object")
    provenance = data.get("provenance")
    if provenance not in ALLOWED_PROVENANCE:
        raise IntakeError(
            f"unsupported provenance {provenance!r}; use 'synthetic' for test fixtures "
            "or 'cached' for permission-cleared real footage"
        )
    permission = data.get("permission_cleared", False)
    if not isinstance(permission, bool):
        raise IntakeError("permission_cleared must be a boolean")
    if provenance == "cached" and not permission:
        raise IntakeError("cached (real) footage requires permission_cleared=true in the sidecar")
    if provenance == "synthetic" and permission:
        raise IntakeError("synthetic fixtures must not claim permission-cleared real footage")
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise IntakeError("segments must be a non-empty ordered list")
    segments: list[SegmentSpec] = []
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise IntakeError(f"segments[{index}] must be an object")
        offset = raw.get("start_offset_s", 0.0)
        duration = raw.get("duration_s")
        sha = raw.get("sha256")
        if not isinstance(offset, (int, float)) or isinstance(offset, bool) or offset < 0:
            raise IntakeError(f"segments[{index}].start_offset_s must be a number >= 0")
        if duration is not None and (not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0):
            raise IntakeError(f"segments[{index}].duration_s must be a number > 0 when given")
        if sha is not None and (not isinstance(sha, str) or len(sha) != 64):
            raise IntakeError(f"segments[{index}].sha256 must be a 64-char hex digest when given")
        segments.append(SegmentSpec(
            file=_label(raw.get("file"), "file"),
            source_label=_label(raw.get("source_label"), "source_label"),
            car_label=_label(raw.get("car_label"), "car_label"),
            area_label=_label(raw.get("area_label"), "area_label"),
            start_offset_s=float(offset),
            duration_s=float(duration) if duration is not None else None,
            sha256=sha.lower() if isinstance(sha, str) else None,
        ))
    return Sidecar(provenance=provenance, permission_cleared=permission, segments=tuple(segments))


def _guarded_input(config: MediaConfig, source: Path) -> list[str]:
    return [
        "-protocol_whitelist", "file",
        "-format_whitelist", ",".join(config.demuxer_whitelist),
        "-i", _file_url(source),
    ]


def _encode_args(config: MediaConfig) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", config.x264_preset, "-crf", str(config.x264_crf),
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-movflags", "+faststart", "-f", "mp4", "-y",
    ]


def build_demo_video(
    sidecar_path: Path,
    output_dir: Path,
    *,
    media_config: MediaConfig | None = None,
    source_config: SourceConfig | None = None,
    video_name: str = VIDEO_NAME,
) -> Path:
    """Stitch the sidecar's snippets and write `<output_dir>/<video_name>` + manifest.

    Returns the manifest path. On any failure raises `IntakeError` (or
    `MediaError` wrapped as `IntakeError`) and leaves no output behind.
    Originals are only read; each file's hash is verified before and after.
    """
    sidecar_path = Path(sidecar_path)
    output_dir = Path(output_dir)
    sidecar = parse_sidecar(sidecar_path)
    base_media = media_config or load_media_config()
    limits = source_config or load_source_config()
    config = source_media_config(limits, base_media)
    max_duration_s = limits.max_duration_ms / 1000.0
    base_dir = sidecar_path.resolve().parent

    video_out = output_dir / video_name
    manifest_out = output_dir / MANIFEST_NAME
    if video_out.exists() or manifest_out.exists():
        raise IntakeError("output already exists; refusing to overwrite")

    # Resolve, hash, and probe every snippet before writing anything.
    resolved: list[tuple[SegmentSpec, Path, str, float]] = []
    total_bytes = 0
    total_duration = 0.0
    for spec in sidecar.segments:
        candidate = (base_dir / spec.file).resolve()
        if candidate.suffix.lower() not in _EXTENSION_TO_CONTAINER:
            raise IntakeError(f"unsupported container for snippet {spec.file!r}")
        if not candidate.is_file():
            raise IntakeError(f"snippet is missing or unreadable: {spec.file!r}")
        try:
            digest = sha256_file(candidate)
        except OSError as exc:
            raise IntakeError(f"snippet is missing or unreadable: {spec.file!r}") from exc
        if spec.sha256 is not None and digest != spec.sha256:
            raise IntakeError(f"sha256 mismatch for snippet {spec.file!r}")
        size = candidate.stat().st_size
        total_bytes += size
        if size > limits.max_source_bytes:
            raise IntakeError(f"snippet {spec.file!r} exceeds the ingest size limit")
        resolved.append((spec, candidate, digest, 0.0))
    if total_bytes > limits.max_source_bytes:
        raise IntakeError("total snippet size exceeds the ingest size limit")

    output_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".intake-", dir=output_dir))
    try:
        probed: list[tuple[SegmentSpec, Path, str, float]] = []
        for spec, candidate, digest, _ in resolved:
            try:
                info = probe(candidate, config, work)
            except MediaError as exc:
                raise IntakeError(f"snippet {spec.file!r} rejected: {exc.reason.value}") from exc
            if spec.start_offset_s >= info.duration_s:
                raise IntakeError(f"start_offset_s is beyond the end of snippet {spec.file!r}")
            effective = info.duration_s - spec.start_offset_s
            if spec.duration_s is not None:
                effective = min(effective, spec.duration_s)
            if effective <= 0:
                raise IntakeError(f"snippet {spec.file!r} yields an empty segment")
            total_duration += effective
            probed.append((spec, candidate, digest, effective))
        if total_duration > max_duration_s:
            raise IntakeError("total stitched duration exceeds the ingest duration limit")

        canvas_w, canvas_h = 0, 0
        infos = []
        for spec, candidate, digest, effective in probed:
            info = probe(candidate, config, work)
            infos.append(info)
            w, h = target_size(info.display_width, info.display_height, base_media.proxy_long_edge)
            canvas_w, canvas_h = max(canvas_w, w), max(canvas_h, h)

        # Normalize each segment to the shared canvas, then re-encode the concat.
        segment_paths: list[Path] = []
        segment_durations: list[float] = []
        for index, ((spec, candidate, digest, effective), info) in enumerate(zip(probed, infos)):
            out = work / f"segment-{index:03d}.mp4"
            vf = (
                f"trim=start={spec.start_offset_s:.3f}:duration={effective:.3f},setpts=PTS-STARTPTS,"
                f"scale={canvas_w}:{canvas_h}:flags=bicubic:force_original_aspect_ratio=decrease,"
                f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p"
            )
            run = run_tool(
                [*_tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                 "-threads", str(config.limits.transcode.threads),
                 *_guarded_input(config, candidate), "-map", "0:v:0", "-vf", vf,
                 *_encode_args(base_media), _file_url(out)],
                limits=config.limits.transcode, cwd=work)
            if run.returncode != 0 or run.timed_out:
                raise IntakeError(f"failed to normalize snippet {spec.file!r}: "
                                  f"{classify_failure(run, MediaReason.TRANSCODE_FAILED).reason.value}")
            segment_paths.append(out)
            segment_durations.append(probe(out, config, work).duration_s)

        list_file = work / "concat.txt"
        list_file.write_text("".join(f"file '{p.name}'\n" for p in segment_paths), encoding="utf-8")
        stitched = work / "stitched.mp4"
        run = run_tool(
            [*_tool("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
             "-threads", str(config.limits.transcode.threads),
             "-protocol_whitelist", "file", "-f", "concat", "-safe", "0",
             "-i", _file_url(list_file), "-map", "0:v:0",
             *_encode_args(base_media), _file_url(stitched)],
            limits=config.limits.transcode, cwd=work)
        if run.returncode != 0 or run.timed_out:
            raise IntakeError("failed to stitch segments")
        output_info = probe(stitched, config, work)
        if abs(output_info.duration_s - sum(segment_durations)) > _DURATION_TOLERANCE_S:
            raise IntakeError("stitched duration does not match the segment durations")

        # Re-read originals: any change during the build discards the output.
        for spec, candidate, digest, _ in probed:
            if sha256_file(candidate) != digest:
                raise IntakeError(f"snippet changed during intake: {spec.file!r}")

        segments_json = []
        cursor = 0.0
        for (spec, candidate, digest, _), duration in zip(probed, segment_durations):
            segments_json.append({
                "source_file": spec.file,
                "source_label": spec.source_label,
                "car_label": spec.car_label,
                "area_label": spec.area_label,
                "source_sha256": digest,
                "start_offset_s": round(spec.start_offset_s, 3),
                "stitched_start_s": round(cursor, 3),
                "stitched_end_s": round(cursor + duration, 3),
                "duration_s": round(duration, 3),
            })
            cursor += duration
        manifest = {
            "schema_version": 1,
            "kind": "demo-footage-intake",
            "provenance": sidecar.provenance,
            "permission_cleared": sidecar.permission_cleared,
            "presentation": (
                "synthetic test footage — not real cars"
                if sidecar.provenance == "synthetic"
                else "cached permission-cleared real footage — not fresh live inference"
            ),
            "is_fresh_live_inference": False,
            "model_requests": 0,
            "video_file": video_name,
            "video_sha256": sha256_file(stitched),
            "total_duration_s": round(output_info.duration_s, 3),
            "total_size_bytes": stitched.stat().st_size,
            "width": canvas_w,
            "height": canvas_h,
            "segments": segments_json,
        }
        (work / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        shutil.move(str(stitched), str(video_out))
        shutil.move(str(work / MANIFEST_NAME), str(manifest_out))
    except IntakeError:
        video_out.unlink(missing_ok=True)
        manifest_out.unlink(missing_ok=True)
        raise
    except MediaError as exc:
        video_out.unlink(missing_ok=True)
        manifest_out.unlink(missing_ok=True)
        raise IntakeError(f"media rejected: {exc.reason.value}") from exc
    except BaseException:
        video_out.unlink(missing_ok=True)
        manifest_out.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return manifest_out
