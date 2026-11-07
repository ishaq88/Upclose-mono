"""Owner routes only; multipart/media routes arrive in their own cards."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.contracts.api import (CreateSessionRequest, CreateSessionResponse, EmptyRequest,
    FinalizeResponse, SessionResponse, SkipPartResponse, UpdateListingRequest, UpdateListingResponse,
    UpdateVehicleLabelRequest)
from app.services.account_auth import authenticate_account
from app.services.accounts import AccountService
from app.services.auth import bearer_token
from app.services.problems import ProblemError
from app.services.problems import validation
from app.services.rate_limits import source_fingerprint
from app.services.sessions import SessionService
from app.settings import Settings


def owner_bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    return bearer_token(authorization)


async def require_true_consent(request: Request) -> None:
    # Literal[True] compares equal to integer 1; check the original JSON boolean explicitly.
    try:
        body = await request.json()
    except ValueError:
        raise validation() from None
    if not isinstance(body, dict) or body.get("consent") is not True:
        raise validation()


def create_session_router(sessions: sessionmaker[DBSession], settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    service = SessionService(sessions, settings)
    accounts = AccountService(sessions, settings)

    @router.post("/sessions", response_model=CreateSessionResponse, status_code=201,
                 dependencies=[Depends(require_true_consent)])
    def create_session(body: CreateSessionRequest, request: Request,
                       authorization: Annotated[str | None, Header()] = None) -> CreateSessionResponse:
        # Optional account Bearer: absent = anonymous as before; present but
        # invalid = 401, never a silent downgrade to anonymous.
        account_id = None
        if authorization is not None:
            account_id = authenticate_account(accounts, authorization).id
        return service.create(body, source_fingerprint(request, settings.trusted_proxy_hosts),
                              account_id=account_id)

    @router.get("/sessions/{session_id}", response_model=SessionResponse)
    def get_session(session_id: UUID, token: Annotated[str, Depends(owner_bearer)],
                    authorization: Annotated[str | None, Header()] = None) -> SessionResponse:
        try:
            return service.get(token, session_id)
        except ProblemError as error:
            if error.status != 401:
                raise
            # Not an owner token: a valid account token reads only its own
            # linked inspection (404 otherwise); anything else stays 401.
            account = authenticate_account(accounts, authorization)
            return service.get_for_account(account.id, session_id)

    @router.put("/sessions/{session_id}/label", response_model=SessionResponse)
    def update_label(session_id: UUID, body: UpdateVehicleLabelRequest, token: Annotated[str, Depends(owner_bearer)]) -> SessionResponse:
        # Owner token only: an account token is not an owner token and fails
        # owner authentication (401); a valid owner token for another
        # session follows the existing 404 convention.
        return service.update_label(token, session_id, body.label)

    @router.put("/sessions/{session_id}/listing", response_model=UpdateListingResponse)
    def update_listing(session_id: UUID, body: UpdateListingRequest, token: Annotated[str, Depends(owner_bearer)],
                       idempotency_key: Annotated[str | None, Header()] = None) -> UpdateListingResponse:
        return service.update_listing(token, session_id, body.listing_text, idempotency_key)

    @router.post("/parts/{part_id}/skip", response_model=SkipPartResponse)
    def skip_part(part_id: UUID, body: EmptyRequest, token: Annotated[str, Depends(owner_bearer)]) -> SkipPartResponse:
        return service.skip(token, part_id)

    @router.post("/sessions/{session_id}/finalize", response_model=FinalizeResponse, status_code=202)
    def finalize_session(session_id: UUID, body: EmptyRequest, token: Annotated[str, Depends(owner_bearer)]) -> FinalizeResponse:
        return service.finalize(token, session_id)

    return router
