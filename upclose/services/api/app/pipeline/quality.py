"""Server-side capture-quality measurement on the proxy (PRD 7.8.2, provisional thresholds).

Pure Pillow arithmetic over frames the media layer already extracted; no network, database or
provider imports. Measurement is warn-and-keep: a valid but poor clip yields warnings and
`usable_for_scoring=False` (later mapped to partial coverage), never an exception and never a
clean pass. A failed measurement is also not usable.
"""

import json
import os
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

from PIL import Image, ImageMath

from app.pipeline.media import (
    FrameManifest,
    FramePlan,
    MediaConfig,
    MediaError,
    NormalizeResult,
    QualityConfig,
    extract_frames,
    normalize,
    plan_frame_indices,
    probe,
)

QUALITY_VERSION: Final[str] = "quality-v1"
_BOX: Final = Image.Resampling.BOX


@dataclass(frozen=True, slots=True)
class FrameQuality:
    frame_index: int
    time_s: float
    blur_variance: float
    mean_luma: float
    bright_pct: float
    dark_pct: float


@dataclass(frozen=True, slots=True)
class QualityWarning:
    code: str
    severity: Literal["warn", "strong"]
    blocks_scoring: bool
    message: str
    value: float | None = None
    threshold: float | None = None


@dataclass(frozen=True, slots=True)
class QualityReport:
    version: str
    config_hash: str
    thresholds_provisional: bool
    working_width: int
    working_height: int
    duration_s: float
    frames: tuple[FrameQuality, ...]
    median_blur_variance: float | None
    median_mean_luma: float | None
    median_bright_pct: float | None
    median_dark_pct: float | None
    warnings: tuple[QualityWarning, ...]
    usable_for_scoring: bool

    def to_json_dict(self) -> dict[str, object]:
        """Plain JSON-safe structure suitable for the parts.quality column."""
        return json.loads(json.dumps(asdict(self), sort_keys=True))


# --------------------------------------------------------------------------- pixel metrics


def laplacian_variance(gray: Image.Image) -> float:
    """Variance of the 4-neighbour Laplacian over interior pixels of an 8-bit luma image.

    Computed with float32 image arithmetic (no numpy): variance = E[x^2] - E[x]^2, with the
    means taken by exact box reduction. Interior-only differs slightly from reflect-border
    implementations; thresholds are provisional either way.
    """
    width, height = gray.size
    if width < 3 or height < 3:
        return 0.0
    base = gray.convert("L").convert("F")
    laplacian = ImageMath.lambda_eval(
        lambda a: a["up"] + a["down"] + a["left"] + a["right"] - 4 * a["mid"],
        up=base.crop((1, 0, width - 1, height - 2)),
        down=base.crop((1, 2, width - 1, height)),
        left=base.crop((0, 1, width - 2, height - 1)),
        right=base.crop((2, 1, width, height - 1)),
        mid=base.crop((1, 1, width - 1, height - 1)),
    )
    squared = ImageMath.lambda_eval(lambda a: a["x"] * a["x"], x=laplacian)
    mean = float(laplacian.resize((1, 1), _BOX).getpixel((0, 0)))
    mean_square = float(squared.resize((1, 1), _BOX).getpixel((0, 0)))
    return max(0.0, mean_square - mean * mean)


def luma_metrics(gray: Image.Image, bright_above: int, dark_below: int) -> tuple[float, float, float]:
    """Mean luma (0-255), % pixels above `bright_above`, % pixels below `dark_below`."""
    histogram = gray.convert("L").histogram()
    total = sum(histogram)
    if total == 0:
        return 0.0, 0.0, 0.0
    mean = sum(level * count for level, count in enumerate(histogram)) / total
    bright = sum(histogram[bright_above + 1 :]) / total * 100
    dark = sum(histogram[:dark_below]) / total * 100
    return mean, bright, dark


def working_size(width: int, height: int, config: QualityConfig) -> tuple[int, int]:
    """Fit inside the 720p working box for the clip's orientation; never upscale."""
    box_w, box_h = (
        (config.working_long_edge, config.working_short_edge)
        if width >= height
        else (config.working_short_edge, config.working_long_edge)
    )
    scale = min(1.0, box_w / width, box_h / height)
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


# --------------------------------------------------------------------------- evaluation


def evaluate_quality(
    frames: tuple[FrameQuality, ...],
    duration_s: float,
    source_display_size: tuple[int, int] | None,
    config: QualityConfig,
) -> tuple[QualityWarning, ...]:
    """Pure threshold evaluation over measured frames. Empty result means every gate passed."""
    warnings: list[QualityWarning] = []
    if not frames:
        return (QualityWarning("measurement_failed", "strong", True, "No frames could be measured."),)
    blur = statistics.median(frame.blur_variance for frame in frames)
    luma = statistics.median(frame.mean_luma for frame in frames)
    bright = statistics.median(frame.bright_pct for frame in frames)
    dark = statistics.median(frame.dark_pct for frame in frames)
    if blur < config.blur_warn_below:
        strong = blur < config.blur_strong_below
        warnings.append(
            QualityWarning(
                "blur_strong" if strong else "blur_low",
                "strong" if strong else "warn",
                blur < config.blur_unusable_below,
                "Footage is blurry; hold steadier or wipe the lens.",
                blur,
                config.blur_strong_below if strong else config.blur_warn_below,
            )
        )
    if luma < config.luma_min:
        warnings.append(
            QualityWarning("exposure_dark", "warn", luma < config.luma_unusable_below, "Too dark; move the car or add light.", luma, config.luma_min)
        )
    elif luma > config.luma_max:
        warnings.append(
            QualityWarning("exposure_bright", "warn", luma > config.luma_unusable_above, "Too bright; shade the panel.", luma, config.luma_max)
        )
    if bright >= config.clip_bright_max_pct:
        warnings.append(QualityWarning("clipping_bright", "warn", False, "Glare may read as damage.", bright, config.clip_bright_max_pct))
    if dark >= config.clip_dark_max_pct:
        warnings.append(QualityWarning("clipping_dark", "warn", False, "Heavy shadow may hide damage.", dark, config.clip_dark_max_pct))
    if source_display_size is not None:
        long_edge, short_edge = max(source_display_size), min(source_display_size)
        if long_edge < config.min_source_long_edge or short_edge < config.min_source_short_edge:
            warnings.append(QualityWarning("resolution_low", "warn", False, "Move closer; small damage may be missed.", float(short_edge), float(config.min_source_short_edge)))
    if duration_s < config.min_duration_s:
        warnings.append(QualityWarning("duration_short", "warn", False, "Clip is short; coverage may be incomplete.", duration_s, config.min_duration_s))
    return tuple(warnings)


def _sample_indices(manifest: FrameManifest, count: int) -> tuple[int, ...]:
    duration = manifest.duration_seconds
    return tuple(sorted({manifest.nearest_index((slot + 0.5) * duration / count) for slot in range(count)}))


def _failed_report(manifest: FrameManifest | None, config: MediaConfig, size: tuple[int, int], reason: str) -> QualityReport:
    return QualityReport(
        version=QUALITY_VERSION,
        config_hash=config.content_hash,
        thresholds_provisional=config.quality.provisional,
        working_width=size[0],
        working_height=size[1],
        duration_s=manifest.duration_seconds if manifest else 0.0,
        frames=(),
        median_blur_variance=None,
        median_mean_luma=None,
        median_bright_pct=None,
        median_dark_pct=None,
        warnings=(QualityWarning("measurement_failed", "strong", True, f"Quality could not be measured ({reason})."),),
        usable_for_scoring=False,
    )


def measure_quality(
    proxy: Path,
    manifest: FrameManifest,
    scratch: Path,
    config: MediaConfig,
    *,
    source_display_size: tuple[int, int] | None = None,
) -> QualityReport:
    """Measure sampled proxy frames at 720p working resolution. Never raises for bad footage."""
    quality = config.quality
    size = working_size(manifest.width, manifest.height, quality)
    try:
        indices = _sample_indices(manifest, quality.sample_count)
        extracted = extract_frames(
            proxy, manifest, indices, scratch, config, out_dir_name="quality-frames", image_format="png", scale_to=size
        )
        measured: list[FrameQuality] = []
        for frame in extracted:
            with Image.open(frame.path) as image:
                gray = image.convert("L")
                gray.load()
            mean, bright, dark = luma_metrics(gray, quality.clip_bright_pixel_above, quality.clip_dark_pixel_below)
            measured.append(
                FrameQuality(frame.frame_index, frame.time_s, laplacian_variance(gray), mean, bright, dark)
            )
    except (MediaError, OSError, ValueError) as exc:
        reason = exc.reason.value if isinstance(exc, MediaError) else type(exc).__name__
        return _failed_report(manifest, config, size, reason)
    frames = tuple(measured)
    warnings = evaluate_quality(frames, manifest.duration_seconds, source_display_size, quality)
    return QualityReport(
        version=QUALITY_VERSION,
        config_hash=config.content_hash,
        thresholds_provisional=quality.provisional,
        working_width=size[0],
        working_height=size[1],
        duration_s=manifest.duration_seconds,
        frames=frames,
        median_blur_variance=statistics.median(frame.blur_variance for frame in frames),
        median_mean_luma=statistics.median(frame.mean_luma for frame in frames),
        median_bright_pct=statistics.median(frame.bright_pct for frame in frames),
        median_dark_pct=statistics.median(frame.dark_pct for frame in frames),
        warnings=warnings,
        usable_for_scoring=not any(warning.blocks_scoring for warning in warnings),
    )


# --------------------------------------------------------------------------- one-call analysis


@dataclass(frozen=True, slots=True)
class ClipAnalysis:
    normalized: NormalizeResult
    quality: QualityReport
    plan: FramePlan
    planned_frame_indices: tuple[int, ...]


def normalize_and_assess(
    source: str | os.PathLike[str], scratch: Path, config: MediaConfig, part_number: int
) -> ClipAnalysis:
    """probe -> normalize (resampled to the part's plan) -> manifest -> quality -> planned frames.

    Raises `MediaError` for hard rejections (corrupt, limits, timeout...). Poor-but-valid footage
    returns normally with `quality.usable_for_scoring == False`.
    """
    plan = config.plan_for(part_number)
    info = probe(source, config, scratch)
    result = normalize(source, scratch, config, target_fps=plan.proxy_fps(info.frame_rate), probe_result=info)
    quality = measure_quality(
        result.path, result.manifest, scratch, config, source_display_size=(info.display_width, info.display_height)
    )
    return ClipAnalysis(result, quality, plan, plan_frame_indices(result.manifest, plan))
