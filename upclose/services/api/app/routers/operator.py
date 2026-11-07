"""Operator-only sanitized cost totals. No mutation or provider endpoints."""
from hmac import compare_digest
from uuid import uuid4

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from app.services.budgets import BudgetService


def create_operator_router(sessions: sessionmaker[Session], operator_token: str | None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/operator/budgets", response_model=None)
    def budgets(authorization: str | None = Header(default=None)) -> dict | JSONResponse:
        if not operator_token:
            return JSONResponse(status_code=404, media_type="application/problem+json", headers={"Cache-Control": "private, no-store"},
                                content={"type": "about:blank", "title": "Not found", "status": 404,
                                         "code": "NOT_FOUND", "detail": "Not found.", "request_id": str(uuid4())})
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not compare_digest(credential.encode(), operator_token.encode()):
            return JSONResponse(status_code=401, media_type="application/problem+json", headers={"Cache-Control": "private, no-store", "WWW-Authenticate": "Bearer"},
                                content={"type": "about:blank", "title": "Unauthorized", "status": 401,
                                         "code": "TOKEN_INVALID", "detail": "Invalid operator credential.", "request_id": str(uuid4())})
        return JSONResponse(BudgetService(sessions).operator_totals(), headers={"Cache-Control": "private, no-store"})

    return router
