"""Buyer-advisory provider adapters. `ADVISOR_PROVIDER=fixture` (default), `meta`, or `gemini`.

One call per request, timeout-ep-bounded, at most one retry on 5xx/timeout. Keys, VINs,
and raw provider payloads never appear in errors or logs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.contracts.sources import SourceSummary
from app.domain.advisory import AdvisoryConfig, load_advisory_config, redact_vins

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "advisory.v1.json"


class AdvisorProviderError(RuntimeError):
    """Provider failure; message never contains the key, a VIN, or a raw payload."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class AdvisorAdapter(Protocol):
    def advise(self, summaries: Sequence[SourceSummary]) -> dict: ...


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https base
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdvisorProviderError("advisor provider request timed out", retryable=True) from exc


def post_with_retry(transport: Transport, url: str, body: bytes, headers: dict[str, str],
                    timeout: float, max_attempts: int) -> tuple[int, bytes]:
    """One POST plus at most one retry on 5xx/timeout. 4xx and payload errors never retry."""
    last: tuple[int, bytes] | None = None
    for _ in range(max(1, max_attempts)):
        try:
            status, raw = transport(url, body, headers, timeout)
        except AdvisorProviderError as exc:
            if not exc.retryable:
                raise
            last = None
            continue
        if status >= 500:
            last = (status, raw)
            continue
        return status, raw
    if last is not None:
        raise AdvisorProviderError(f"advisor provider returned HTTP {last[0]}")
    raise AdvisorProviderError("advisor provider request timed out")


def _safe_error(prefix: str, detail: str = "") -> AdvisorProviderError:
    text = f"{prefix} {detail}".strip()
    return AdvisorProviderError(redact_vins(text)[:240])


@dataclass(frozen=True)
class ResolvedAdapter:
    provider: str
    model: str


def get_advisor_adapter(environ: Mapping[str, str] | None = None,
                        transport: Transport | None = None,
                        config_path: str | Path | None = None) -> AdvisorAdapter:
    """Select the advisory provider from the environment. No network at selection time."""
    from .config import load_env
    env = load_env(environ)
    config: AdvisoryConfig = load_advisory_config(config_path or DEFAULT_CONFIG_PATH)
    if env.provider == "fixture":
        from .fixture import FixtureAdvisor
        return FixtureAdvisor(config)
    sender = transport or urllib_transport
    if env.provider == "meta":
        from .meta import MetaAdvisor
        return MetaAdvisor(env, config, sender)
    from .gemini import GeminiAdvisor
    return GeminiAdvisor(env, config, sender)


def wire_json(result) -> str:
    """Serialize an advisory result for logs or tests without leaking secrets."""
    return json.dumps(result.to_dict())
