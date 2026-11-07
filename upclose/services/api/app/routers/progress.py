"""Owner progress route; `Cache-Control: private, no-store` comes from the app-wide middleware."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.api import ProgressResponse
from app.routers.sessions import owner_bearer
from app.services.progress import ProgressService
from app.settings import Settings


def create_progress_router(sessions: sessionmaker[DBSession], settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    service = ProgressService(sessions, settings)

    @router.get("/sessions/{session_id}/progress", response_model=ProgressResponse)
    def get_progress(session_id: UUID, token: Annotated[str, Depends(owner_bearer)]) -> ProgressResponse:
        return service.get(token, session_id)

    return router
