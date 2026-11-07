"""Meta advisory provider over the documented Responses API.

Request shape follows the T04a doc snapshot in `.docs/evidence/provider-contract.md` and
`app/adapters/meta/transport.py`: `POST {META_API_BASE}/v1/responses` with a model ID and
one user message carrying `input_text`. The model ID is never invented here; it comes
from `ADVISOR_META_MODEL`.

Official references (checked 2026-11-07):
- https://dev.meta.ai/docs/api-reference/responses/create-response (`POST /v1/responses`,
  example model `muse-spark-1.3`)
- https://dev.meta.ai/docs/protocols/responses (Responses API serves Muse Spark)
- https://dev.meta.ai/docs/pricing-rate-limits (Standard IDs `muse-spark-1.3/1.2/1.1`)
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from app.contracts.sources import SourceSummary
from app.domain.advisory import (AdvisoryConfig, build_prompt, citable_keys, parse_response_text)

from . import AdvisorProviderError, Transport, _safe_error, post_with_retry, urllib_transport
from .config import AdvisorEnv

PROVIDER = "meta"


def _output_text(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    if not isinstance(output, list):
        return None
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text" \
                    and isinstance(part.get("text"), str):
                return part["text"]
    return None


class MetaAdvisor:
    def __init__(self, env: AdvisorEnv, config: AdvisoryConfig,
                 transport: Transport = urllib_transport) -> None:
        if env.provider != PROVIDER or not env.api_key or not env.model:
            raise AdvisorProviderError("meta advisory is not configured")
        self._env = env
        self._config = config
        self._transport = transport

    def advise(self, summaries: Sequence[SourceSummary]) -> dict:
        citable = citable_keys(summaries)
        prompt = build_prompt(summaries, self._config)
        body = json.dumps({"model": self._env.model, "input": [
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]}]}).encode()
        try:
            status, raw = post_with_retry(self._transport, f"{self._env.api_base}/v1/responses",
                                          body, {"Authorization": "Bearer " + self._env.api_key,
                                                 "Content-Type": "application/json"},
                                          self._config.timeout_seconds, self._config.max_attempts)
        except AdvisorProviderError:
            raise
        except Exception as exc:
            raise _safe_error("meta advisory request failed", type(exc).__name__) from None
        if status in (401, 403):
            raise AdvisorProviderError("meta advisory is not authorized (check MODEL_API_KEY)")
        if status == 429 or status >= 500:
            raise AdvisorProviderError(f"meta advisory provider returned HTTP {status}")
        if status != 200:
            raise AdvisorProviderError(f"meta advisory provider returned HTTP {status}")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise AdvisorProviderError("meta advisory returned malformed data") from None
        if isinstance(payload, dict) and payload.get("model") not in (None, self._env.model):
            raise AdvisorProviderError("meta advisory model mismatch")
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise AdvisorProviderError("meta advisory response was incomplete")
        text = _output_text(payload)
        if text is None:
            raise AdvisorProviderError("meta advisory returned malformed data")
        result = parse_response_text(text, citable, provider=PROVIDER, model=self._env.model,
                                     config=self._config)
        return result.to_dict()
