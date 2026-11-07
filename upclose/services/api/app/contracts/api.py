from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BeforeValidator, Field, HttpUrl, model_validator

from .base import ContractModel, Count, PartNumber, UTCDateTime
from .enums import PART_ZONES, PartState, ReportState, SessionState, UploadState, Zone
from .reports import Part1Verdict


class CreateSessionRequest(ContractModel):
    purpose: Literal["buyer"]
    capture_mode: Literal["guided", "single_video"] = "guided"
    consent_version: Annotated[str, Field(min_length=1, max_length=128)]
    consent: Literal[True]
    listing_text: Annotated[str, Field(max_length=10000)] | None = None


class CreateSessionResponse(ContractModel):
    session_id: UUID
    session_token: Annotated[str, Field(min_length=22, max_length=256)]


class UpdateListingRequest(ContractModel):
    listing_text: Annotated[str, Field(max_length=10000)]


class UpdateListingResponse(ContractModel):
    listing_version: Annotated[int, Field(strict=True, ge=1)]


def _clean_vehicle_label(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("vehicle label must be text")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 80:
        raise ValueError("vehicle label must be 1-80 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise ValueError("vehicle label must not contain control characters")
    return cleaned


VehicleLabel = Annotated[str, BeforeValidator(_clean_vehicle_label), Field(min_length=1, max_length=80)]


class UpdateVehicleLabelRequest(ContractModel):
    label: VehicleLabel


class PartSummary(ContractModel):
    id: UUID
    part_no: PartNumber
    zone: Zone
    state: PartState
    reason: Annotated[str, Field(max_length=240)] | None = None
    # Owner-only: lets a client that lost its local upload record abort a stranded attempt.
    active_upload_id: UUID | None = None

    @model_validator(mode="after")
    def canonical_part(self) -> Self:
        if self.zone != PART_ZONES[self.part_no - 1]:
            raise ValueError("part number and zone disagree")
        return self


class SessionResponse(ContractModel):
    session_id: UUID
    purpose: Literal["buyer"]
    capture_mode: Literal["guided", "single_video"] = "guided"
    state: SessionState
    consent_at: UTCDateTime
    listing_text: Annotated[str, Field(max_length=10000)] | None
    listing_version: Annotated[int, Field(strict=True, ge=1)]
    vehicle_label: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    parts: tuple[PartSummary, ...]


class CreateUploadRequest(ContractModel):
    part_no: PartNumber
    content_type: Literal["video/mp4", "video/webm", "video/quicktime"]
    content_length: Annotated[int, Field(strict=True, gt=0, le=200 * 1024 * 1024)]


class CreateUploadResponse(ContractModel):
    part_id: UUID
    upload_id: UUID
    chunk_size_bytes: Literal[8388608] = 8388608
    chunk_count: Annotated[int, Field(strict=True, ge=1, le=25)]


class UploadUrlsRequest(ContractModel):
    part_numbers: Annotated[tuple[Annotated[int, Field(strict=True, ge=1, le=25)], ...], Field(min_length=1, max_length=10)]

    @model_validator(mode="after")
    def unique_pieces(self) -> Self:
        if len(set(self.part_numbers)) != len(self.part_numbers):
            raise ValueError("piece numbers must be unique")
        return self


class UploadUrl(ContractModel):
    part_number: Annotated[int, Field(strict=True, ge=1, le=25)]
    url: HttpUrl
    expires_at: UTCDateTime
    headers: dict[str, str]


class UploadUrlsResponse(ContractModel):
    items: Annotated[tuple[UploadUrl, ...], Field(max_length=10)]


class UploadedPiece(ContractModel):
    part_number: Annotated[int, Field(strict=True, ge=1, le=25)]
    etag: Annotated[str, Field(min_length=1, max_length=256)]
    size_bytes: Annotated[int, Field(strict=True, gt=0, le=8388608)]


class UploadResponse(ContractModel):
    upload_id: UUID
    state: UploadState
    pieces: tuple[UploadedPiece, ...]


class EmptyRequest(ContractModel):
    pass


class PartAcceptedResponse(ContractModel):
    part_id: UUID
    state: PartState


class SkipPartResponse(ContractModel):
    state: Literal["skipped"] = "skipped"


class FinalizeResponse(ContractModel):
    state: Literal["finalized"] = "finalized"
    report_slug: Annotated[str, Field(min_length=1, max_length=128)]


class ReportProgress(ContractModel):
    slug: Annotated[str, Field(min_length=1, max_length=128)]
    state: ReportState
    unlocked: bool = False


class ProgressResponse(ContractModel):
    state: SessionState
    parts: tuple[PartSummary, ...]
    free_verdict: Part1Verdict | None
    report: ReportProgress | None


class CheckoutRequest(ContractModel):
    product: Literal["report_unlock"]


class CheckoutResponse(ContractModel):
    checkout_url: HttpUrl | None = None
    unlocked: bool = False


class DeleteMediaResponse(ContractModel):
    state: Literal["deleting", "deleted"]


class Problem(ContractModel):
    type: str
    title: Annotated[str, Field(min_length=1, max_length=240)]
    status: Annotated[int, Field(strict=True, ge=400, le=599)]
    code: Literal["TOKEN_INVALID", "NOT_FOUND", "STATE_CONFLICT", "UPLOAD_TOO_LARGE", "UPLOAD_INCOMPLETE", "RATE_LIMITED", "VALIDATION", "REPORT_NOT_READY", "INTERNAL", "EMAIL_TAKEN", "INVALID_CREDENTIALS"]
    detail: Annotated[str, Field(max_length=1000)]
    request_id: UUID
