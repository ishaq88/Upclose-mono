"""Bounded media utilities: probe, normalize, frame manifest and extraction.

No database, storage or job-queue dependencies. Every ffmpeg/ffprobe call goes through
`run_tool`: argument lists only (never a shell), local plain files only, a protocol and
demuxer whitelist, a minimal environment, a wall-clock timeout and POSIX rlimits.

Numeric limits come from `config/media.v1.json` and are provisional design defaults.
"""

import hashlib
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from bisect import bisect_left
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Final, Literal, Self

from PIL import Image
from pydantic import Field, model_validator

from app.contracts.base import ContractModel
from app.domain.config import HashedConfig

try:  # POSIX only; the API image is Linux. Importing must not break other hosts.
    import resource
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    resource = None  # type: ignore[assignment]

DEFAULT_CONFIG_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "config" / "media.v1.json"
MANIFEST_VERSION: Final[str] = "frame-manifest-v1"
SCRATCH_PREFIX: Final[str] = "upclose-media-"
SCRATCH_MARKER: Final[str] = ".upclose-scratch"
_SIGNAL_RESOURCE: Final[frozenset[int]] = frozenset(
    {-int(sig) for sig in (signal.SIGXCPU, signal.SIGXFSZ, signal.SIGKILL, signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS)}
)
_RESOURCE_STDERR: Final[tuple[str, ...]] = (
    "cannot allocate memory",
    "out of memory",
    "file too large",
    "failed to map segment",
    "memory allocation",
    "memoryerror",
    "error while loading shared libraries",
)
_ROTATION_FILTERS: Final[dict[int, str]] = {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}


# --------------------------------------------------------------------------- errors


class MediaReason(StrEnum):
    INVALID_INPUT = "invalid_input"
    CORRUPT = "corrupt"
    NO_VIDEO = "no_video"
    MULTIPLE_VIDEO_STREAMS = "multiple_video_streams"
    UNSUPPORTED_CONTAINER = "unsupported_container"
    UNSUPPORTED_CODEC = "unsupported_codec"
    UNSUPPORTED_ROTATION = "unsupported_rotation"
    DURATION_EXCEEDED = "duration_exceeded"
    DIMENSIONS_EXCEEDED = "dimensions_exceeded"
    RESOURCE_LIMIT = "resource_limit"
    TIMEOUT = "timeout"
    TRANSCODE_FAILED = "transcode_failed"
    OUTPUT_INVALID = "output_invalid"
    EXTRACT_FAILED = "extract_failed"


class MediaError(Exception):
    """Typed media rejection. `detail` never contains filesystem paths."""

    def __init__(self, reason: MediaReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- config


class ProcessLimits(ContractModel):
    wall_time_s: Annotated[float, Field(gt=0, le=3600)]
    cpu_seconds: Annotated[int, Field(strict=True, ge=1, le=7200)]
    address_space_bytes: Annotated[int, Field(strict=True, ge=1)]
    max_file_bytes: Annotated[int, Field(strict=True, ge=1)]
    threads: Annotated[int, Field(strict=True, ge=1, le=8)]


class ToolLimits(ContractModel):
    probe: ProcessLimits
    transcode: ProcessLimits
    extract: ProcessLimits


class FramePlan(ContractModel):
    part_number: Annotated[int, Field(strict=True, ge=1, le=8)]
    mode: Literal["video", "stills"]
    purpose: Annotated[str, Field(min_length=1, max_length=64)]
    sample_fps: Annotated[float, Field(gt=0, le=60)] | None = None
    count: Annotated[int, Field(strict=True, ge=1, le=64)] | None = None

    @model_validator(mode="after")
    def coherent_mode(self) -> Self:
        if self.mode == "video" and (self.sample_fps is None or self.count is not None):
            raise ValueError("video plan requires sample_fps and no count")
        if self.mode == "stills" and (self.count is None or self.sample_fps is not None):
            raise ValueError("stills plan requires count and no sample_fps")
        return self

    def proxy_fps(self, source_fps: float | None) -> float | None:
        """Target proxy frame rate: the sampled rate, never above the source rate."""
        if self.mode != "video":
            return None
        assert self.sample_fps is not None
        return self.sample_fps if source_fps is None else min(self.sample_fps, source_fps)


class QualityConfig(ContractModel):
    provisional: bool
    source: Annotated[str, Field(min_length=1, max_length=512)]
    sample_count: Annotated[int, Field(strict=True, ge=1, le=32)]
    working_long_edge: Annotated[int, Field(strict=True, ge=16)]
    working_short_edge: Annotated[int, Field(strict=True, ge=16)]
    blur_warn_below: Annotated[float, Field(ge=0)]
    blur_strong_below: Annotated[float, Field(ge=0)]
    blur_unusable_below: Annotated[float, Field(ge=0)]
    luma_min: Annotated[float, Field(ge=0, le=255)]
    luma_max: Annotated[float, Field(ge=0, le=255)]
    luma_unusable_below: Annotated[float, Field(ge=0, le=255)]
    luma_unusable_above: Annotated[float, Field(ge=0, le=255)]
    clip_bright_pixel_above: Annotated[int, Field(strict=True, ge=0, le=255)]
    clip_dark_pixel_below: Annotated[int, Field(strict=True, ge=0, le=255)]
    clip_bright_max_pct: Annotated[float, Field(ge=0, le=100)]
    clip_dark_max_pct: Annotated[float, Field(ge=0, le=100)]
    min_source_long_edge: Annotated[int, Field(strict=True, ge=1)]
    min_source_short_edge: Annotated[int, Field(strict=True, ge=1)]
    min_duration_s: Annotated[float, Field(ge=0)]


class MediaConfig(HashedConfig):
    notes: Annotated[str, Field(max_length=1024)] = ""
    max_duration_s: Annotated[float, Field(gt=0)]
    duration_tolerance_s: Annotated[float, Field(ge=0)]
    max_source_long_edge: Annotated[int, Field(strict=True, ge=16)]
    max_source_short_edge: Annotated[int, Field(strict=True, ge=16)]
    max_source_bytes: Annotated[int, Field(strict=True, ge=1)]
    min_edge: Annotated[int, Field(strict=True, ge=1)]
    proxy_long_edge: Annotated[int, Field(strict=True, ge=16)]
    max_output_frames: Annotated[int, Field(strict=True, ge=1)]
    max_extract_frames: Annotated[int, Field(strict=True, ge=1)]
    allowed_containers: tuple[str, ...]
    demuxer_whitelist: tuple[str, ...]
    allowed_video_codecs: tuple[str, ...]
    x264_preset: Annotated[str, Field(pattern=r"^[a-z]{4,12}$")]
    x264_crf: Annotated[int, Field(strict=True, ge=0, le=51)]
    limits: ToolLimits
    frame_plans: tuple[FramePlan, ...]
    quality: QualityConfig
    # Vehicle masking only (never the damage stages). Null keeps the full proxy: every frame at
    # proxy resolution. Set, the masking request carries a derived video of keyframes (at most
    # this rate) and/or a downscaled long edge; masks are mapped back onto every proxy frame.
    vehicle_mask_sample_fps: Annotated[float, Field(gt=0, le=60)] | None = None
    vehicle_mask_long_edge: Annotated[int, Field(strict=True, ge=16)] | None = None

    @model_validator(mode="after")
    def complete_plans(self) -> Self:
        if sorted(plan.part_number for plan in self.frame_plans) != list(range(1, 9)):
            raise ValueError("frame_plans requires each part number 1..8 exactly once")
        if not self.allowed_containers or not self.allowed_video_codecs or not self.demuxer_whitelist:
            raise ValueError("container, codec and demuxer allowlists must be non-empty")
        video_rates = [plan.sample_fps for plan in self.frame_plans if plan.sample_fps is not None]
        if self.vehicle_mask_sample_fps is not None and self.vehicle_mask_sample_fps > min(video_rates):
            raise ValueError("vehicle_mask_sample_fps cannot exceed any video frame plan's sample_fps")
        if self.vehicle_mask_long_edge is not None and self.vehicle_mask_long_edge > self.proxy_long_edge:
            raise ValueError("vehicle_mask_long_edge cannot exceed proxy_long_edge")
        return self

    def plan_for(self, part_number: int) -> FramePlan:
        for plan in self.frame_plans:
            if plan.part_number == part_number:
                return plan
        raise KeyError(part_number)


def load_media_config(path: str | Path = DEFAULT_CONFIG_PATH) -> MediaConfig:
    return MediaConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- scratch


@contextmanager
def scratch_dir(root: str | Path) -> Iterator[Path]:
    """Private per-job scratch directory, removed on success and on failure."""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{SCRATCH_PREFIX}{uuid.uuid4().hex}"
    path.mkdir(mode=0o700)
    (path / SCRATCH_MARKER).touch()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def sweep_stale_scratch(root: str | Path, older_than: timedelta, *, now: float | None = None) -> tuple[Path, ...]:
    """Remove scratch directories left by a crashed process. Run at process start.

    Only real directories named with the scratch prefix that carry the marker file are
    removed; age is the marker's creation time. Choose `older_than` above the stage deadline.
    """
    base = Path(root)
    if not base.is_dir():
        return ()
    cutoff = (time.time() if now is None else now) - older_than.total_seconds()
    removed: list[Path] = []
    for child in sorted(base.iterdir()):
        marker = child / SCRATCH_MARKER
        if not child.name.startswith(SCRATCH_PREFIX) or child.is_symlink() or not child.is_dir():
            continue
        try:
            if marker.lstat().st_mtime >= cutoff:
                continue
        except FileNotFoundError:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child)
    return tuple(removed)


# --------------------------------------------------------------------------- subprocess


@dataclass(frozen=True, slots=True)
class ToolRun:
    returncode: int
    stdout: bytes
    stderr: str
    timed_out: bool


def ensure_plain_file(path: str | os.PathLike[str]) -> Path:
    """Accept only an existing regular local file: no URL, symlink, FIFO, device or directory."""
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise MediaError(MediaReason.INVALID_INPUT, "input is not a path") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw or "://" in raw:
        raise MediaError(MediaReason.INVALID_INPUT, "input is not a plain local file path")
    absolute = Path(os.path.abspath(raw))
    try:
        mode = os.lstat(absolute).st_mode
    except OSError as exc:
        raise MediaError(MediaReason.INVALID_INPUT, "input file is not readable") from exc
    if not stat.S_ISREG(mode):
        raise MediaError(MediaReason.INVALID_INPUT, "input is not a regular file")
    return absolute


def _minimal_env(cwd: Path) -> dict[str, str]:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(cwd), "TMPDIR": str(cwd)}


def _limit_applier(limits: ProcessLimits):
    def apply() -> None:
        assert resource is not None
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds + 2))
        resource.setrlimit(resource.RLIMIT_AS, (limits.address_space_bytes, limits.address_space_bytes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (limits.max_file_bytes, limits.max_file_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def run_tool(
    args: Sequence[str],
    *,
    limits: ProcessLimits,
    cwd: Path,
    stdout_cap_bytes: int = 16 * 1024 * 1024,
) -> ToolRun:
    """Run one subprocess without a shell under timeout and rlimits.

    stdout/stderr go to files in `cwd` (themselves bound by RLIMIT_FSIZE) so a noisy child
    cannot exhaust parent memory. The child leads its own session; a timeout kills the group.
    The rlimit hook only calls `setrlimit`, but note preexec_fn in a multithreaded parent
    (e.g. a heartbeat thread) is inherently best-effort; keep handlers' fork windows short.
    """
    if resource is None:
        raise MediaError(MediaReason.RESOURCE_LIMIT, "resource limits unavailable on this host")
    if not args or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
        raise ValueError("args must be a non-empty list of NUL-free strings")
    timed_out = False
    with tempfile.TemporaryFile(dir=cwd) as out, tempfile.TemporaryFile(dir=cwd) as err:
        process = subprocess.Popen(
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            cwd=cwd,
            env=_minimal_env(cwd),
            shell=False,
            close_fds=True,
            start_new_session=True,
            preexec_fn=_limit_applier(limits),
        )
        try:
            process.wait(timeout=limits.wall_time_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process)
        out.seek(0)
        stdout = out.read(stdout_cap_bytes + 1)
        err.seek(0)
        stderr = err.read(64 * 1024).decode("utf-8", errors="replace")
    if len(stdout) > stdout_cap_bytes:
        raise MediaError(MediaReason.RESOURCE_LIMIT, "tool output exceeded capture bound")
    return ToolRun(returncode=process.returncode, stdout=stdout, stderr=stderr, timed_out=timed_out)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _scrub(text: str, *hide: str) -> str:
    for item in hide:
        if item:
            text = text.replace(item, "<path>")
    return " ".join(text.split())[:200]


def classify_failure(run: ToolRun, default: MediaReason, *hide: str) -> MediaError:
    """Map a failed tool run onto a typed reason without leaking paths."""
    if run.timed_out:
        return MediaError(MediaReason.TIMEOUT, "tool exceeded wall-clock limit")
    lowered = run.stderr.lower()
    if "not on whitelist" in lowered:
        return MediaError(MediaReason.UNSUPPORTED_CONTAINER, "container or protocol is not allowed")
    # ffmpeg traps SIGXCPU like SIGTERM and exits 255 with no error line; no other path yields 255.
    ffmpeg_trapped_signal = run.returncode == 255 and not run.stderr.strip()
    if run.returncode in _SIGNAL_RESOURCE or ffmpeg_trapped_signal or any(marker in lowered for marker in _RESOURCE_STDERR):
        return MediaError(MediaReason.RESOURCE_LIMIT, "tool exceeded a process resource limit")
    last = run.stderr.strip().splitlines()[-1] if run.stderr.strip() else f"exit code {run.returncode}"
    return MediaError(default, _scrub(last, *hide))


def _file_url(path: Path) -> str:
    return f"file:{path}"


def _guarded_input_args(config: MediaConfig, source: Path) -> list[str]:
    """Input options shared by ffprobe and ffmpeg: local file protocol and known demuxers only."""
    return [
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        ",".join(config.demuxer_whitelist),
        "-i",
        _file_url(source),
    ]


# --------------------------------------------------------------------------- probe


@dataclass(frozen=True, slots=True)
class Rotation:
    clockwise_degrees: int
    source: Literal["none", "display_matrix", "legacy_tag"]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    container: str
    codec: str
    pix_fmt: str | None
    coded_width: int
    coded_height: int
    display_width: int
    display_height: int
    rotation: Rotation
    duration_s: float
    frame_rate: float | None
    audio_stream_count: int
    size_bytes: int


def parse_rotation(stream: dict[str, object]) -> Rotation:
    """Clockwise rotation needed for upright display.

    ffprobe's display-matrix `rotation` is counterclockwise degrees, so clockwise = -rotation
    (an iPhone portrait clip reports -90 and needs 90 clockwise). The legacy `rotate` tag is
    clockwise degrees. ffmpeg 5.1 autorotate honors only the display matrix, so the legacy tag
    is applied by an explicit filter in `normalize`.
    """
    for entry in stream.get("side_data_list") or []:  # type: ignore[union-attr]
        if isinstance(entry, dict) and "rotation" in entry:
            return Rotation(_quarter_turn(float(entry["rotation"]) * -1), "display_matrix")
    tags = stream.get("tags")
    if isinstance(tags, dict) and "rotate" in tags:
        try:
            return Rotation(_quarter_turn(float(tags["rotate"])), "legacy_tag")
        except (TypeError, ValueError) as exc:
            raise MediaError(MediaReason.UNSUPPORTED_ROTATION, "unreadable rotate tag") from exc
    return Rotation(0, "none")


def _quarter_turn(degrees: float) -> int:
    if not math.isfinite(degrees):
        raise MediaError(MediaReason.UNSUPPORTED_ROTATION, "non-finite rotation")
    turns = round(degrees / 90)
    if abs(degrees - turns * 90) > 1.0:
        raise MediaError(MediaReason.UNSUPPORTED_ROTATION, "rotation is not a quarter turn")
    return (turns * 90) % 360


def _ratio(value: object) -> Fraction | None:
    if not isinstance(value, str) or "/" not in value and ":" not in value:
        return None
    num, _, den = value.replace(":", "/").partition("/")
    try:
        fraction = Fraction(int(num), int(den))
    except (ValueError, ZeroDivisionError):
        return None
    return fraction if fraction > 0 else None


def _positive_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _display_size(width: int, height: int, sar: Fraction | None, rotation: Rotation) -> tuple[int, int]:
    square_width = round(width * sar) if sar is not None and sar != 1 else width
    if rotation.clockwise_degrees in (90, 270):
        return height, square_width
    return square_width, height


def probe(path: str | os.PathLike[str], config: MediaConfig, scratch: Path | None = None) -> ProbeResult:
    """Inspect a candidate file and reject before any decode of its video data.

    Raises `MediaError` with reason `invalid_input`, `corrupt`, `no_video`,
    `multiple_video_streams`, `unsupported_container/codec/rotation`, `duration_exceeded`,
    `dimensions_exceeded`, `resource_limit` or `timeout`.
    """
    source = ensure_plain_file(path)
    if scratch is None:
        with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as temporary:
            return probe(source, config, Path(temporary))
    size = source.stat().st_size
    if size <= 0:
        raise MediaError(MediaReason.CORRUPT, "empty file")
    if size > config.max_source_bytes:
        raise MediaError(MediaReason.RESOURCE_LIMIT, "source file exceeds size limit")
    run = run_tool(
        [
            *_tool("ffprobe"),
            "-v",
            "error",
            "-probesize",
            "10000000",
            "-analyzeduration",
            "10000000",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            *_guarded_input_args(config, source),
        ],
        limits=config.limits.probe,
        cwd=scratch,
    )
    if run.returncode != 0 or run.timed_out:
        raise classify_failure(run, MediaReason.CORRUPT, str(source))
    try:
        data = json.loads(run.stdout)
    except ValueError as exc:
        raise MediaError(MediaReason.CORRUPT, "unreadable probe output") from exc
    return _validate_probe(data, source, size, config, scratch)


def _tool(name: str) -> list[str]:
    resolved = shutil.which(name, path="/usr/local/bin:/usr/bin:/bin")
    if resolved is None:
        raise MediaError(MediaReason.RESOURCE_LIMIT, f"{name} is not installed")
    return [resolved]


def _validate_probe(data: object, source: Path, size: int, config: MediaConfig, scratch: Path) -> ProbeResult:
    if not isinstance(data, dict) or not isinstance(data.get("format"), dict):
        raise MediaError(MediaReason.CORRUPT, "probe returned no format")
    container_names = str(data["format"].get("format_name", "")).split(",")
    container = next((name for name in container_names if name in config.allowed_containers), None)
    if container is None:
        raise MediaError(MediaReason.UNSUPPORTED_CONTAINER, "container is not allowed")
    streams = [item for item in data.get("streams", []) if isinstance(item, dict)]
    video = [
        item
        for item in streams
        if item.get("codec_type") == "video" and not (item.get("disposition") or {}).get("attached_pic")
    ]
    if not video:
        raise MediaError(MediaReason.NO_VIDEO, "no usable video stream")
    if len(video) > 1:
        raise MediaError(MediaReason.MULTIPLE_VIDEO_STREAMS, "more than one video stream")
    stream = video[0]
    codec = str(stream.get("codec_name", ""))
    if codec not in config.allowed_video_codecs:
        raise MediaError(MediaReason.UNSUPPORTED_CODEC, "video codec is not allowed")
    width, height = stream.get("width"), stream.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or min(width, height) < config.min_edge:
        raise MediaError(MediaReason.CORRUPT, "invalid frame dimensions")
    rotation = parse_rotation(stream)
    display_width, display_height = _display_size(width, height, _ratio(stream.get("sample_aspect_ratio")), rotation)
    if max(display_width, display_height) > config.max_source_long_edge or min(display_width, display_height) > config.max_source_short_edge:
        raise MediaError(MediaReason.DIMENSIONS_EXCEEDED, "source dimensions exceed limit")
    duration = _positive_float(data["format"].get("duration")) or _positive_float(stream.get("duration"))
    if duration is None:
        duration = _scan_duration(source, config, scratch)
    if duration > config.max_duration_s + config.duration_tolerance_s:
        raise MediaError(MediaReason.DURATION_EXCEEDED, "source duration exceeds limit")
    frame_rate = _ratio(stream.get("avg_frame_rate")) or _ratio(stream.get("r_frame_rate"))
    return ProbeResult(
        container=container,
        codec=codec,
        pix_fmt=stream.get("pix_fmt") if isinstance(stream.get("pix_fmt"), str) else None,
        coded_width=width,
        coded_height=height,
        display_width=display_width,
        display_height=display_height,
        rotation=rotation,
        duration_s=duration,
        frame_rate=float(frame_rate) if frame_rate is not None else None,
        audio_stream_count=sum(1 for item in streams if item.get("codec_type") == "audio"),
        size_bytes=size,
    )


def _scan_duration(source: Path, config: MediaConfig, scratch: Path) -> float:
    """Duration from demuxed packets for files without a duration header.

    Browser MediaRecorder WebM commonly has no duration, so this path is expected, not exotic.
    """
    run = run_tool(
        [
            *_tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time,duration_time",
            "-of",
            "csv=p=0",
            *_guarded_input_args(config, source),
        ],
        limits=config.limits.probe,
        cwd=scratch,
    )
    if run.returncode != 0 or run.timed_out:
        raise classify_failure(run, MediaReason.CORRUPT, str(source))
    starts: list[float] = []
    ends: list[float] = []
    for line in run.stdout.decode("utf-8", errors="replace").splitlines():
        first, _, second = line.partition(",")
        start = _positive_or_zero(first)
        if start is None:
            continue
        starts.append(start)
        ends.append(start + (_positive_or_zero(second) or 0.0))
    if not starts or max(ends) - min(starts) <= 0:
        raise MediaError(MediaReason.CORRUPT, "duration could not be determined")
    return max(ends) - min(starts)


def _positive_or_zero(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


# --------------------------------------------------------------------------- manifest


@dataclass(frozen=True, slots=True)
class FrameManifest:
    """Exact frame index -> presentation timestamp mapping for one proxy file."""

    proxy_sha256: str
    width: int
    height: int
    time_base_num: int
    time_base_den: int
    pts: tuple[int, ...]
    version: str = MANIFEST_VERSION

    def __post_init__(self) -> None:
        if not self.pts or self.time_base_num <= 0 or self.time_base_den <= 0:
            raise ValueError("manifest requires frames and a positive time base")
        if any(later <= earlier for earlier, later in zip(self.pts, self.pts[1:])):
            raise ValueError("presentation timestamps must be strictly increasing")

    @property
    def frame_count(self) -> int:
        return len(self.pts)

    def time_fraction(self, index: int) -> Fraction:
        return Fraction(self.pts[index] * self.time_base_num, self.time_base_den)

    def time_seconds(self, index: int) -> float:
        return float(self.time_fraction(index))

    @property
    def duration_seconds(self) -> float:
        """Presentation span plus one typical frame interval (median delta)."""
        if len(self.pts) < 2:
            return 0.0
        deltas = sorted(later - earlier for earlier, later in zip(self.pts, self.pts[1:]))
        span = self.pts[-1] - self.pts[0] + deltas[len(deltas) // 2]
        return float(Fraction(span * self.time_base_num, self.time_base_den))

    def nearest_index(self, seconds_from_start: float) -> int:
        """Frame whose timestamp is nearest the offset from the first frame (earlier on ties)."""
        target = self.pts[0] + seconds_from_start * self.time_base_den / self.time_base_num
        position = bisect_left(self.pts, target)
        if position == 0:
            return 0
        if position == len(self.pts):
            return len(self.pts) - 1
        before, after = self.pts[position - 1], self.pts[position]
        return position - 1 if target - before <= after - target else position

    def to_canonical_json(self) -> str:
        payload = {
            "version": self.version,
            "proxy_sha256": self.proxy_sha256,
            "width": self.width,
            "height": self.height,
            "time_base": [self.time_base_num, self.time_base_den],
            "pts": list(self.pts),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.to_canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, text: str) -> Self:
        payload = json.loads(text)
        if payload.get("version") != MANIFEST_VERSION:
            raise ValueError("unsupported manifest version")
        num, den = payload["time_base"]
        return cls(
            proxy_sha256=payload["proxy_sha256"],
            width=payload["width"],
            height=payload["height"],
            time_base_num=num,
            time_base_den=den,
            pts=tuple(payload["pts"]),
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    proxy: Path, config: MediaConfig, scratch: Path, *, proxy_sha256: str | None = None, width: int | None = None, height: int | None = None
) -> FrameManifest:
    """Per-frame presentation timestamps from ffprobe frame data (not assumed constant fps)."""
    source = ensure_plain_file(proxy)
    run = run_tool(
        [
            *_tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts,best_effort_timestamp:stream=time_base,width,height",
            "-of",
            "json",
            *_guarded_input_args(config, source),
        ],
        limits=config.limits.extract,
        cwd=scratch,
    )
    if run.returncode != 0 or run.timed_out:
        raise classify_failure(run, MediaReason.CORRUPT, str(source))
    try:
        data = json.loads(run.stdout)
        stream = data["streams"][0]
        base = _ratio(stream["time_base"])
        values = [_frame_pts(frame) for frame in data["frames"]]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise MediaError(MediaReason.CORRUPT, "unreadable frame data") from exc
    if base is None or not values:
        raise MediaError(MediaReason.CORRUPT, "no decodable frames")
    if len(values) > config.max_output_frames:
        raise MediaError(MediaReason.RESOURCE_LIMIT, "frame count exceeds limit")
    try:
        return FrameManifest(
            proxy_sha256=proxy_sha256 or sha256_file(source),
            width=width if width is not None else int(stream["width"]),
            height=height if height is not None else int(stream["height"]),
            time_base_num=base.numerator,
            time_base_den=base.denominator,
            pts=tuple(values),
        )
    except ValueError as exc:
        raise MediaError(MediaReason.CORRUPT, "frame timestamps are not strictly increasing") from exc


def _frame_pts(frame: dict[str, object]) -> int:
    value = frame.get("pts", frame.get("best_effort_timestamp"))
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("frame without integer timestamp")
    return value


# --------------------------------------------------------------------------- normalize


@dataclass(frozen=True, slots=True)
class NormalizeResult:
    path: Path
    width: int
    height: int
    frame_count: int
    duration_s: float
    source_sha256: str
    output_sha256: str
    source: ProbeResult
    manifest: FrameManifest


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def target_size(display_width: int, display_height: int, proxy_long_edge: int) -> tuple[int, int]:
    """Even-sized proxy dimensions, long edge <= limit, never upscaled."""
    scale = min(1.0, proxy_long_edge / max(display_width, display_height))
    return _even(display_width * scale), _even(display_height * scale)


def normalize(
    source: str | os.PathLike[str],
    scratch: Path,
    config: MediaConfig,
    *,
    target_fps: float | None = None,
    probe_result: ProbeResult | None = None,
    output_name: str = "proxy.mp4",
) -> NormalizeResult:
    """Transcode to the canonical proxy: MP4/H.264/yuv420p, no audio, upright, long edge <= limit.

    Rotation: a display matrix is applied by ffmpeg autorotate (which also drops the matrix);
    a legacy `rotate` tag, which ffmpeg ignores, is applied by an explicit filter. The output is
    probed afterwards and must be upright with no residual rotation metadata.
    `target_fps` resamples the proxy (the proxy frame count is the provider bill); omit to keep
    source timing. The partial output is removed on any failure.
    """
    src = ensure_plain_file(source)
    info = probe_result or probe(src, config, scratch)
    out = scratch / output_name
    width, height = target_size(info.display_width, info.display_height, config.proxy_long_edge)
    source_sha = sha256_file(src)
    args = _normalize_args(config, src, out, info, width, height, target_fps)
    try:
        run = run_tool(args, limits=config.limits.transcode, cwd=scratch)
        if run.returncode != 0 or run.timed_out:
            raise classify_failure(run, MediaReason.TRANSCODE_FAILED, str(src), str(out))
        return _finish_normalize(out, scratch, config, info, width, height, source_sha)
    except BaseException:
        out.unlink(missing_ok=True)
        raise


def _normalize_args(
    config: MediaConfig, src: Path, out: Path, info: ProbeResult, width: int, height: int, target_fps: float | None
) -> list[str]:
    filters: list[str] = []
    if info.rotation.source == "legacy_tag" and info.rotation.clockwise_degrees:
        filters.append(_ROTATION_FILTERS[info.rotation.clockwise_degrees])
    filters.append(f"scale={width}:{height}:flags=bicubic:out_color_matrix=bt709")
    filters.append("setsar=1")
    if target_fps is not None:
        filters.append(f"fps={target_fps:g}")
    filters.append("format=yuv420p")
    threads = str(config.limits.transcode.threads)
    cap = config.max_duration_s + 1
    return [
        *_tool("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        threads,
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        ",".join(config.demuxer_whitelist),
        "-i",
        _file_url(src),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-vf",
        ",".join(filters),
        "-fps_mode",
        "passthrough",
        "-c:v",
        "libx264",
        "-preset",
        config.x264_preset,
        "-crf",
        str(config.x264_crf),
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-t",
        f"{cap:g}",
        "-frames:v",
        str(config.max_output_frames + 1),
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        _file_url(out),
    ]


def _finish_normalize(
    out: Path, scratch: Path, config: MediaConfig, info: ProbeResult, width: int, height: int, source_sha: str
) -> NormalizeResult:
    if not out.is_file() or out.stat().st_size == 0:
        raise MediaError(MediaReason.OUTPUT_INVALID, "no output produced")
    produced = probe(out, config, scratch)
    if produced.rotation.clockwise_degrees != 0 or produced.rotation.source != "none":
        raise MediaError(MediaReason.OUTPUT_INVALID, "output retains rotation metadata")
    if produced.codec != "h264" or produced.pix_fmt != "yuv420p" or produced.audio_stream_count:
        raise MediaError(MediaReason.OUTPUT_INVALID, "output is not video-only H.264 yuv420p")
    if (produced.coded_width, produced.coded_height) != (width, height):
        raise MediaError(MediaReason.OUTPUT_INVALID, "output dimensions differ from the expected upright size")
    output_sha = sha256_file(out)
    manifest = build_manifest(out, config, scratch, proxy_sha256=output_sha, width=width, height=height)
    if manifest.duration_seconds > config.max_duration_s + config.duration_tolerance_s:
        raise MediaError(MediaReason.DURATION_EXCEEDED, "decoded duration exceeds limit")
    return NormalizeResult(
        path=out,
        width=width,
        height=height,
        frame_count=manifest.frame_count,
        duration_s=manifest.duration_seconds,
        source_sha256=source_sha,
        output_sha256=output_sha,
        source=info,
        manifest=manifest,
    )


# --------------------------------------------------------------------------- frame selection and extraction


def plan_frame_indices(manifest: FrameManifest, plan: FramePlan) -> tuple[int, ...]:
    """Frame indices for a part's plan, ascending and unique.

    Video plans sample at `sample_fps`; stills plans take `count` frames at the centres of
    equal time windows. Fewer frames than requested simply yields fewer indices.
    """
    duration = manifest.duration_seconds
    if plan.mode == "video":
        assert plan.sample_fps is not None
        targets = [step / plan.sample_fps for step in range(max(1, round(duration * plan.sample_fps)))]
    else:
        assert plan.count is not None
        targets = [(slot + 0.5) * duration / plan.count for slot in range(plan.count)]
    return tuple(sorted({manifest.nearest_index(target) for target in targets}))


@dataclass(frozen=True, slots=True)
class ExtractedFrame:
    frame_index: int
    pts: int
    time_s: float
    path: Path
    width: int
    height: int
    sha256: str


def extract_frames(
    proxy: Path,
    manifest: FrameManifest,
    indices: Sequence[int],
    scratch: Path,
    config: MediaConfig,
    *,
    out_dir_name: str = "frames",
    image_format: Literal["jpeg", "png"] = "jpeg",
    scale_to: tuple[int, int] | None = None,
) -> tuple[ExtractedFrame, ...]:
    """Write exactly the requested frame indices (decode order == manifest order) as images.

    Selection uses ffmpeg `select=eq(n,K)` with timestamps passed through, so file k is frame
    `sorted(indices)[k]`. The file count is verified. `scale_to` downsizes for measurement.
    """
    ordered = sorted(set(indices))
    if not ordered or ordered[0] < 0 or ordered[-1] >= manifest.frame_count:
        raise ValueError("indices must be non-empty and inside the manifest")
    if len(ordered) > config.max_extract_frames:
        raise MediaError(MediaReason.RESOURCE_LIMIT, "too many frames requested")
    source = ensure_plain_file(proxy)
    out_dir = scratch / out_dir_name
    out_dir.mkdir(exist_ok=True)
    extension = "jpg" if image_format == "jpeg" else "png"
    filters = ["select='" + "+".join(f"eq(n,{index})" for index in ordered) + "'"]
    if scale_to is not None:
        filters.append(f"scale={scale_to[0]}:{scale_to[1]}:flags=area")
    filters.append("format=yuvj420p" if image_format == "jpeg" else "format=rgb24")
    args = [
        *_tool("ffmpeg"),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        str(config.limits.extract.threads),
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        ",".join(config.demuxer_whitelist),
        "-i",
        _file_url(source),
        "-map",
        "0:v:0",
        "-vf",
        ",".join(filters),
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(len(ordered)),
        *(["-q:v", "2"] if image_format == "jpeg" else []),
        "-start_number",
        "0",
        "-y",
        _file_url(out_dir / f"frame_%06d.{extension}"),
    ]
    run = run_tool(args, limits=config.limits.extract, cwd=scratch)
    if run.returncode != 0 or run.timed_out:
        raise classify_failure(run, MediaReason.EXTRACT_FAILED, str(source), str(out_dir))
    return _collect_frames(out_dir, extension, ordered, manifest)


def _collect_frames(out_dir: Path, extension: str, ordered: list[int], manifest: FrameManifest) -> tuple[ExtractedFrame, ...]:
    produced = sorted(out_dir.glob(f"frame_*.{extension}"))
    if len(produced) != len(ordered):
        raise MediaError(MediaReason.EXTRACT_FAILED, "extracted frame count differs from request")
    frames: list[ExtractedFrame] = []
    for index, path in zip(ordered, produced):
        with Image.open(path) as image:
            width, height = image.size
        frames.append(
            ExtractedFrame(
                frame_index=index,
                pts=manifest.pts[index],
                time_s=manifest.time_seconds(index),
                path=path,
                width=width,
                height=height,
                sha256=sha256_file(path),
            )
        )
    return tuple(frames)
