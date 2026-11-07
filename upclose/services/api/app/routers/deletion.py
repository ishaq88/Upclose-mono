"""Owner-authorized, naturally idempotent media deletion."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.api import DeleteMediaResponse, EmptyRequest
from app.routers.sessions import owner_bearer
from app.services.deletion import request_deletion
from app.settings import Settings


def create_deletion_router(factory: sessionmaker[DBSession], settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.post("/sessions/{session_id}/delete-media", response_model=DeleteMediaResponse, status_code=202)
    def delete_media(session_id: UUID, body: EmptyRequest, response: Response,
                     token: Annotated[str, Depends(owner_bearer)]) -> DeleteMediaResponse:
        response.headers["Cache-Control"] = "private, no-store"
        return request_deletion(factory, token, session_id)

    return router
