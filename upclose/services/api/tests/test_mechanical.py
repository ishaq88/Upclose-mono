"""Mechanical category score and the Overall blend (pure domain, no database). All data is synthetic."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.contracts.mechanical import FrozenDash, FrozenDashLit
from app.contracts.sources import ObdSnapshotData, RigTelemetryData, SourceRecord
from app.domain.mechanical import MechanicalConfig, category_scores, load_mechanical_config, score_mechanical
from app.domain.sources import load_sources_config, summarize_obd, summarize_rig
from tests.test_sources_domain import obd_data, rig_data

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_mechanical_config()
SOURCES = load_sources_config(ROOT / "config/sources.v1.json")
VERDICTS = ROOT / "tests/fixtures/verdicts"
D = Decimal


def record(kind: str, summary) -> SourceRecord:
    return SourceRecord(kind=kind, provenance="synthetic", device="bench",
                        captured_at=datetime(2026, 11, 7, tzinfo=timezone.utc), data_sha256="a" * 64, summary=summary)


def obd(**overrides) -> SourceRecord:
    base = {"mil_on": False, "dtcs": [], "readiness": {}, "distance_since_codes_cleared_km": 5000,
            "warmups_since_codes_cleared": 40, "vin": None, "pids": {}}
    return record("obd_snapshot", summarize_obd(ObdSnapshotData.model_validate({**base, **overrides}), "synthetic", SOURCES))


def pod(fixture: str | None) -> SourceRecord:
    data = rig_data(seconds=45.0)
    if fixture is not None:
        data["model_verdict"] = json.loads((VERDICTS / fixture).read_text())
    return record("rig_telemetry", summarize_rig(RigTelemetryData.model_validate(data), "synthetic", SOURCES))


def test_config_labels_every_weight_unvalidated_and_is_hashed() -> None:
    assert CONFIG.validated is False and CONFIG.blend.label == "unvalidated"
    assert all(row.label == "unvalidated" for row in CONFIG.weights.values())
    assert len(CONFIG.content_hash) == 64
    raw = json.loads((ROOT / "config/mechanical.v1.json").read_text())
    raw["weights"].pop("mil_on")
    with pytest.raises(ValidationError):
        MechanicalConfig.model_validate(raw)


def test_not_checked_without_obd_or_assessed_verdict() -> None:
    assert score_mechanical([], None, CONFIG).score is None
    assert score_mechanical([pod(None)], None, CONFIG).score is None  # a pod with no verdict is not an assessment
    assert score_mechanical([pod("short.json")], None, CONFIG).score is None  # not_assessed is never clean


def test_clean_obd_scores_full() -> None:
    result = score_mechanical([obd()], None, CONFIG)
    assert result.score == D(100) and result.deductions == () and result.flags == ()


def test_deductions_and_cap() -> None:
    result = score_mechanical([obd(mil_on=True, dtcs=[{"code": "P0301", "status": "stored"}, {"code": "P0420", "status": "pending"}])],
                              None, CONFIG)
    assert result.score == D(100) - 15 - 8 - 10
    many = obd(dtcs=[{"code": f"P03{i:02X}", "status": "stored"} for i in range(8)])
    assert score_mechanical([many], None, CONFIG).score == D(55)  # codes capped at -45


def test_cleared_codes_deduct_and_flag() -> None:
    result = score_mechanical([obd(distance_since_codes_cleared_km=3)], None, CONFIG)
    assert result.score == D(90) and result.flags == ("codes_recently_cleared",)


def test_anomaly_verdict_counts_and_clean_verdict_does_not() -> None:
    suspected = score_mechanical([pod("misfire.json")], None, CONFIG)
    assert suspected.score == D(75)
    clean = score_mechanical([pod("ok.json")], None, CONFIG)
    assert clean.score == D(100)


def test_dash_deduction_only_when_assessed_and_never_alone() -> None:
    lit = FrozenDash(state="assessed", lit=(FrozenDashLit(indicator="oil_pressure", severity="urgent", confidence=5, deduction=D(20)),),
                     deduction=D(20), flags=("mil_not_visible",), config_hash="b" * 64)
    assert score_mechanical([obd()], lit, CONFIG).score == D(80)
    assert score_mechanical([obd()], lit, CONFIG).flags == ("mil_not_visible",)
    unread = lit.model_copy(update={"state": "not_readable"})
    assert score_mechanical([obd()], unread, CONFIG).score == D(100)
    assert score_mechanical([], lit, CONFIG).score is None


def test_overall_blends_only_when_both_assessed() -> None:
    both = category_scores(D(80), score_mechanical([obd(mil_on=True)], None, CONFIG), CONFIG)
    assert both.overall.basis == "blend" and both.overall.score == D("0.4") * 80 + D("0.6") * 90
    visual_only = category_scores(D(80), score_mechanical([], None, CONFIG), CONFIG)
    assert visual_only.overall.score is None and visual_only.overall.basis == "visual_only"
    assert visual_only.overall.note == "Visual only: mechanical not checked."
    assert visual_only.mechanical.state == "not_checked" and visual_only.mechanical.score is None
    mech_only = category_scores(None, score_mechanical([obd()], None, CONFIG), CONFIG)
    assert mech_only.overall.score is None and mech_only.overall.basis == "mechanical_only"
    assert category_scores(None, score_mechanical([], None, CONFIG), CONFIG).overall.basis == "none"
