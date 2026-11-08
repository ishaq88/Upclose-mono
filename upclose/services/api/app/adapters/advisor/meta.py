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
- https://dev.meta.ai/docs/protocols/responses (`max_output_tokens` covers reasoning plus visible
  output, minimum 16; `store: false`; response `usage` token counts)

One transport attempt per prepared request, only behind a dispatch grant, with a fixed output
cap. Usage is read from the response before any validation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from app.briefing.dispatch import (DispatchGrant, MAX_ADVISORY_OUTPUT_TOKENS, PreparedRequest, Usage,
                                   UsageRecorder, sanitized_id)
from app.contracts.sources import SourceSummary
from app.domain.advisory import (AdvisoryConfig, build_prompt, citable_keys, parse_response_text)

from . import (AdvisorProviderError, Transport, dispatch_advisory, effective_timeout, int_or_none,
               prepare_advisory, urllib_transport)
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


def _usage(status: int, raw: bytes) -> Usage | None:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    tokens_in, tokens_out = int_or_none(usage.get("input_tokens")), int_or_none(usage.get("output_tokens"))
    if tokens_in is None or tokens_out is None:
        return None
    details = usage.get("input_tokens_details")
    cached = int_or_none(details.get("cached_tokens")) if isinstance(details, dict) else None
    if cached is not None and cached > tokens_in:
        cached = None
    return Usage(input_tokens=tokens_in, cached_input_tokens=cached, output_tokens=tokens_out,
                 provider_request_id=sanitized_id(payload.get("id")))


class MetaAdvisor:
    def __init__(self, env: AdvisorEnv, config: AdvisoryConfig,
                 transport: Transport = urllib_transport) -> None:
        if env.provider != PROVIDER or not env.api_key or not env.model:
            raise AdvisorProviderError("meta advisory is not configured")
        self._env = env
        self._config = config
        self._transport = transport

    def _url(self) -> str:
        return f"{self._env.api_base}/v1/responses"

    def prepare(self, summaries: Sequence[SourceSummary]) -> PreparedRequest:
        prompt = build_prompt(summaries, self._config)
        body = json.dumps({"model": self._env.model, "store": False,
                           "max_output_tokens": MAX_ADVISORY_OUTPUT_TOKENS,
                           "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}]}).encode()
        return prepare_advisory(provider=PROVIDER, model=self._env.model, url=self._url(), body=body)

    def advise(self, summaries: Sequence[SourceSummary], *, grant: DispatchGrant | None = None,
               record: UsageRecorder | None = None, deadline_seconds: float | None = None) -> dict:
        return self.advise_prepared(self.prepare(summaries), summaries, grant=grant, record=record,
                                    deadline_seconds=deadline_seconds)

    def advise_prepared(self, prepared: PreparedRequest, summaries: Sequence[SourceSummary], *,
                        grant: DispatchGrant | None, record: UsageRecorder | None,
                        deadline_seconds: float | None = None) -> dict:
        if prepared.provider != PROVIDER or prepared.model != self._env.model or prepared.kind != "advisory" \
                or prepared.url != self._url():
            raise AdvisorProviderError("prepared request does not match this advisor", code="prepared_request_invalid")
        timeout = effective_timeout(self._config, deadline_seconds)
        citable = citable_keys(summaries)
        headers = {"Authorization": "Bearer " + self._env.api_key, "Content-Type": "application/json"}

        def interpret(status: int, raw: bytes) -> tuple[dict, bool]:
            if status in (401, 403):
                raise AdvisorProviderError("meta advisory is not authorized (check MODEL_API_KEY)",
                                           code="unauthorized", http_status=status)
            if status != 200:
                raise AdvisorProviderError(f"meta advisory provider returned HTTP {status}",
                                           code=f"http_{status}", http_status=status)
            try:
                payload = json.loads(raw)
            except ValueError:
                raise AdvisorProviderError("meta advisory returned malformed data", code="malformed") from None
            if isinstance(payload, dict) and payload.get("model") not in (None, self._env.model):
                raise AdvisorProviderError("meta advisory model mismatch", code="model_mismatch")
            if not isinstance(payload, dict) or payload.get("status") != "completed":
                raise AdvisorProviderError("meta advisory response was incomplete", code="incomplete")
            text = _output_text(payload)
            if text is None:
                raise AdvisorProviderError("meta advisory returned malformed data", code="malformed")
            wire = parse_response_text(text, citable, provider=PROVIDER, model=self._env.model,
                                       config=self._config).to_dict()
            return wire, wire.get("status") != "not_available"

        return dispatch_advisory(prepared, grant, record, transport=self._transport, headers=headers,
                                 timeout=timeout, usage_of=_usage, interpret=interpret, label="meta advisory")
