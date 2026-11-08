"""Mechanical category score and the Overall blend. Pure: no network, ORM or framework imports.

Mechanical starts at the configured start value and loses points for frozen mechanical evidence. It exists
only when there is an assessed OBD snapshot or an assessed pod verdict; a `not_assessed` verdict is never
counted as clean. Every weight is an unvalidated design default (config/mechanical.v1.json).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Sequence

from pydantic import Field, model_validator

from app.contracts.base import ContractModel
from app.contracts.mechanical import (CategoryScore, CategoryScores, FrozenDash, MechanicalBreakdown,
                                      MechanicalDeduction, OverallScore)
from app.contracts.sources import ObdSummary, RigTelemetrySummary, SourceRecord
from app.domain.config import HashedConfig

Points = Annotated[Decimal, Field(ge=0, le=100)]
WEIGHT_NAMES = ("anomaly_suspected", "dtc_stored_or_permanent", "dtc_pending", "dtc_cap", "mil_on",
                "codes_recently_cleared", "dash_mil_mismatch", "dash_mil_not_visible")


class Weight(ContractModel):
    points: Points
    label: Literal["unvalidated"]


class Blend(ContractModel):
    visual: Annotated[Decimal, Field(gt=0)]
    mechanical: Annotated[Decimal, Field(gt=0)]
    label: Literal["unvalidated"]


class MechanicalConfig(HashedConfig):
    note: str
    validated: Literal[False]
    start: Points
    weights: dict[str, Weight]
    use_dash_deduction: bool
    blend: Blend

    @model_validator(mode="after")
    def complete(self) -> "MechanicalConfig":
        if set(self.weights) != set(WEIGHT_NAMES):
            raise ValueError("mechanical weights must match the vocabulary exactly")
        return self

    def points(self, name: str) -> Decimal:
        return self.weights[name].points


def load_mechanical_config(path: str | Path | None = None) -> MechanicalConfig:
    target = Path(path) if path else Path(__file__).resolve().parents[2] / "config" / "mechanical.v1.json"
    return MechanicalConfig.model_validate_json(target.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class MechanicalResult:
    score: Decimal | None
    reason: str | None
    deductions: tuple[MechanicalDeduction, ...] = ()
    flags: tuple[str, ...] = ()

    @property
    def breakdown(self) -> MechanicalBreakdown:
        return MechanicalBreakdown(deductions=self.deductions, flags=self.flags)


def _assessed(sources: Sequence[SourceRecord], kind: str) -> SourceRecord | None:
    return next((row for row in sources if row.kind == kind and row.summary.state == "assessed"), None)


def score_mechanical(sources: Sequence[SourceRecord], dash: FrozenDash | None, config: MechanicalConfig) -> MechanicalResult:
    obd = _assessed(sources, "obd_snapshot")
    rig = _assessed(sources, "rig_telemetry")
    verdict = None
    if rig is not None and isinstance(rig.summary, RigTelemetrySummary) and rig.summary.model_verdict is not None:
        if rig.summary.model_verdict.verdict != "not_assessed":
            verdict = rig.summary.model_verdict
    if obd is None and verdict is None:
        return MechanicalResult(None, "Needs an OBD snapshot or an assessed engine-pod verdict.")
    deductions: list[MechanicalDeduction] = []
    flags: list[str] = []
    if verdict is not None and verdict.verdict == "anomaly_suspected":
        deductions.append(MechanicalDeduction(code="anomaly_suspected", label="Engine anomaly suspected",
                                              points=config.points("anomaly_suspected")))
    if obd is not None and isinstance(obd.summary, ObdSummary):
        summary = obd.summary
        solid = sum(row.status in ("stored", "permanent") for row in summary.dtcs)
        pending = sum(row.status == "pending" for row in summary.dtcs)
        code_points = min(solid * config.points("dtc_stored_or_permanent") + pending * config.points("dtc_pending"),
                          config.points("dtc_cap"))
        if code_points > 0:
            deductions.append(MechanicalDeduction(code="trouble_codes", label="Trouble codes reported", points=code_points))
        if summary.mil_on:
            deductions.append(MechanicalDeduction(code="mil_on", label="Check-engine light on", points=config.points("mil_on")))
        if summary.codes_recently_cleared:
            deductions.append(MechanicalDeduction(code="codes_recently_cleared", label="Codes may have been recently cleared",
                                                  points=config.points("codes_recently_cleared")))
            flags.append("codes_recently_cleared")
    if dash is not None and dash.state == "assessed" and config.use_dash_deduction:
        if dash.deduction > 0:
            deductions.append(MechanicalDeduction(code="dash_lights", label="Warning lights lit on the dashboard", points=dash.deduction))
        for flag in dash.flags:
            flags.append(flag)
            extra = config.weights.get(f"dash_{flag}")
            if extra is not None and extra.points > 0:
                deductions.append(MechanicalDeduction(code=flag, label=flag.replace("_", " ").capitalize(), points=extra.points))
    total = sum((row.points for row in deductions), Decimal(0))
    return MechanicalResult(max(config.start - total, Decimal(0)), None, tuple(deductions), tuple(flags))


def category_scores(visual: Decimal | None, mechanical: MechanicalResult, config: MechanicalConfig) -> CategoryScores:
    """Overall is a blend only when both categories are assessed; otherwise it says which one is missing."""
    mech = mechanical.score
    visual_card = (CategoryScore(state="assessed", score=visual) if visual is not None else
                   CategoryScore(state="not_checked", reason="Not enough visual footage was assessed."))
    mech_card = (CategoryScore(state="assessed", score=mech) if mech is not None else
                 CategoryScore(state="not_checked", reason=mechanical.reason))
    if visual is not None and mech is not None:
        blend = config.blend
        value = (visual * blend.visual + mech * blend.mechanical) / (blend.visual + blend.mechanical)
        overall = OverallScore(state="assessed", score=value, basis="blend", note="Blend of the Visual and Mechanical scores.")
    elif visual is not None:
        overall = OverallScore(state="not_checked", basis="visual_only", note="Visual only: mechanical not checked.")
    elif mech is not None:
        overall = OverallScore(state="not_checked", basis="mechanical_only", note="Mechanical only: visual not assessed.")
    else:
        overall = OverallScore(state="not_checked", basis="none", note="Neither category could be assessed.")
    return CategoryScores(visual=visual_card, mechanical=mech_card, overall=overall, config_hash=config.content_hash)
