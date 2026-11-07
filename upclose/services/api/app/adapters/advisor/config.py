"""Advisor provider environment. Read only here; app/settings.py is untouched."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

META_API_BASE_DEFAULT = "https://api.meta.ai"


class AdvisorConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdvisorEnv:
    provider: str
    api_key: str = field(default="", repr=False)
    api_base: str = ""
    model: str = ""


def load_env(environ: Mapping[str, str] | None = None) -> AdvisorEnv:
    env = os.environ if environ is None else environ
    provider = (env.get("ADVISOR_PROVIDER") or "fixture").strip().lower()
    if provider == "fixture":
        return AdvisorEnv(provider="fixture")
    if provider == "meta":
        missing = [name for name in ("MODEL_API_KEY", "ADVISOR_META_MODEL") if not (env.get(name) or "").strip()]
        if missing:
            raise AdvisorConfigError(
                "missing required environment variable(s): " + ", ".join(missing)
                + ". No text model ID is invented here; set ADVISOR_META_MODEL explicitly.")
        base = (env.get("META_API_BASE") or META_API_BASE_DEFAULT).strip().rstrip("/") or META_API_BASE_DEFAULT
        if not base.startswith("https://"):
            raise AdvisorConfigError("META_API_BASE must be an https URL")
        return AdvisorEnv(provider="meta", api_key=(env.get("MODEL_API_KEY") or "").strip(),
                          api_base=base, model=(env.get("ADVISOR_META_MODEL") or "").strip())
    if provider == "gemini":
        missing = [name for name in ("GEMINI_API_KEY", "ADVISOR_GEMINI_MODEL") if not (env.get(name) or "").strip()]
        if missing:
            raise AdvisorConfigError("missing required environment variable(s): " + ", ".join(missing))
        return AdvisorEnv(provider="gemini", api_key=(env.get("GEMINI_API_KEY") or "").strip(),
                          model=(env.get("ADVISOR_GEMINI_MODEL") or "").strip())
    raise AdvisorConfigError("ADVISOR_PROVIDER must be 'fixture', 'meta', or 'gemini'")
