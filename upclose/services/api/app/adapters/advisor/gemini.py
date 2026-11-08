"""Gemini advisory provider over the official REST `generateContent` with a JSON response schema.

Official references (checked 2026-11-07):
- https://aistudio.google.com/api-reference/generate-content
  (`POST /v1beta/models/{model}:generateContent` on `https://generativelanguage.googleapis.com`)
- https://aistudio.google.com/docs/structured-output (JSON mode via `generationConfig`
  `responseMimeType: "application/json"` plus a `responseSchema`)
- https://aistudio.google.com/docs/models (current Flash IDs include `gemini-3.8-flash`;
  stable previous-generation Flash is `gemini-3.6-flash`)

Gemini stays behind the same dispatch grant as Meta: one transport attempt, a fixed output cap
(`generationConfig.maxOutputTokens`), usage read from `usageMetadata` before validation. Billed
output is candidates plus thought tokens. Whether `maxOutputTokens` also bounds thought tokens
is UNVERIFIED, so price thoughts from the evidence and do not rely on the cap. Gemini is not in
the approved price table, so no grant should be issued for it yet.

The model ID is never invented here; it comes from `ADVISOR_GEMINI_MODEL`. The API key
travels in the `x-goog-api-key` header so it never lands in a URL, an error, or a log.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from app.briefing.dispatch import (DispatchGrant, MAX_ADVISORY_OUTPUT_TOKENS, PreparedRequest, Usage,
                                   UsageRecorder, sanitized_id)
from app.contracts.sources import SourceSummary
from app.domain.advisory import (AdvisoryConfig, build_prompt, citable_keys, parse_response_text)

from . import (AdvisorProviderError, Transport, dispatch_advisory, effective_timeout, int_or_none,
               prepare_advisory, urllib_transport)
from .config import AdvisorEnv

PROVIDER = "gemini"
API_BASE = "https://generativelanguage.googleapis.com"
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

SYSTEM_INSTRUCTION = (
    "You are helping a used-car buyer. The evidence may be simulated bench data and every "
    "value is an uncalibrated estimate, not a measurement. Do not diagnose with certainty; "
    "phrase everything as questions for the seller and checks for a qualified mechanic. "
    "Never claim the vehicle is problem-free."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "questions_for_seller": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"text": {"type": "string"},
                                     "cites": {"type": "array", "items": {"type": "string"}}},
                      "required": ["text", "cites"]},
        },
        "mechanic_checks": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"text": {"type": "string"},
                                     "cites": {"type": "array", "items": {"type": "string"}}},
                      "required": ["text", "cites"]},
        },
        "summary": {"type": "string"},
    },
    "required": ["questions_for_seller", "mechanic_checks", "summary"],
}


def _candidate_text(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    parts: list[str] = []
    for candidate in candidates:
        content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
        raw_parts = content.get("parts", []) if isinstance(content, dict) else []
        if not isinstance(raw_parts, list):
            continue
        for part in raw_parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    text = "".join(parts).strip()
    return text or None


def _usage(status: int, raw: bytes) -> Usage | None:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    meta = payload.get("usageMetadata") if isinstance(payload, dict) else None
    if not isinstance(meta, dict):
        return None
    tokens_in = int_or_none(meta.get("promptTokenCount"))
    candidates = int_or_none(meta.get("candidatesTokenCount"))
    thoughts = int_or_none(meta.get("thoughtsTokenCount")) or 0
    if tokens_in is None or candidates is None:
        return None
    cached = int_or_none(meta.get("cachedContentTokenCount"))
    if cached is not None and cached > tokens_in:
        cached = None
    return Usage(input_tokens=tokens_in, cached_input_tokens=cached, output_tokens=candidates + thoughts,
                 provider_request_id=sanitized_id(payload.get("responseId")))


class GeminiAdvisor:
    def __init__(self, env: AdvisorEnv, config: AdvisoryConfig,
                 transport: Transport = urllib_transport) -> None:
        if env.provider != PROVIDER or not env.api_key or not env.model:
            raise AdvisorProviderError("gemini advisory is not configured")
        if not _MODEL_ID.match(env.model):
            raise AdvisorProviderError("gemini advisory model ID is malformed")
        self._env = env
        self._config = config
        self._transport = transport

    def _url(self) -> str:
        return f"{API_BASE}/v1beta/models/{self._env.model}:generateContent"

    def prepare(self, summaries: Sequence[SourceSummary]) -> PreparedRequest:
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": build_prompt(summaries, self._config)}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": RESPONSE_SCHEMA,
                                 "maxOutputTokens": MAX_ADVISORY_OUTPUT_TOKENS}}).encode()
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
        headers = {"x-goog-api-key": self._env.api_key, "Content-Type": "application/json"}

        def interpret(status: int, raw: bytes) -> tuple[dict, bool]:
            if status != 200:
                raise AdvisorProviderError(f"gemini advisory provider returned HTTP {status}",
                                           code=f"http_{status}", http_status=status)
            try:
                payload = json.loads(raw)
            except ValueError:
                raise AdvisorProviderError("gemini advisory returned malformed data", code="malformed") from None
            text = _candidate_text(payload)
            if text is None:
                if isinstance(payload, dict) and "promptFeedback" in payload:
                    raise AdvisorProviderError("gemini advisory prompt was blocked", code="blocked")
                raise AdvisorProviderError("gemini advisory returned no text", code="no_text")
            wire = parse_response_text(text, citable, provider=PROVIDER, model=self._env.model,
                                       config=self._config).to_dict()
            return wire, wire.get("status") != "not_available"

        return dispatch_advisory(prepared, grant, record, transport=self._transport, headers=headers,
                                 timeout=timeout, usage_of=_usage, interpret=interpret, label="gemini advisory")
