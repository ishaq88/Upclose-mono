"""Server-side accounting for the auxiliary live providers (advisory, narration, voice guide).

The briefing adapters own request preparation and bounded transport; they know nothing about sessions, jobs
or money. This module adapts `BudgetService` to the two callbacks they accept:

* `grant(prepared) -> bool`: atomic session+daily reservation at the approved worst-case price, then the
  durable `mark_dispatched` (charge now unknown). Only a True answer lets bytes leave the process.
* `record(prepared, evidence)`: persists what the call cost, or leaves the reservation in place.

Rules: one ledger row per logical request for the whole session (`logical_key` is a digest of kind, provider,
model, voice and the exact body), so a retried stage can never buy a second unknown request; a lost lease,
deleted session, expired deadline, missing receipt, unapproved model or exhausted budget all answer False.
Token providers settle at the documented rate from reported usage (even when the answer was invalid).
ElevenLabs publishes no per-response billing evidence, so its reservation stays `unknown`: never settled at an
estimate and never released.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from app.jobs.queue import Lease
from app.services.budgets import (BudgetError, BudgetService, ReservationRequest, UsageUnits, documented_prices,
                                  price_usage, price_version_for)

TOKEN_PROVIDERS = frozenset({"meta"})
KINDS = frozenset({"advisory", "narration", "voice_guide"})
MAX_ADVISORY_INPUT_TOKENS = 16384
MAX_ADVISORY_OUTPUT_TOKENS = 2048
MAX_SPEECH_CHARACTERS = 2500


def price_key(provider: str, model: str) -> str:
    """Meta keeps its legacy bare model id; every other provider is qualified so prices cannot collide."""
    return model if provider == "meta" else f"{provider}/{model}"


def prepared_units(prepared) -> UsageUnits:  # type: ignore[no-untyped-def]
    """Worst-case units from a prepared request, with defense-in-depth caps independent of the adapter."""
    units = prepared.units
    if prepared.kind not in KINDS:
        raise BudgetError("invalid_usage")
    if prepared.kind == "advisory":
        if units.characters or not 0 < units.input_tokens <= MAX_ADVISORY_INPUT_TOKENS \
                or not 0 < units.output_tokens <= MAX_ADVISORY_OUTPUT_TOKENS:
            raise BudgetError("invalid_usage")
        return UsageUnits(input_tokens=units.input_tokens, output_tokens=units.output_tokens)
    if units.input_tokens or units.output_tokens or not 0 < units.characters <= MAX_SPEECH_CHARACTERS:
        raise BudgetError("invalid_usage")
    return UsageUnits(characters=units.characters)


class AuxiliaryDispatcher:
    """One per stage attempt. `grant`/`record` are the callbacks handed to the briefing builders."""

    def __init__(self, budget: BudgetService | None, lease: Lease | None) -> None:
        self._budget, self._lease = budget, lease
        self._open: dict[UUID, object] = {}  # attempt id -> prepared request, granted but not yet recorded
        self.denied_reason: str | None = None
        self.granted = 0  # grants committed by this dispatcher (a provider request may have left the process)

    def remaining_seconds(self, margin: float = 15.0) -> float:
        """Time left on the lease deadline minus a safety margin; adapters bound their single transport by it."""
        if self._lease is None:
            return 0.0
        return max(0.0, (self._lease.deadline - datetime.now(timezone.utc)).total_seconds() - margin)

    @staticmethod
    def attempt_id(session_id: UUID, logical_key: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"budget:{session_id}:aux:{logical_key}")

    @property
    def available(self) -> bool:
        return self._budget is not None and self._lease is not None and self._lease.session_id is not None \
            and self._budget.config.model_mode == "live"

    def grant(self, prepared) -> bool:  # type: ignore[no-untyped-def]
        self.denied_reason = None
        if not self.available:
            self.denied_reason = "budget_not_connected"
            return False
        budget, lease = self._budget, self._lease
        assert budget is not None and lease is not None and lease.session_id is not None
        try:
            verify = getattr(prepared, "verify", None)
            if callable(verify):
                verify()  # the body about to be sent must still be the body that was priced
            units = prepared_units(prepared)
            key = price_key(prepared.provider, prepared.model)
            price_usage(key, units, documented_prices())  # unapproved model/price fails before any ledger write
            attempt = self.attempt_id(lease.session_id, prepared.logical_key)
            if budget.has_attempt(attempt):
                self.denied_reason = "already_attempted"  # unknown or settled: never repeat a request
                return False
            budget.reserve(ReservationRequest(lease.session_id, lease.job_id, attempt, lease.token, lease.deadline,
                                              key, price_version_for(key), units))
            if not budget.mark_dispatched(attempt, lease.token):
                self.denied_reason = "not_dispatched"
                return False
        except BudgetError as error:
            self.denied_reason = error.code
            logging.warning("Auxiliary %s call not granted code=%s", getattr(prepared, "kind", "unknown"), error.code)
            return False
        except Exception:  # a denied grant must never become a transport
            self.denied_reason = "grant_failed"
            logging.warning("Auxiliary call not granted (grant_failed)")
            return False
        self._open[attempt] = prepared
        self.granted += 1
        return True

    def record(self, prepared, evidence) -> None:  # type: ignore[no-untyped-def]
        if self._lease is None or self._lease.session_id is None or self._budget is None:
            return
        attempt = self.attempt_id(self._lease.session_id, prepared.logical_key)
        if self._open.pop(attempt, None) is None:
            return  # never granted here: nothing to account
        try:
            self._settle(attempt, prepared, evidence)
        except Exception:
            logging.warning("Auxiliary usage recording failed; the ledger keeps the unknown charge")

    def _settle(self, attempt: UUID, prepared, evidence) -> None:  # type: ignore[no-untyped-def]
        budget = self._budget
        assert budget is not None
        outcome = getattr(evidence, "outcome", "unknown")
        if outcome in {"billed", "billed_invalid"} and prepared.provider in TOKEN_PROVIDERS:
            used, cached, produced = evidence.input_tokens, evidence.cached_input_tokens or 0, evidence.output_tokens
            if used is not None and produced is not None:
                try:
                    actual = UsageUnits(input_tokens=used, cached_input_tokens=cached, output_tokens=produced)
                    key = price_key(prepared.provider, prepared.model)
                    request_id = evidence.provider_request_id
                    latency = evidence.latency_ms
                    budget.settle(attempt, actual_units=actual,
                                  actual_micro_usd=price_usage(key, actual, documented_prices()),
                                  outcome="succeeded", evidence_code=f"{prepared.provider}-usage-reported",
                                  provider_request_id=request_id if isinstance(request_id, str) and 0 < len(request_id) <= 255 else None,
                                  latency_ms=latency if type(latency) is int and latency >= 0 else None)
                    return
                except BudgetError:
                    pass  # unpriced or inconsistent usage stays reserved
        code = "billing_unverified" if outcome in {"billed", "billed_invalid"} else getattr(evidence, "failure_code", None)
        budget.mark_unknown(attempt, failure_code=code, http_status=getattr(evidence, "http_status", None))

    def close(self) -> None:
        """Any call granted but never recorded (adapter crash, timeout) keeps its unknown charge."""
        budget = self._budget
        for attempt in list(self._open):
            self._open.pop(attempt)
            try:
                if budget is not None:
                    budget.mark_unknown(attempt, failure_code="unrecorded")
            except Exception:
                logging.warning("Auxiliary close could not mark an unknown charge")
