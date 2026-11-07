"""Operator authority is separate from owner/share authority and disabled when unset."""
from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Header, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session as DBSession, sessionmaker
from app.adapters.storage import ObjectStorage
from app.contracts.evidence import BoundingBox
from app.services.privacy import PrivacyService, operator_auth
from app.settings import Settings

class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: UUID
    revision_id: UUID
    finding_id: UUID
    redactions: tuple[BoundingBox, ...] = Field(default=(), max_length=100)

class Approval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sha256: str = Field(pattern='^[0-9a-f]{64}$')
    manual_review_receipt: str = Field(max_length=128)


def create_privacy_router(factory: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage) -> APIRouter:
    router = APIRouter()
    service = PrivacyService(factory, storage)
    @router.post('/api/v1/operator/privacy/derivatives', status_code=201)
    def candidate(body: Candidate, response: Response, x_operator_token: Annotated[str | None, Header()] = None) -> dict[str, str]:
        operator_auth(settings, x_operator_token)
        response.headers['Cache-Control'] = 'private, no-store'
        return service.create(body.session_id, body.revision_id, body.finding_id, body.redactions)
    @router.post('/api/v1/operator/privacy/derivatives/{derivative_id}/approve')
    def approve(derivative_id: UUID, body: Approval, response: Response,
                x_operator_token: Annotated[str | None, Header()] = None) -> dict[str, str]:
        operator_auth(settings, x_operator_token)
        response.headers['Cache-Control'] = 'private, no-store'
        return service.approve(derivative_id, body.sha256, body.manual_review_receipt)
    @router.get('/api/v1/operator/privacy/derivatives/{derivative_id}')
    def preview(derivative_id: UUID, x_operator_token: Annotated[str | None, Header()] = None) -> Response:
        operator_auth(settings, x_operator_token)
        return Response(service.preview(derivative_id), media_type='image/png', headers={
            'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})
    return router
