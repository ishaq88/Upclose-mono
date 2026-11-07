"""Owner-only continuous-video uploads; bytes go directly to private storage."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.storage import ObjectStorage
from app.contracts.api import EmptyRequest, UploadUrlsRequest, UploadUrlsResponse
from app.contracts.video import (CreateVideoUploadRequest, CreateVideoUploadResponse,
    VideoAcceptedResponse, VideoSourceResponse, VideoUploadResponse)
from app.routers.sessions import owner_bearer
from app.routers.uploads import reject_oversized_declaration
from app.services.video_uploads import VideoUploadService
from app.settings import Settings


def create_video_upload_router(sessions: sessionmaker[DBSession], settings: Settings,
                               storage: ObjectStorage) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    service = VideoUploadService(sessions, settings, storage)

    @router.post("/sessions/{session_id}/video", response_model=CreateVideoUploadResponse, status_code=201,
                 dependencies=[Depends(reject_oversized_declaration)])
    def create(session_id: UUID, body: CreateVideoUploadRequest,
               token: Annotated[str, Depends(owner_bearer)],
               idempotency_key: Annotated[str | None, Header()] = None) -> CreateVideoUploadResponse:
        return service.create(token, session_id, body, idempotency_key)

    @router.get("/sessions/{session_id}/video", response_model=VideoSourceResponse)
    def current(session_id: UUID, token: Annotated[str, Depends(owner_bearer)]) -> VideoSourceResponse:
        return service.current(token, session_id)

    @router.get("/video-uploads/{upload_id}", response_model=VideoUploadResponse)
    def get(upload_id: UUID, token: Annotated[str, Depends(owner_bearer)]) -> VideoUploadResponse:
        return service.get(token, upload_id)

    @router.post("/video-uploads/{upload_id}/urls", response_model=UploadUrlsResponse)
    def urls(upload_id: UUID, body: UploadUrlsRequest,
             token: Annotated[str, Depends(owner_bearer)]) -> UploadUrlsResponse:
        return service.urls(token, upload_id, body.part_numbers)

    @router.post("/video-uploads/{upload_id}/complete", response_model=VideoAcceptedResponse, status_code=202)
    def complete(upload_id: UUID, body: EmptyRequest,
                 token: Annotated[str, Depends(owner_bearer)]) -> VideoAcceptedResponse:
        return service.complete(token, upload_id)

    @router.delete("/video-uploads/{upload_id}", status_code=204)
    def abort(upload_id: UUID, token: Annotated[str, Depends(owner_bearer)]) -> Response:
        service.abort(token, upload_id)
        return Response(status_code=204)

    return router
