"""Normalized provider boundary; fixture data never implies live accuracy."""
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.base import Confidence, ContractModel, Count, ObjectKey
from app.contracts import BoundingBox, ClaimResult, Finding, NarratedStatement, VerificationVerdict


class FrameRef(ContractModel):
    frame_index: Count
    timestamp_ms: Count
    frame_key: ObjectKey


class MediaRef(ContractModel):
    media_key: ObjectKey
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    kind: Literal["image", "video"]
    stored_width: Annotated[int, Field(strict=True, gt=0, le=3840)]
    stored_height: Annotated[int, Field(strict=True, gt=0, le=3840)]
    rotation_degrees: Literal[0, 90, 180, 270] = 0
    frames: Annotated[tuple[FrameRef, ...], Field(min_length=1, max_length=15000)]

    @property
    def display_size(self) -> tuple[int, int]:
        if self.rotation_degrees in (90, 270):
            return self.stored_height, self.stored_width
        return self.stored_width, self.stored_height

    @model_validator(mode="after")
    def unique_manifest(self) -> Self:
        indices = [frame.frame_index for frame in self.frames]
        if len(indices) != len(set(indices)):
            raise ValueError("duplicate source frame index")
        if self.kind == "image" and indices != [0]:
            raise ValueError("image manifest requires only source frame zero")
        return self


class ProviderUsage(ContractModel):
    """Nullable values are unknown, never silently zero live spend."""
    images_processed: Count | None = None
    video_frames_processed: Count | None = None
    input_tokens: Count | None = None
    output_tokens: Count | None = None


class MaskRaster(ContractModel):
    width: Annotated[int, Field(strict=True, gt=0)]
    height: Annotated[int, Field(strict=True, gt=0)]
    # Raster is box-local, row-major binary bytes, never a full-frame mask.
    pixels: bytes

    @model_validator(mode="after")
    def valid_raster(self) -> Self:
        if len(self.pixels) != self.width * self.height or any(value not in (0, 1) for value in self.pixels):
            raise ValueError("mask raster must be complete row-major binary pixels")
        return self


class SegmentObservation(ContractModel):
    track_id: str
    frame_index: Count
    frame_key: ObjectKey
    timestamp_ms: Count
    frame_width: Annotated[int, Field(strict=True, gt=0)]
    frame_height: Annotated[int, Field(strict=True, gt=0)]
    bbox: BoundingBox
    mask: MaskRaster
    confidence: Confidence | None


class SegmentResult(ContractModel):
    request_id: UUID
    prompt_id: Annotated[str, Field(min_length=1, max_length=128)]
    model: Literal["sam-3.1"] = "sam-3.1"
    parser_version: Literal["meta-sam-parser==0.1.0"] = "meta-sam-parser==0.1.0"
    provenance: Literal["synthetic", "cached", "live"]
    observations: tuple[SegmentObservation, ...]
    usage: ProviderUsage
    limits_encountered: tuple[str, ...] = ()


class Segmenter(Protocol):
    def segment(self, media_ref: MediaRef, prompt_id: str, request_id: UUID) -> SegmentResult: ...


class Verifier(Protocol):
    def verify(self, candidate: Finding, context: tuple[Finding, ...], rubric: str) -> VerificationVerdict: ...


class ClaimChecker(Protocol):
    def check(self, listing: str, evidence: tuple[Finding, ...]) -> tuple[ClaimResult, ...]: ...


class Narrator(Protocol):
    def narrate(self, scope_evidence: tuple[Finding, ...]) -> tuple[NarratedStatement, ...]: ...
