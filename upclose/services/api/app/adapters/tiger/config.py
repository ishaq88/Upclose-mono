"""Tiger adapter environment config. The URL is read here and never logged."""

import os
from collections.abc import Mapping

ENV_VAR = "TIGER_DATABASE_URL"
CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 5000
MAX_POINTS = 600


def database_url(environ: Mapping[str, str] | None = None) -> str:
    """Return the Tiger connection string, or "" when it is not configured."""
    env = os.environ if environ is None else environ
    return (env.get(ENV_VAR) or "").strip()
