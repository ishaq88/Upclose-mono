"""Category scores (Visual, Mechanical, Overall) and the frozen dashboard-reading record."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from .base import ContractModel, Score

CategoryState = Literal["assessed", "not_checked"]
OverallBasis = Literal["blend", "visual_only", "mechanical_only", "none"]
CATEGORY_LABEL = "Provisional index, not a diagnosis"


class FrozenDashLit(ContractModel):
    indicator: Annotated[str, Field(max_length=32)]
    severity: Literal["urgent", "serious", "advisory"]
    confidence: Annotated[int, Field(strict=True, ge=1, le=5)]
    deduction: Decimal


class FrozenDash(ContractModel):
    """Dashboard-light reading frozen with the manifest. Mechanical scoring uses the deduction only when assessed."""
    state: Literal["assessed", "not_readable", "not_checked"]
    provenance: Literal["synthetic", "cached", "live"] = "synthetic"
    reason: Annotated[str, Field(max_length=120)] | None = None
    lit: tuple[FrozenDashLit, ...] = ()
    deduction: Decimal = Decimal(0)
    flags: tuple[Annotated[str, Field(max_length=32)], ...] = ()
    config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    crop_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None = None


class MechanicalDeduction(ContractModel):
    code: str
    label: str
    points: Decimal


class FreeDashCheck(ContractModel):
    checked: bool
    flag_count: Annotated[int, Field(strict=True, ge=0)]


class PaidDashCheck(ContractModel):
    assessment: FrozenDash | None = None
    evidence_available: Literal[False] = False
    note: str = "Uncalibrated warning-light reading, not a diagnosis. Crop withheld pending operator privacy review."


class MechanicalBreakdown(ContractModel):
    """Paid only: which evidence moved the Mechanical score. Never in the free view."""
    deductions: tuple[MechanicalDeduction, ...] = ()
    flags: tuple[str, ...] = ()


class CategoryScore(ContractModel):
    state: CategoryState
    score: Score | None = None
    reason: str | None = None


class OverallScore(ContractModel):
    state: CategoryState
    score: Score | None = None
    basis: OverallBasis
    note: str


class CategoryScores(ContractModel):
    """Public scores. A category without evidence is `not_checked` with no number, never a silent pass."""
    visual: CategoryScore
    mechanical: CategoryScore
    overall: OverallScore
    config_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    label: str = CATEGORY_LABEL
