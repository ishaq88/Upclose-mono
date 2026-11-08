"""VIN provider environment. Read only here; app/settings.py is untouched."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from . import VinConfigError


@dataclass(frozen=True)
class VinEnv:
    provider: Literal["fixture", "nhtsa"]


def load_env(environ: Mapping[str, str] | None = None) -> VinEnv:
    env = os.environ if environ is None else environ
    provider = (env.get("VIN_PROVIDER") or "fixture").strip().lower()
    if provider == "fixture":
        return VinEnv(provider="fixture")
    if provider == "nhtsa":
        return VinEnv(provider="nhtsa")
    raise VinConfigError("VIN_PROVIDER must be 'fixture' or 'nhtsa'")
