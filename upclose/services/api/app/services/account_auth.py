"""Account bearer-token authentication helpers (T15a).

Separate authority from inspection owner tokens (``app.services.auth``)
and operator credentials: an account token resolves to an ``Account``,
never to an inspection ``Session``. Only token hashes persist; helpers
here accept the plaintext token from the ``Authorization`` header and
delegate verification to ``AccountService.verify_token``.

Router usage (next card)::

    account = authenticate_account(db_or_service, authorization_header)

or with the service directly::

    account = service.verify_token(account_bearer_token(header))
"""
from __future__ import annotations

import re

from app.models.schema import Account

from .accounts import AccountService
from .problems import ProblemError

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22,256}")


def token_invalid() -> ProblemError:
    """The single account-token failure; never echoes the presented token."""
    return ProblemError(401, "TOKEN_INVALID", "Unauthorized",
                        "A valid account Bearer token is required.")


def account_bearer_token(authorization: str | None) -> str:
    """Extract an account token from an Authorization header or raise 401."""
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if separator and scheme.lower() == "bearer" and _TOKEN_RE.fullmatch(token):
            return token
    raise token_invalid()


def authenticate_account(service: AccountService, authorization: str | None) -> Account:
    """Resolve an Authorization header to its account via ``service``.

    Raises ``TOKEN_INVALID`` (401) for a missing/malformed header and for
    unknown, expired, revoked, or disabled-account tokens.
    """
    return service.verify_token(account_bearer_token(authorization))
