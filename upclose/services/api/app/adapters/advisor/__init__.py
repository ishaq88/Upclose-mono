"""Buyer-advisory provider adapters. `ADVISOR_PROVIDER=fixture` (default), `meta`, or `gemini`.

Live providers make exactly one transport attempt per prepared request and only after a
dispatch grant (see app.briefing.dispatch); there is no inner retry. Keys, VINs, and raw
provider payloads never appear in errors or logs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.briefing.dispatch import (DispatchError, DispatchGrant, PreparedRequest, PreparedUnits, Usage,
                                   UsageRecorder, execute_once, prepare_request, sanitized_id,
                                   MAX_ADVISORY_OUTPUT_TOKENS)
from app.contracts.sources import SourceSummary
from app.domain.advisory import AdvisoryConfig, load_advisory_config, redact_vins

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "advisory.v1.json"


class AdvisorProviderError(DispatchError):
    """Provider failure; message never contains the key, a VIN, or a raw payload."""

    def __init__(self, message: str, *, retryable: bool = False, code: str = "provider_error",
                 http_status: int | None = None) -> None:
        super().__init__(message, code=code, http_status=http_status)
        self.retryable = retryable


class AdvisorAdapter(Protocol):
    def advise(self, summaries: Sequence[SourceSummary], *, grant: DispatchGrant | None = None,
               record: UsageRecorder | None = None, deadline_seconds: float | None = None) -> dict: ...


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https base
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdvisorProviderError("advisor provider request timed out", retryable=True, code="timeout") from exc


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


def effective_timeout(config: AdvisoryConfig, deadline_seconds: float | None) -> float:
    """Single-attempt timeout: the configured bound, shortened to the remaining deadline."""
    timeout = config.timeout_seconds if deadline_seconds is None else min(config.timeout_seconds, deadline_seconds)
    if timeout <= 0.05:
        raise AdvisorProviderError("advisory deadline already reached; nothing was sent", code="deadline")
    return timeout


def prepare_advisory(*, provider: str, model: str, url: str, body: bytes) -> PreparedRequest:
    """Cap check: the body (prompt, schema, instructions) is the whole input, bounded in bytes."""
    return prepare_request(kind="advisory", provider=provider, model=model, url=url, body=body,
                           units=PreparedUnits(input_tokens=len(body), output_tokens=MAX_ADVISORY_OUTPUT_TOKENS))


def int_or_none(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def dispatch_advisory(prepared: PreparedRequest, grant: DispatchGrant | None, record: UsageRecorder | None, *,
                      transport: Transport, headers: dict[str, str], timeout: float,
                      usage_of: Callable[[int, bytes], Usage | None],
                      interpret: Callable[[int, bytes], tuple[dict, bool]], label: str) -> dict:
    """One transport attempt behind the grant; never retries."""
    return execute_once(prepared, grant, record,
                        send=lambda: transport(prepared.url, prepared.body, headers, timeout),
                        usage_of=usage_of, interpret=interpret, fail_label=label,
                        error_type=AdvisorProviderError)


__all__ = ["AdvisorAdapter", "AdvisorProviderError", "Transport", "get_advisor_adapter", "sanitized_id"]
