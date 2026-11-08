"""Engine-audio upload wire format. The clip lives in private object storage; reports never carry it."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from .base import ContractModel

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 120
AudioContentType = Literal["audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4", "audio/x-m4a"]


class CreateAudioUploadRequest(ContractModel):
    content_type: AudioContentType
    content_length: Annotated[int, Field(strict=True, ge=1, le=MAX_AUDIO_BYTES)]


class AudioUploadResponse(ContractModel):
    upload_id: UUID
    chunk_count: Annotated[int, Field(strict=True, ge=1)]


class AudioAcceptedResponse(ContractModel):
    upload_id: UUID
    state: Literal["accepted"] = "accepted"
