"""Bounded single-source capture; declared ranges are hints, not visual verification."""
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from .api import UploadedPiece
from .base import ContractModel, PartNumber
from .enums import UploadState


class VideoRange(ContractModel):
    part_no: PartNumber
    start_ms: Annotated[int, Field(strict=True, ge=0, lt=180000)]
    end_ms: Annotated[int, Field(strict=True, gt=0, le=180000)]

    @model_validator(mode="after")
    def bounded_range(self) -> Self:
        if not 0 < self.end_ms - self.start_ms <= 60000:
            raise ValueError("a marked section must be positive and at most 60 seconds")
        return self


class CreateVideoUploadRequest(ContractModel):
    content_type: Literal["video/mp4", "video/webm", "video/quicktime"]
    content_length: Annotated[int, Field(strict=True, gt=0, le=200 * 1024 * 1024)]
    segments: Annotated[tuple[VideoRange, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def distinct_ranges(self) -> Self:
        if len({item.part_no for item in self.segments}) != len(self.segments):
            raise ValueError("only one marked section per area is supported")
        ordered = sorted(self.segments, key=lambda item: item.start_ms)
        if any(first.end_ms > second.start_ms for first, second in zip(ordered, ordered[1:])):
            raise ValueError("marked sections must not overlap")
        return self


class CreateVideoUploadResponse(ContractModel):
    upload_id: UUID
    chunk_size_bytes: Literal[8388608] = 8388608
    chunk_count: Annotated[int, Field(strict=True, ge=1, le=25)]


class VideoUploadResponse(ContractModel):
    upload_id: UUID
    state: UploadState
    processing_state: Literal["pending", "queued", "ready", "failed"]
    reason: Annotated[str, Field(max_length=240)] | None = None
    expected_bytes: Annotated[int, Field(strict=True, gt=0, le=200 * 1024 * 1024)]
    content_type: Literal["video/mp4", "video/webm", "video/quicktime"]
    duration_ms: Annotated[int, Field(strict=True, gt=0, le=180250)] | None = None
    segments: Annotated[tuple[VideoRange, ...], Field(max_length=8)] = ()
    pieces: tuple[UploadedPiece, ...] = ()


class VideoSourceResponse(ContractModel):
    source: VideoUploadResponse | None = None


class VideoAcceptedResponse(ContractModel):
    upload_id: UUID
    state: UploadState
    processing_state: Literal["pending", "queued", "ready", "failed"]
