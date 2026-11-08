"""Owner routes for the private engine-audio upload (create, presign, complete, abort)."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.storage import ObjectStorage
from app.contracts.api import EmptyRequest, UploadUrlsRequest, UploadUrlsResponse
from app.contracts.audio import AudioAcceptedResponse, AudioUploadResponse, CreateAudioUploadRequest
from app.routers.sessions import owner_bearer
from app.services.audio_uploads import AudioUploadService
from app.settings import Settings


def create_audio_upload_router(sessions: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    service = AudioUploadService(sessions, settings, storage)

    @router.post("/sessions/{session_id}/audio", response_model=AudioUploadResponse, status_code=201)
    def create_audio(session_id: UUID, body: CreateAudioUploadRequest,
                     token: Annotated[str, Depends(owner_bearer)]) -> AudioUploadResponse:
        return service.create(token, session_id, body)

    @router.post("/audio/{upload_id}/urls", response_model=UploadUrlsResponse)
    def audio_urls(upload_id: UUID, body: UploadUrlsRequest,
                   token: Annotated[str, Depends(owner_bearer)]) -> UploadUrlsResponse:
        return service.urls(token, upload_id, tuple(body.part_numbers))

    @router.post("/audio/{upload_id}/complete", response_model=AudioAcceptedResponse)
    def complete_audio(upload_id: UUID, body: EmptyRequest,
                       token: Annotated[str, Depends(owner_bearer)]) -> AudioAcceptedResponse:
        return service.complete(token, upload_id)

    @router.delete("/audio/{upload_id}", status_code=204)
    def abort_audio(upload_id: UUID, token: Annotated[str, Depends(owner_bearer)]) -> Response:
        service.abort(token, upload_id)
        return Response(status_code=204)

    return router
