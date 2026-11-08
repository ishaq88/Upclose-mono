"""Buyer advisory block for the report. Never raises for a provider problem.

`build_advisory_block` calls the existing advisor adapter under a hard deadline and
returns a validated, immutable block. The block is either `available` (every item cites
evidence present in the input; see app.domain.advisory) or `not_available` with a reason.
It always carries the label "AI advisory, not a finding".
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import Field, ValidationError, model_validator

from app.adapters.advisor.config import AdvisorConfigError
from app.briefing.dispatch import DispatchError, DispatchGrant, UsageRecorder
from app.contracts.base import ContractModel
from app.contracts.sources import SourceSummary
from app.domain.advisory import AdvisoryError, citable_keys, plain_text, redact_vins

if TYPE_CHECKING:  # the adapter package imports contracts, which import this module
    from app.adapters.advisor import AdvisorAdapter

ADVISORY_LABEL = "AI advisory, not a finding"
DEFAULT_DEADLINE_SECONDS = 45.0
_VEHICLE_PART = re.compile(r"[^A-Za-z0-9 .-]")

Text = Annotated[str, Field(min_length=1, max_length=400)]


class BlockItem(ContractModel):
    text: Text
    cites: Annotated[tuple[Annotated[str, Field(min_length=1, max_length=120)], ...],
                     Field(min_length=1, max_length=8)]


class AdvisoryBlock(ContractModel):
    label: Literal["AI advisory, not a finding"] = ADVISORY_LABEL
    status: Literal["available", "not_available"]
    provider: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=128)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=32)]
    provenance: Literal["fixture", "live"]
    vehicle: Annotated[str, Field(max_length=80)] | None = None
    questions_for_seller: tuple[BlockItem, ...] = ()
    mechanic_checks: tuple[BlockItem, ...] = ()
    summary: Annotated[str, Field(max_length=1000)] | None = None
    reason: Annotated[str, Field(max_length=240)] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.status == "available":
            if not (self.questions_for_seller or self.mechanic_checks) or not self.summary:
                raise ValueError("an available advisory needs items and a summary")
            if self.reason is not None:
                raise ValueError("an available advisory has no reason")
        else:
            if not self.reason:
                raise ValueError("not_available needs a reason")
            if self.questions_for_seller or self.mechanic_checks or self.summary:
                raise ValueError("not_available carries no advisory content")
        if self.provenance == "fixture" and self.provider != "fixture":
            raise ValueError("fixture provenance requires the fixture provider")
        return self

    def to_json(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, raw: object) -> Self:
        """Re-validate a stored block before rendering or serving it."""
        return cls.model_validate(raw)


def _vehicle_label(vin_info: Mapping[str, object] | None) -> str | None:
    """Display label from year/make/model only; the VIN itself is never copied."""
    if not vin_info:
        return None
    parts = [_VEHICLE_PART.sub("", str(vin_info.get(key) or "")).strip() for key in ("year", "make", "model")]
    label = " ".join(part for part in parts if part)
    return redact_vins(label)[:80] or None


def _provenance(provider: str) -> Literal["fixture", "live"]:
    return "fixture" if provider == "fixture" else "live"


def _not_available(reason: str, provider: str, model: str, version: str, vehicle: str | None) -> AdvisoryBlock:
    return AdvisoryBlock(status="not_available", provider=provider or "unknown", model=model or "unknown",
                         prompt_version=version, provenance=_provenance(provider), vehicle=vehicle,
                         reason=redact_vins(reason)[:240])


def _from_result(result: object, vehicle: str | None, fallback_provider: str) -> AdvisoryBlock:
    if not isinstance(result, dict):
        return _not_available("advisor returned an unexpected result", fallback_provider, "unknown",
                              "advisory-v1", vehicle)
    provider = str(result.get("provider") or fallback_provider)
    model = str(result.get("model") or "unknown")
    version = str(result.get("prompt_version") or "advisory-v1")
    if result.get("status") == "not_available":
        return _not_available(plain_text(result.get("reason")) or "advisory not available",
                              provider, model, version, vehicle)
    try:
        return AdvisoryBlock(
            status="available", provider=provider, model=model, prompt_version=version,
            provenance=_provenance(provider), vehicle=vehicle,
            questions_for_seller=tuple(BlockItem(**row) for row in result["questions_for_seller"]),
            mechanic_checks=tuple(BlockItem(**row) for row in result["mechanic_checks"]),
            summary=result["summary"])
    except (ValidationError, KeyError, TypeError):
        return _not_available("advisor output failed validation", provider, model, version, vehicle)


def build_advisory_block(source_summaries: Sequence[SourceSummary], vin_info: Mapping[str, object] | None,
                         settings, *, environ: Mapping[str, str] | None = None,
                         adapter: AdvisorAdapter | None = None,
                         budget_ok: Callable[[], bool] | None = None,
                         grant: DispatchGrant | None = None, record: UsageRecorder | None = None,
                         deadline_seconds: float = DEFAULT_DEADLINE_SECONDS) -> AdvisoryBlock:
    """Build the advisory block. `adapter`/`environ` are injection points for tests and wiring.

    A non-fixture provider is only called with a dispatch `grant` (the server's reserve-then-mark-
    dispatched callback); without one, or with a denied one, nothing is sent and the block is
    `not_available`. The adapter makes one transport attempt bounded by `deadline_seconds` (no
    background thread) and reports sanitized usage to `record`, even for an invalid answer.
    `budget_ok` is an optional extra pre-check (False -> not_available).
    `settings.model_mode == "replay"` makes no provider call.
    """
    from app.adapters.advisor import AdvisorProviderError, get_advisor_adapter
    vehicle = _vehicle_label(vin_info)
    summaries = tuple(source_summaries)
    if not summaries or not citable_keys(summaries):
        return _not_available("no evidence available to advise on", "fixture", "none", "advisory-v1", vehicle)
    if getattr(settings, "model_mode", "fixture") == "replay" and adapter is None:
        return _not_available("advisory is not generated in replay mode", "fixture", "none",
                              "advisory-v1", vehicle)
    provider = "unknown"
    try:
        chosen = adapter or get_advisor_adapter(environ)
        provider = type(chosen).__name__.removesuffix("Advisor").lower()
        if provider != "fixture":
            if budget_ok is not None and not budget_ok():
                return _not_available("advisory budget is exhausted", provider, "unknown", "advisory-v1", vehicle)
            if grant is None:
                return _not_available("advisory is not funded; no dispatch grant", provider, "unknown",
                                      "advisory-v1", vehicle)
            result = chosen.advise(summaries, grant=grant, record=record,
                                   deadline_seconds=max(0.1, deadline_seconds))
        else:
            result = chosen.advise(summaries)
    except (AdvisorProviderError, DispatchError, AdvisorConfigError, AdvisoryError) as exc:
        return _not_available(str(exc) or "advisor unavailable", provider, "unknown", "advisory-v1", vehicle)
    except Exception as exc:  # a report must never break on the advisory
        return _not_available(f"advisor failed ({type(exc).__name__})", provider, "unknown",
                              "advisory-v1", vehicle)
    return _from_result(result, vehicle, provider)
