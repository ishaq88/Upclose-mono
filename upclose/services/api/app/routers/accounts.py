"""Account HTTP API (T15b): register/login/logout/me/password/claim/my-reports.

Account Bearer [REDACTED] are a distinct authority from inspection owner
tokens and operator credentials. A freshly issued token is returned once,
on register/login only; no hash, salt, or other token is ever returned.
"""
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.email import EmailSender, build_email_sender
from app.contracts.accounts import (AccountSessionsResponse, AccountSummary, AuthResponse,
    ChangePasswordRequest, ClaimSessionRequest, ClaimSessionResponse, LoginRequest,
    PasswordResetConfirmRequest, PasswordResetRequest, RegisterRequest)
from app.models.schema import Account
from app.services.account_auth import account_bearer_token, authenticate_account
from app.services.accounts import AccountService
from app.services.password_reset import PasswordResetService
from app.services.rate_limits import source_fingerprint
from app.services.sessions import SessionService
from app.settings import Settings


def _summary(account: Account) -> AccountSummary:
    return AccountSummary(id=account.id, email=account.email,
                          created_at=account.created_at, disabled=account.disabled)


def create_account_router(sessions: sessionmaker[DBSession], settings: Settings,
                          email_sender: EmailSender | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/accounts")
    accounts = AccountService(sessions, settings)
    resets = PasswordResetService(sessions, settings, email_sender or build_email_sender(settings))
    inspections = SessionService(sessions, settings)

    def current(authorization: Annotated[str | None, Header()] = None) -> Account:
        return authenticate_account(accounts, authorization)

    @router.post("/register", response_model=AuthResponse, status_code=201)
    def register(body: RegisterRequest) -> AuthResponse:
        account = accounts.register(body.email, body.password)
        issued = accounts.issue_token(account.id)
        return AuthResponse(account=_summary(account), token=issued.token,
                            expires_at=issued.expires_at)

    @router.post("/login", response_model=AuthResponse)
    def login(body: LoginRequest, request: Request) -> AuthResponse:
        result = accounts.login(body.email, body.password,
                                source_key=source_fingerprint(request, settings.trusted_proxy_hosts))
        return AuthResponse(account=_summary(result.account), token=result.issued.token,
                            expires_at=result.issued.expires_at)

    @router.post("/logout", status_code=204)
    def logout(authorization: Annotated[str | None, Header()] = None) -> Response:
        token = account_bearer_token(authorization)
        authenticate_account(accounts, authorization)
        accounts.revoke_token(token)
        return Response(status_code=204)

    @router.get("/me", response_model=AccountSummary)
    def me(authorization: Annotated[str | None, Header()] = None) -> AccountSummary:
        return _summary(current(authorization))

    @router.post("/password", status_code=204)
    def change_password(body: ChangePasswordRequest,
                        authorization: Annotated[str | None, Header()] = None) -> Response:
        token = account_bearer_token(authorization)
        account = authenticate_account(accounts, authorization)
        # keep_token spares the caller's own token; all others are revoked.
        accounts.change_password(account.id, body.current_password, body.new_password,
                                 keep_token=token)
        return Response(status_code=204)

    @router.post("/password-reset/request", status_code=202)
    def password_reset_request(body: PasswordResetRequest, request: Request) -> Response:
        # Always 202 with an empty body: unknown, disabled, malformed, and
        # throttled requests are indistinguishable from a sent reset.
        resets.request_reset(body.email,
                             source_fingerprint(request, settings.trusted_proxy_hosts))
        return Response(status_code=202)

    @router.post("/password-reset/confirm", status_code=204)
    def password_reset_confirm(body: PasswordResetConfirmRequest) -> Response:
        resets.confirm_reset(body.token, body.new_password)
        return Response(status_code=204)

    @router.post("/claim", response_model=ClaimSessionResponse)
    def claim(body: ClaimSessionRequest,
              authorization: Annotated[str | None, Header()] = None) -> ClaimSessionResponse:
        account = current(authorization)
        return inspections.claim(account.id, body.session_id, body.owner_token)

    @router.get("/sessions", response_model=AccountSessionsResponse)
    def my_sessions(authorization: Annotated[str | None, Header()] = None) -> AccountSessionsResponse:
        account = current(authorization)
        return inspections.list_for_account(account.id)

    return router
