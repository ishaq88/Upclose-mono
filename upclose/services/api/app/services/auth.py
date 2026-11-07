"""Opaque owner authority: only a SHA-256 hash reaches persistence."""
import hashlib
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models import Session
from app.contracts.enums import SessionState
from .problems import ProblemError, not_found


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(authorization: str | None) -> str:
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and re.fullmatch(r"[A-Za-z0-9_-]{22,256}", token):
            return token
    raise unauthorized()


def unauthorized() -> ProblemError:
    return ProblemError(401, "TOKEN_INVALID", "Unauthorized", "A valid owner bearer token is required.")


def authenticate(db: DBSession, token: str) -> UUID:
    owner_id = db.scalar(select(Session.id).where(Session.token_hash == token_hash(token)))
    if owner_id is None:
        raise unauthorized()
    return owner_id


def owner_session(db: DBSession, token: str, session_id: UUID | None = None, *, lock: bool = False) -> Session:
    owner_id = authenticate(db, token)
    if session_id is not None and owner_id != session_id:
        raise not_found()
    query = select(Session).where(Session.id == owner_id)
    if lock:
        query = query.with_for_update()
    owner = db.scalar(query)
    if owner is None:
        raise unauthorized()
    if owner.state in (SessionState.DELETING, SessionState.DELETED):
        raise not_found()
    return owner
