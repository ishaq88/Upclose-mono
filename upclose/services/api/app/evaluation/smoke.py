"""Bounded SAM smoke: fixture by default, live only through BudgetService.

No default live calls. Live dispatch reuses `run_sam_request`, which reserves,
marks dispatched, sends once, and settles (or holds unknown) per request.
Receipts contain counts/usage/latency/provenance only: no keys, tokens, media
keys, signed URLs, raw media, or provider payloads.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from app.adapters.meta.parser import ProviderFailure
from app.adapters.meta.types import MediaRef, Segmenter
from app.jobs.queue import Lease
from app.pipeline.sam_dispatch import run_sam_request
from app.services.budgets import BudgetService

SCHEMA_VERSION = "upclose-sam-smoke-receipt-v1"
ABSOLUTE_MAX_REQUESTS = 3
ABSOLUTE_MAX_TIMEOUT_SECONDS = 120
LIVE_ENV_OPT_IN = "UPCLOSE_SMOKE_ALLOW_LIVE"

_FORBIDDEN_KEY_PARTS = (
    "api_key", "apikey", "authorization", "secret", "token", "password",
    "signed", "payload", "media_key", "frame_key", "raw_media", "base64",
)
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class SmokeError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SmokeConfig:
    mode: Literal["fixture", "live"] = "fixture"
    max_requests: int = 1
    timeout_seconds: int = 30
    prompt_id: str = "vehicle"

    def __post_init__(self) -> None:
        if self.mode not in {"fixture", "live"}:
            raise SmokeError("invalid_mode")
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= ABSOLUTE_MAX_REQUESTS:
            raise SmokeError("request_budget_exceeded")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= ABSOLUTE_MAX_TIMEOUT_SECONDS:
            raise SmokeError("time_budget_exceeded")
        if not self.prompt_id or len(self.prompt_id) > 128:
            raise SmokeError("invalid_prompt")


def _safe_code(code: object) -> str:
    return code if isinstance(code, str) and _SAFE_CODE.match(code) else "provider_failed"


def assert_receipt_sanitized(value: object, *, _key: str = "") -> None:
    lowered = _key.lower()
    if any(part in lowered for part in _FORBIDDEN_KEY_PARTS):
        raise SmokeError("receipt_sanitization_failed")
    if isinstance(value, dict):
        for key, item in value.items():
            assert_receipt_sanitized(item, _key=str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            assert_receipt_sanitized(item)
    elif isinstance(value, str):
        if "Bearer " in value or ";base64" in value or value.startswith("data:"):
            raise SmokeError("receipt_sanitization_failed")


def _receipt(config: SmokeConfig, *, outcome: str, attempted: int, succeeded: int,
             failed: int, observations: int, images: int | None, frames: int | None,
             usage_unknown: bool, latencies_ms: list[int], failures: list[str]) -> dict:
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "mode": config.mode,
        "provenance": "synthetic" if config.mode == "fixture" else "live",
        "outcome": outcome,
        "prompt_id": config.prompt_id,
        "counts": {"planned_requests": config.max_requests,
                   "requests_attempted": attempted, "requests_succeeded": succeeded,
                   "requests_failed": failed, "observations": observations},
        "usage": {"images_processed": images, "video_frames_processed": frames,
                  "usage_unknown": usage_unknown},
        "latency": {"per_request_ms": latencies_ms,
                    "total_ms": sum(latencies_ms),
                    "max_ms": max(latencies_ms) if latencies_ms else None},
        "budget": {"max_requests": config.max_requests,
                   "timeout_seconds": config.timeout_seconds,
                   "dispatch_path": ("BudgetService.reserve->mark_dispatched->segment->settle"
                                     if config.mode == "live" else "none")},
        "failures": failures,
        "gates": {"live_gate": "not_attempted" if config.mode == "fixture" else outcome,
                  "standard_access": "unverified"},
    }
    assert_receipt_sanitized(receipt)
    return receipt


def failed_receipt(config: SmokeConfig, code: str) -> dict:
    return _receipt(config, outcome="failed_closed", attempted=0, succeeded=0, failed=0,
                    observations=0, images=None, frames=None, usage_unknown=False,
                    latencies_ms=[], failures=[_safe_code(code)])


def run_smoke(config: SmokeConfig, *, budget_service: BudgetService | None = None,
              segmenter: Segmenter | None = None, lease: Lease | None = None,
              media: MediaRef | None = None, allow_live: bool = False,
              clock: Callable[[], float] = time.monotonic,
              request_id_factory: Callable[[], UUID] = uuid4) -> dict:
    if config.mode == "fixture":
        # Dry-run: never touch a segmenter, transport, storage, or budget ledger.
        return _receipt(config, outcome="dry_run", attempted=0, succeeded=0, failed=0,
                        observations=0, images=None, frames=None, usage_unknown=False,
                        latencies_ms=[], failures=[])
    if not allow_live:
        raise SmokeError("live_not_opted_in")
    if budget_service is None or lease is None or segmenter is None or media is None:
        raise SmokeError("budget_not_reserved")
    if not getattr(segmenter, "requires_budget", False):
        # A segmenter without the budget marker would bypass reservation in run_sam_request.
        raise SmokeError("budget_not_reserved")
    started = clock()
    attempted = succeeded = failed = observations = 0
    images: int | None = None
    frames: int | None = None
    usage_unknown = False
    latencies: list[int] = []
    failures: list[str] = []
    for _ in range(config.max_requests):
        if clock() - started >= config.timeout_seconds:
            failures.append("time_budget_exhausted")
            break
        attempted += 1
        before = clock()
        try:
            result = run_sam_request(lease, segmenter, media, request_id_factory(),
                                     config.prompt_id, budget_service)
        except ProviderFailure as error:
            failed += 1
            failures.append(_safe_code(error.code))
            break
        latencies.append(max(0, int((clock() - before) * 1000)))
        succeeded += 1
        observations += len(result.observations)
        usage = result.usage
        if usage.images_processed is None and usage.video_frames_processed is None:
            usage_unknown = True
        if usage.images_processed is not None:
            images = (images or 0) + usage.images_processed
        if usage.video_frames_processed is not None:
            frames = (frames or 0) + usage.video_frames_processed
    outcome = "succeeded" if succeeded and not failed and not failures else "failed"
    if not succeeded and not failures:
        outcome = "failed"
    return _receipt(config, outcome=outcome, attempted=attempted, succeeded=succeeded,
                    failed=failed, observations=observations, images=images, frames=frames,
                    usage_unknown=usage_unknown, latencies_ms=latencies, failures=failures)
