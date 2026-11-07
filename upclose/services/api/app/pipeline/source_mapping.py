"""Typed area-mapping boundary. No live recognition or capture-order inference exists.

User ranges are immutable hints, never verified coverage. The fixture mapper is explicitly
synthetic and is not installed by the worker. Source media uses frame selection, not inferred
equal-time splitting, to retain exact original frame identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field

from app.contracts.video import VideoRange
from app.domain.config import HashedConfig
from app.pipeline.media import MediaConfig, load_media_config
from app.pipeline.orchestration import canonical_hash

SOURCE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "source-video.v1.json"
MAPPING_UNAVAILABLE = "VIDEO_MAPPING_UNAVAILABLE"
MAPPING_UNVERIFIED = "VIDEO_MAPPING_UNVERIFIED"


class SourceConfig(HashedConfig):
    max_duration_ms: Annotated[int, Field(strict=True, ge=1, le=180000)]
    max_source_bytes: Annotated[int, Field(strict=True, ge=1, le=209715200)]
    max_range_ms: Annotated[int, Field(strict=True, ge=1, le=60000)]
    max_ranges: Annotated[int, Field(strict=True, ge=1, le=8)]
    max_source_frames: Annotated[int, Field(strict=True, ge=1, le=10800)]
    mapper: Literal["unavailable-v1"]


def load_source_config() -> SourceConfig:
    return SourceConfig.model_validate_json(SOURCE_CONFIG_PATH.read_text(encoding="utf-8"))


def source_config_hash() -> str:
    return canonical_hash({"source": load_source_config().content_hash,
                           "media": load_media_config().content_hash})


def source_media_config(config: SourceConfig, media: MediaConfig) -> MediaConfig:
    return media.model_copy(update={"max_duration_s": config.max_duration_ms / 1000,
                                    "max_source_bytes": config.max_source_bytes,
                                    "max_output_frames": config.max_source_frames})


def validate_ranges(ranges: tuple[VideoRange, ...], duration_ms: int, config: SourceConfig) -> None:
    if len(ranges) > config.max_ranges or len({r.part_no for r in ranges}) != len(ranges):
        raise ValueError("duplicate or excessive source ranges")
    ordered = sorted(ranges, key=lambda value: value.start_ms)
    for position, item in enumerate(ordered):
        if item.end_ms > duration_ms or item.end_ms - item.start_ms > config.max_range_ms:
            raise ValueError("source range exceeds duration")
        if position and ordered[position - 1].end_ms > item.start_ms:
            raise ValueError("overlapping source ranges")


@dataclass(frozen=True, slots=True)
class MappingInput:
    source_sha256: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class MappingResult:
    state: Literal["unavailable", "synthetic"]
    ranges: tuple[VideoRange, ...] = ()
    reason: str = MAPPING_UNAVAILABLE

    def __post_init__(self) -> None:
        if self.state == "unavailable" and self.ranges:
            raise ValueError("unavailable mapping cannot assign areas")


class AreaMapper(Protocol):
    def map(self, source: MappingInput) -> MappingResult: ...


class UnavailableAreaMapper:
    def map(self, source: MappingInput) -> MappingResult:
        return MappingResult("unavailable")


class SyntheticAreaMapper:
    """Only explicit test fixtures; no claims about the pixels in uploaded footage."""
    def __init__(self, ranges: tuple[VideoRange, ...]) -> None:
        self.ranges = ranges

    def map(self, source: MappingInput) -> MappingResult:
        validate_ranges(self.ranges, source.duration_ms, load_source_config())
        return MappingResult("synthetic", self.ranges, "SYNTHETIC_MAPPING_FIXTURE")
