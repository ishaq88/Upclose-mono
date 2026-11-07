"""Gemini advisory provider over the official REST `generateContent` with a JSON response schema.

Official references (checked 2026-11-07):
- https://aistudio.google.com/api-reference/generate-content
  (`POST /v1beta/models/{model}:generateContent` on `https://generativelanguage.googleapis.com`)
- https://aistudio.google.com/docs/structured-output (JSON mode via `generationConfig`
  `responseMimeType: "application/json"` plus a `responseSchema`)
- https://aistudio.google.com/docs/models (current Flash IDs include `gemini-3.8-flash`;
  stable previous-generation Flash is `gemini-3.6-flash`)

The model ID is never invented here; it comes from `ADVISOR_GEMINI_MODEL`. The API key
travels in the `x-goog-api-key` header so it never lands in a URL, an error, or a log.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from app.contracts.sources import SourceSummary
from app.domain.advisory import (AdvisoryConfig, build_prompt, citable_keys, parse_response_text)

from . import AdvisorProviderError, Transport, _safe_error, post_with_retry, urllib_transport
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

    def advise(self, summaries: Sequence[SourceSummary]) -> dict:
        citable = citable_keys(summaries)
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "contents": [{"role": "user", "parts": [{"text": build_prompt(summaries, self._config)}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": RESPONSE_SCHEMA}}).encode()
        url = f"{API_BASE}/v1beta/models/{self._env.model}:generateContent"
        try:
            status, raw = post_with_retry(self._transport, url, body,
                                          {"x-goog-api-key": self._env.api_key,
                                           "Content-Type": "application/json"},
                                          self._config.timeout_seconds, self._config.max_attempts)
        except AdvisorProviderError:
            raise
        except Exception as exc:
            raise _safe_error("gemini advisory request failed", type(exc).__name__) from None
        if status in (400, 401, 403, 404):
            raise AdvisorProviderError(f"gemini advisory provider returned HTTP {status}")
        if status == 429 or status >= 500:
            raise AdvisorProviderError(f"gemini advisory provider returned HTTP {status}")
        if status != 200:
            raise AdvisorProviderError(f"gemini advisory provider returned HTTP {status}")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise AdvisorProviderError("gemini advisory returned malformed data") from None
        text = _candidate_text(payload)
        if text is None:
            if isinstance(payload, dict) and "promptFeedback" in payload:
                raise AdvisorProviderError("gemini advisory prompt was blocked")
            raise AdvisorProviderError("gemini advisory returned no text")
        result = parse_response_text(text, citable, provider=PROVIDER, model=self._env.model,
                                     config=self._config)
        return result.to_dict()
