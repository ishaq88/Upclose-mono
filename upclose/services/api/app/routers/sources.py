"""Owner route for evidence-source plugins (capture kits). Body is a strict, kind-discriminated envelope."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.sources import SourceListResponse, SourceRequest, SourceResponse
from app.routers.sessions import owner_bearer
from app.services.sources import SourceService
from app.settings import Settings


def create_source_router(sessions: sessionmaker[DBSession], settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    service = SourceService(sessions, settings)

    @router.post("/sessions/{session_id}/sources", response_model=SourceResponse, status_code=201)
    def attach_source(session_id: UUID, body: SourceRequest,
                      token: Annotated[str, Depends(owner_bearer)]) -> SourceResponse:
        return service.attach(token, session_id, body)

    @router.get("/sessions/{session_id}/sources", response_model=SourceListResponse)
    def list_sources(session_id: UUID, token: Annotated[str, Depends(owner_bearer)]) -> SourceListResponse:
        return service.list(token, session_id)

    return router
