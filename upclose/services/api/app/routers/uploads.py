"""Owner multipart upload routes; completion never streams media through the API."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.storage import ObjectStorage
from app.contracts.api import (CreateUploadRequest, CreateUploadResponse, EmptyRequest, PartAcceptedResponse,
    UploadResponse, UploadUrlsRequest, UploadUrlsResponse)
from app.routers.sessions import owner_bearer
from app.services.problems import ProblemError
from app.services.uploads import UploadService, too_large
from app.settings import Settings

MAX_CLIP_BYTES = 200 * 1024 * 1024


async def reject_oversized_declaration(request: Request) -> None:
    # The frozen request model reports an oversize clip as generic VALIDATION; name it precisely.
    try:
        body = await request.json()
    except ValueError:
        return
    length = body.get("content_length") if isinstance(body, dict) else None
    if isinstance(length, int) and not isinstance(length, bool) and length > MAX_CLIP_BYTES:
        raise too_large("A clip may not exceed 200 MiB.")


def create_upload_router(sessions: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    service = UploadService(sessions, settings, storage)

    @router.post("/sessions/{session_id}/parts", response_model=CreateUploadResponse, status_code=201,
                 dependencies=[Depends(reject_oversized_declaration)])
    def create_upload(session_id: UUID, body: CreateUploadRequest, token: Annotated[str, Depends(owner_bearer)],
                      idempotency_key: Annotated[str | None, Header()] = None) -> CreateUploadResponse:
        return service.create(token, session_id, body, idempotency_key)

    @router.post("/uploads/{upload_id}/urls", response_model=UploadUrlsResponse)
    def upload_urls(upload_id: UUID, body: UploadUrlsRequest,
                    token: Annotated[str, Depends(owner_bearer)]) -> UploadUrlsResponse:
        return service.urls(token, upload_id, body.part_numbers)

    @router.get("/uploads/{upload_id}", response_model=UploadResponse)
    def get_upload(upload_id: UUID, token: Annotated[str, Depends(owner_bearer)]) -> UploadResponse:
        return service.get(token, upload_id)

    @router.post("/uploads/{upload_id}/complete", response_model=PartAcceptedResponse, status_code=202)
    def complete_upload(upload_id: UUID, body: EmptyRequest,
                        token: Annotated[str, Depends(owner_bearer)]) -> PartAcceptedResponse:
        return service.complete(token, upload_id)

    @router.delete("/uploads/{upload_id}", status_code=204)
    def abort_upload(upload_id: UUID, token: Annotated[str, Depends(owner_bearer)]) -> Response:
        service.abort(token, upload_id)
        return Response(status_code=204)

    return router
