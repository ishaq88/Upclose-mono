"""Typed seam between live auxiliary providers (advisory, narration, voice guide) and the budget ledger.

A live call is *prepared* (an immutable request with a body digest and capped worst-case units),
then *granted* by the server (reservation committed and dispatch marked, so the charge is now
unknown), then sent exactly once. Usage is reported to a recorder even when the answer is invalid.

Nothing here knows about the database: the server supplies `DispatchGrant` and `UsageRecorder`
callables. A live adapter with no grant refuses before any transport. Fixture providers never
touch this seam.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

MAX_ADVISORY_INPUT_BYTES = 16 * 1024
MAX_ADVISORY_OUTPUT_TOKENS = 2048
MAX_SPEECH_CHARACTERS = 2500

Kind = Literal["advisory", "narration", "voice_guide"]
Outcome = Literal["billed", "billed_invalid", "unknown"]
T = TypeVar("T")


class DispatchError(RuntimeError):
    """Sanitized refusal or failure; never contains a key, body, text or VIN. `code` is machine-readable."""

    def __init__(self, message: str, *, code: str = "dispatch_failed", http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class PreparedRequestError(DispatchError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="prepared_request_invalid")


class DispatchDenied(DispatchError):
    def __init__(self, message: str = "dispatch grant denied; nothing was sent") -> None:
        super().__init__(message, code="grant_denied")


def utf16_units(text: str) -> int:
    """Conservative character count (UTF-16 code units; never fewer than provider counting)."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True)
class PreparedUnits:
    input_tokens: int = 0
    output_tokens: int = 0
    characters: int = 0


@dataclass(frozen=True)
class PreparedRequest:
    kind: Kind
    provider: str
    model: str
    url: str  # never carries a key
    body: bytes
    body_sha256: str
    units: PreparedUnits
    logical_key: str
    voice_id: str | None = None

    def verify(self) -> None:
        """Recheck digest, logical key and caps against the exact bytes about to be sent."""
        if not isinstance(self.body, bytes) or hashlib.sha256(self.body).hexdigest() != self.body_sha256:
            raise PreparedRequestError("prepared request body was modified")
        if self.logical_key != logical_key(self.kind, self.provider, self.model, self.voice_id, self.body_sha256):
            raise PreparedRequestError("prepared request identity was modified")
        u = self.units
        if min(u.input_tokens, u.output_tokens, u.characters) < 0:
            raise PreparedRequestError("prepared request has negative units")
        try:
            payload = json.loads(self.body)
        except ValueError:
            raise PreparedRequestError("prepared request body is not JSON") from None
        if self.kind == "advisory":
            if u.characters or len(self.body) > MAX_ADVISORY_INPUT_BYTES or u.input_tokens < len(self.body) \
                    or not 0 < u.output_tokens <= MAX_ADVISORY_OUTPUT_TOKENS:
                raise PreparedRequestError("advisory request exceeds its caps")
            if _sent_output_cap(payload) != u.output_tokens:
                raise PreparedRequestError("advisory output cap is not in the request body")
        elif self.kind in ("narration", "voice_guide"):
            text = payload.get("text") if isinstance(payload, dict) else None
            if u.input_tokens or u.output_tokens or not isinstance(text, str) or not text \
                    or u.characters < utf16_units(text) or u.characters > MAX_SPEECH_CHARACTERS:
                raise PreparedRequestError("speech request exceeds its caps")
        else:
            raise PreparedRequestError("unknown prepared request kind")


def _sent_output_cap(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    cap = payload.get("max_output_tokens")  # Meta Responses API
    if cap is None and isinstance(payload.get("generationConfig"), dict):
        cap = payload["generationConfig"].get("maxOutputTokens")  # Gemini generateContent
    return cap if type(cap) is int else None


def logical_key(kind: str, provider: str, model: str, voice_id: str | None, body_sha256: str) -> str:
    raw = json.dumps([kind, provider, model, voice_id or "", body_sha256], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prepare_request(*, kind: Kind, provider: str, model: str, url: str, body: bytes, units: PreparedUnits,
                    voice_id: str | None = None) -> PreparedRequest:
    digest = hashlib.sha256(body).hexdigest()
    prepared = PreparedRequest(kind=kind, provider=provider, model=model, url=url, body=body,
                               body_sha256=digest, units=units, voice_id=voice_id,
                               logical_key=logical_key(kind, provider, model, voice_id, digest))
    prepared.verify()
    return prepared


@dataclass(frozen=True)
class UsageEvidence:
    """Sanitized evidence of one dispatched call. No body, key, spoken text or VIN."""
    outcome: Outcome
    body_sha256: str
    latency_ms: int
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    characters: int | None = None
    provider_request_id: str | None = None
    http_status: int | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    characters: int | None = None
    provider_request_id: str | None = None


DispatchGrant = Callable[[PreparedRequest], bool]
UsageRecorder = Callable[[PreparedRequest, UsageEvidence], None]


def sanitized_id(value: object) -> str | None:
    return value[:255] if isinstance(value, str) and value and value.isprintable() else None


def execute_once(prepared: PreparedRequest, grant: DispatchGrant | None, record: UsageRecorder | None, *,
                 send: Callable[[], tuple[int, bytes]], usage_of: Callable[[int, bytes], Usage | None],
                 interpret: Callable[[int, bytes], tuple[T, bool]], fail_label: str,
                 error_type: type[DispatchError] = DispatchError) -> T:
    """Verify, obtain the grant, send exactly once, record usage exactly once.

    `send` performs the single transport attempt (raises on timeout/connection trouble).
    `usage_of` extracts billed usage from a response before any validation (None when absent).
    `interpret` validates the response and returns (result, valid); it may raise DispatchError.
    A transport exception, a non-2xx status, or missing usage is `unknown` (the reservation stays).
    """
    prepared.verify()
    if grant is None:
        raise DispatchDenied(f"{fail_label} needs a dispatch grant; nothing was sent")
    try:
        allowed = grant(prepared) is True
    except Exception:
        allowed = False
    if not allowed:
        raise DispatchDenied()
    started = time.monotonic()
    state: dict[str, object] = {"outcome": "unknown", "usage": None, "status": None, "code": "no_response"}

    def latency() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        try:
            status, raw = send()
        except DispatchError as exc:
            state["code"] = exc.code
            raise
        except Exception as exc:
            state["code"] = type(exc).__name__[:64]
            raise error_type(f"{fail_label} request failed ({type(exc).__name__})") from None
        state["status"] = status
        usage = None
        if 200 <= status < 300:
            try:
                usage = usage_of(status, raw)
            except Exception:
                usage = None
        state["usage"] = usage
        state["code"] = None if usage is not None else ("http_%d" % status if status >= 300 else "usage_missing")
        try:
            result, valid = interpret(status, raw)
        except DispatchError as exc:
            state["outcome"] = "billed_invalid" if usage is not None else "unknown"
            if usage is None:
                state["code"] = state["code"] or exc.code
            raise
        except Exception:
            state["outcome"] = "billed_invalid" if usage is not None else "unknown"
            raise error_type(f"{fail_label} returned unusable data") from None
        state["outcome"] = ("billed" if valid else "billed_invalid") if usage is not None else "unknown"
        return result
    finally:
        _report(prepared, record, state, latency())


def _report(prepared: PreparedRequest, record: UsageRecorder | None, state: dict, latency_ms: int) -> None:
    if record is None:
        return
    usage = state["usage"]
    evidence = UsageEvidence(
        outcome=state["outcome"], body_sha256=prepared.body_sha256, latency_ms=latency_ms,
        input_tokens=getattr(usage, "input_tokens", None), cached_input_tokens=getattr(usage, "cached_input_tokens", None),
        output_tokens=getattr(usage, "output_tokens", None), characters=getattr(usage, "characters", None),
        provider_request_id=getattr(usage, "provider_request_id", None), http_status=state["status"],
        failure_code=state["code"] if isinstance(state["code"], str) else None)
    try:
        record(prepared, evidence)
    except Exception:
        pass  # a recorder fault must not hide the result; the ledger stays unknown/reserved
