"""Dashboard warning-light assessment. Pure: no network, ORM or framework imports.

Unreadable or unchecked is never a clean result. All severities and deductions are
uncalibrated design defaults (see config/dash_lights.v1.json).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.contracts.base import ContractModel
from app.domain.config import HashedConfig

INDICATORS = ("check_engine", "oil_pressure", "battery_charging", "coolant_temp", "brake_system",
              "abs", "airbag", "tpms", "traction_stability", "other")
Severity = Literal["urgent", "serious", "advisory"]
EngineEvidence = Literal["tach_above_zero", "unknown", "off"]
State = Literal["assessed", "not_readable", "not_checked"]
MAX_NOTES = 200
MAX_LIT = 16


class IndicatorConfig(ContractModel):
    id: Literal["check_engine", "oil_pressure", "battery_charging", "coolant_temp", "brake_system",
                "abs", "airbag", "tpms", "traction_stability", "other"]
    severity: Severity


class FrameConfig(ContractModel):
    min_frames: Annotated[int, Field(strict=True, ge=1)]
    min_cluster_area_fraction: Annotated[Decimal, Field(gt=0, le=1)]
    area_target_fraction: Annotated[Decimal, Field(gt=0, le=1)]
    sharpness_reference: Annotated[Decimal, Field(gt=0)]
    min_sharpness: Annotated[Decimal, Field(ge=0)]
    area_weight: Annotated[Decimal, Field(ge=0)]
    sharpness_weight: Annotated[Decimal, Field(ge=0)]
    crop_margin_fraction: Annotated[Decimal, Field(ge=0, le=1)]


class SamPromptConfig(ContractModel):
    prompt_id: Annotated[str, Field(min_length=1, max_length=128)]
    text: Annotated[str, Field(min_length=1, max_length=200)]


class DashConfig(HashedConfig):
    note: str
    validated: Literal[False]
    indicators: tuple[IndicatorConfig, ...]
    deductions: dict[Severity, Annotated[Decimal, Field(ge=0)]]
    deduction_cap: Annotated[Decimal, Field(gt=0)]
    frames: FrameConfig
    sam_prompt: SamPromptConfig

    @model_validator(mode="after")
    def complete(self) -> "DashConfig":
        if tuple(sorted(i.id for i in self.indicators)) != tuple(sorted(INDICATORS)):
            raise ValueError("indicator vocabulary must match exactly")
        if set(self.deductions) != {"urgent", "serious", "advisory"}:
            raise ValueError("a deduction is required for every severity")
        return self


def load_dash_config(path: str | Path | None = None) -> DashConfig:
    target = Path(path) if path else Path(__file__).resolve().parents[2] / "config" / "dash_lights.v1.json"
    return DashConfig.model_validate_json(target.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class LitIndicator:
    indicator: str
    confidence: int  # 1-5, model-stated and uncalibrated


@dataclass(frozen=True, slots=True)
class DashReading:
    cluster_readable: bool
    engine_running_evidence: EngineEvidence
    lit_indicators: tuple[LitIndicator, ...]
    notes: str


class ReadingRejected(ValueError):
    """The model response failed validation; carries a stable code only, never response text."""


READING_KEYS = frozenset({"cluster_readable", "engine_running_evidence", "lit_indicators", "notes"})


def reading_schema() -> dict[str, object]:
    """Strict JSON schema for the reader response (also used for provider structured output)."""
    return {"type": "object", "additionalProperties": False,
            "required": sorted(READING_KEYS),
            "properties": {
                "cluster_readable": {"type": "boolean"},
                "engine_running_evidence": {"type": "string", "enum": ["tach_above_zero", "unknown", "off"]},
                "lit_indicators": {"type": "array", "maxItems": MAX_LIT, "items": {
                    "type": "object", "additionalProperties": False, "required": ["indicator", "confidence"],
                    "properties": {"indicator": {"type": "string", "enum": list(INDICATORS)},
                                   "confidence": {"type": "integer", "minimum": 1, "maximum": 5}}}},
                "notes": {"type": "string", "maxLength": MAX_NOTES}}}


def parse_reading(text: str) -> DashReading:
    """Validate a model response before anything is written. Raises ReadingRejected."""
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        raise ReadingRejected("not_json") from None
    if not isinstance(raw, dict) or set(raw) != READING_KEYS:
        raise ReadingRejected("wrong_keys")
    if type(raw["cluster_readable"]) is not bool:
        raise ReadingRejected("cluster_readable")
    if raw["engine_running_evidence"] not in ("tach_above_zero", "unknown", "off"):
        raise ReadingRejected("engine_running_evidence")
    notes = raw["notes"]
    if not isinstance(notes, str) or len(notes) > MAX_NOTES:
        raise ReadingRejected("notes")
    items = raw["lit_indicators"]
    if not isinstance(items, list) or len(items) > MAX_LIT:
        raise ReadingRejected("lit_indicators")
    lit: list[LitIndicator] = []
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"indicator", "confidence"}
                or item["indicator"] not in INDICATORS
                or type(item["confidence"]) is not int or not 1 <= item["confidence"] <= 5):
            raise ReadingRejected("lit_indicator_item")
        lit.append(LitIndicator(item["indicator"], item["confidence"]))
    return DashReading(raw["cluster_readable"], raw["engine_running_evidence"], tuple(lit), notes)


@dataclass(frozen=True, slots=True)
class LitFinding:
    indicator: str
    severity: str
    confidence: int
    deduction: Decimal


@dataclass(frozen=True, slots=True)
class DashAssessment:
    state: State
    reason: str | None
    lit: tuple[LitFinding, ...] = ()
    raw_deduction: Decimal = Decimal(0)
    deduction: Decimal = Decimal(0)
    flags: tuple[str, ...] = ()
    config_version: str = ""
    config_hash: str = ""
    uncalibrated: bool = field(default=True)

    @property
    def is_clean(self) -> bool:
        """True only for an assessed reading with no lit indicator and no flag."""
        return self.state == "assessed" and not self.lit and not self.flags


def assess(reading: DashReading | None, config: DashConfig, *, obd_mil_on: bool | None = None,
           unreadable_reason: str | None = None) -> DashAssessment:
    """reading None with no reason is not_checked; with a reason it is not_readable."""
    meta = {"config_version": config.version, "config_hash": config.content_hash}
    if reading is None:
        if unreadable_reason:
            return DashAssessment("not_readable", unreadable_reason, **meta)
        return DashAssessment("not_checked", None, **meta)
    if not reading.cluster_readable:
        return DashAssessment("not_readable", "cluster_not_readable", **meta)
    if reading.engine_running_evidence == "off":
        return DashAssessment("not_readable", "engine_off", **meta)
    if reading.engine_running_evidence != "tach_above_zero":
        return DashAssessment("not_readable", "engine_running_not_evident", **meta)
    severity = {i.id: i.severity for i in config.indicators}
    best: dict[str, int] = {}
    for item in reading.lit_indicators:
        best[item.indicator] = max(best.get(item.indicator, 0), item.confidence)
    lit = tuple(LitFinding(name, severity[name], conf, config.deductions[severity[name]])
                for name, conf in sorted(best.items()))
    raw = sum((f.deduction for f in lit), Decimal(0))
    flags: list[str] = []
    check_lit = "check_engine" in best
    if obd_mil_on is False and check_lit:
        flags.append("mil_mismatch")
    if obd_mil_on is True and not check_lit:
        flags.append("mil_not_visible")
    return DashAssessment("assessed", None, lit, raw, min(raw, config.deduction_cap), tuple(flags), **meta)
