"""Owner-scoped non-secret command replay under the session row lock."""
from collections.abc import Callable
from datetime import timedelta
import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models import RequestKey, Session
from .problems import conflict, validation


_FORBIDDEN = {"session_token", "token", "owner_token", "authorization", "signed_url", "url", "checkout_url"}


def _safe(value: object) -> bool:
    if isinstance(value, dict):
        return all(isinstance(key, str) and key.lower() not in _FORBIDDEN and _safe(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_safe(item) for item in value)
    return value is None or isinstance(value, (str, int, float, bool))


def execute_idempotent(db: DBSession, *, authority_id: UUID, route: str, key: str,
                       body: dict[str, object], operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    if not key or len(key) > 255:
        raise validation()
    # Serialize every owner command before touching request keys or child records.
    db.execute(select(Session.id).where(Session.id == authority_id).with_for_update())
    request_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    previous = db.scalar(select(RequestKey).where(RequestKey.authority_id == authority_id,
        RequestKey.route == route, RequestKey.key == key))
    if previous:
        if previous.request_hash != request_hash:
            raise conflict()
        return dict(previous.response)
    response = operation()
    if not _safe(response):
        raise ValueError("Idempotency response is not an explicitly safe replay DTO")
    now = db.scalar(select(func.clock_timestamp()))
    db.add(RequestKey(authority_id=authority_id, route=route, key=key, request_hash=request_hash,
                      response=response, expires_at=now + timedelta(days=30)))
    db.flush()
    return response
