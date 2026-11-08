"""POST a source bundle to UpClose. The owner token comes from the environment only."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

import httpx

TOKEN_ENV = "UPCLOSE_OWNER_TOKEN"


class PostError(RuntimeError):
    """Usage-level problem (missing token, bad session id)."""


@dataclass
class PostResult:
    ok: bool
    status: int | None
    message: str
    body: dict | None = None  # parsed JSON of a 201 response


def token_from_env() -> str:
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if not tok:
        raise PostError(f"set {TOKEN_ENV} in the environment (the token is never accepted on the command line)")
    return tok


def post_source(
    body: str | bytes, api: str, session: str, token: str, client: httpx.Client | None = None
) -> PostResult:
    try:
        sid = str(uuid.UUID(session))
    except ValueError:
        raise PostError("--session must be a UUID") from None
    url = f"{api.rstrip('/')}/api/v1/sessions/{sid}/sources"
    own = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        try:
            resp = client.post(
                url,
                content=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            return PostResult(False, None, f"could not reach {api}: {type(exc).__name__}")
    finally:
        if own:
            client.close()
    code = resp.status_code
    if code == 201:
        try:
            j = resp.json()
            return PostResult(
                True, code,
                f"created source {j.get('source_id')} kind={j.get('kind')} sha256={j.get('data_sha256')} summary={j.get('summary')}",
                j if isinstance(j, dict) else None,
            )
        except ValueError:
            return PostResult(True, code, "created (response was not JSON)")
    if code == 409:
        return PostResult(False, code, "409: session is already finalized; sources can only be added while it is capturing")
    if code == 401:
        return PostResult(False, code, f"401: owner token rejected; check {TOKEN_ENV}")
    if code == 404:
        return PostResult(False, code, "404: session not found (or deleted); check --session and --api")
    detail = re.sub(r"\s+", " ", resp.text)[:300]
    return PostResult(False, code, f"{code}: {detail}")
