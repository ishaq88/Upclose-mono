"""Missing inputs cost points; configs and stored results written before that rule keep their hash and replay unchanged.

All data is synthetic."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts.enums import Concept, Zone
from app.contracts.mechanical import FrozenDash
from app.contracts.scoring import ScoreResult
from app.domain import load_scoring_config, score_assessment
from app.domain.mechanical import MechanicalConfig, category_scores, load_mechanical_config, score_mechanical
from app.domain.narration import NarrationConfig, _headline, load_narration_config
from app.domain.scoring import visual_reason
from tests.test_mechanical import obd, pod
from tests.test_scoring import finding, inputs

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "tests/fixtures/legacy_config"
D = Decimal

# Content hashes of the configs frozen into reports finished before missing inputs were penalised.
OLD_SCORING_HASH = "c22099466cd5d87af644a773c7b8ac2b8d688641160abc17bbf51fc3f143ff63"
OLD_MECHANICAL_HASH = "de1565745d0b065d66e1c1d187213b403cbad4c8f612447d046d1f41dba5c76c"

NEW_SCORING = load_scoring_config(ROOT / "config/scoring.v1.json")
OLD_SCORING = load_scoring_config(LEGACY / "scoring.v1.json")
NEW_MECH = load_mechanical_config()
OLD_MECH = load_mechanical_config(LEGACY / "mechanical.v1.json")
SEEN = (Zone.DRIVER_SIDE, Zone.PASSENGER_SIDE, Zone.FRONT_REAR_BODY, Zone.TIRES, Zone.ENGINE_BAY)
DASH_OK = FrozenDash(state="assessed", config_hash="a" * 64)
DENT = (finding(100, zone=Zone.DRIVER_SIDE, concept=Concept.DENT, severity=4),)  # 4 x 2 = 8 points off


def test_old_configs_keep_their_content_hash_and_dump_without_the_new_fields() -> None:
    assert OLD_SCORING.content_hash == OLD_SCORING_HASH and OLD_MECH.content_hash == OLD_MECHANICAL_HASH
    assert "unassessed_zone_score" not in OLD_SCORING.model_dump(mode="json")
    assert "missing_inputs" not in OLD_MECH.model_dump(mode="json")
    assert OLD_SCORING.unassessed_zone_score is None and OLD_MECH.missing_inputs is None


def test_live_configs_turn_the_rule_on_with_new_hashes_and_versions() -> None:
    assert NEW_SCORING.unassessed_zone_score == D(50) and NEW_SCORING.content_hash != OLD_SCORING_HASH
    assert NEW_MECH.content_hash != OLD_MECHANICAL_HASH and NEW_MECH.missing_inputs is not None
    assert NEW_SCORING.version.endswith("-uncalibrated") and NEW_MECH.version.endswith("-unvalidated")
    assert NEW_SCORING.version != OLD_SCORING.version and NEW_MECH.version != OLD_MECH.version
    raw = json.loads((ROOT / "config/mechanical.v1.json").read_text())
    assert all(row["label"] == "unvalidated" for row in raw["missing_inputs"].values())
    assert [raw["missing_inputs"][k]["points"] for k in ("obd", "engine_pod", "dash")] == ["25", "20", "10"]


def test_worked_example_visual_mechanical_overall() -> None:
    result = score_assessment(inputs(DENT, SEEN), NEW_SCORING)
    assert result.zones[0].score == D(92)
    assert next(z for z in result.zones if z.zone == Zone.UNDERBODY).score is None  # the zone row keeps no score
    assert result.headline == D("89.125")
    mech = score_mechanical([obd(mil_on=True, dtcs=[{"code": "P0301", "status": "stored"}])], DASH_OK, NEW_MECH)
    assert mech.score == D(55)
    assert [row.code for row in mech.deductions] == ["trouble_codes", "mil_on", "missing_engine_pod"]
    cats = category_scores(result.headline, mech, NEW_MECH, visual_reason(result, NEW_SCORING))
    assert cats.overall.basis == "blend" and cats.overall.score == D("68.65")
    assert cats.visual.reason == "1 of 6 zones not seen; each counted as 50."
    assert "engine pod" in cats.mechanical.reason and "not damage" in cats.mechanical.reason


def test_nothing_at_all_is_visual_50_mechanical_45_overall_47() -> None:
    result = score_assessment(inputs((), ()), NEW_SCORING)
    assert result.headline == D(50) and result.unassessed_zone_score == D(50)
    mech = score_mechanical([], None, NEW_MECH)
    assert mech.score == D(45) and [row.code for row in mech.deductions] == ["missing_obd", "missing_engine_pod", "missing_dash"]
    cats = category_scores(result.headline, mech, NEW_MECH, visual_reason(result, NEW_SCORING))
    assert cats.overall.score == D(47) and cats.overall.basis == "blend"
    assert cats.visual.reason == "6 of 6 zones not seen; each counted as 50."


def test_missing_penalties_are_individual_floored_and_dash_respects_its_switch() -> None:
    assert score_mechanical([obd()], DASH_OK, NEW_MECH).score == D(80)  # pod missing only
    assert score_mechanical([obd(), pod("misfire.json")], DASH_OK, NEW_MECH).score == D(75)  # nothing missing
    clean = score_mechanical([obd(), pod("ok.json")], DASH_OK, NEW_MECH)
    assert clean.score == D(100) and clean.deductions == () and clean.reason is None
    assert score_mechanical([obd(), pod("ok.json")], None, NEW_MECH).score == D(90)
    unreadable = FrozenDash(state="not_readable", config_hash="a" * 64)
    assert score_mechanical([obd(), pod("ok.json")], unreadable, NEW_MECH).score == D(90)
    assert score_mechanical([obd(), pod("short.json")], DASH_OK, NEW_MECH).score == D(80)  # not_assessed verdict is missing
    off = MechanicalConfig.model_validate({**NEW_MECH.model_dump(), "use_dash_deduction": False})
    assert score_mechanical([obd(), pod("ok.json")], None, off).score == D(100)
    heavy = obd(mil_on=True, distance_since_codes_cleared_km=3,
                dtcs=[{"code": f"P03{i:02X}", "status": "stored"} for i in range(8)])
    assert score_mechanical([heavy, pod("misfire.json")], None, NEW_MECH).score == D(0)  # floor


def test_old_config_scores_exactly_as_before() -> None:
    few = score_assessment(inputs(DENT, (Zone.DRIVER_SIDE, Zone.PASSENGER_SIDE)), OLD_SCORING)
    assert few.headline is None and few.unassessed_zone_score is None  # under three zones: no headline
    assert "unassessed_zone_score" not in few.model_dump(mode="json")
    full = score_assessment(inputs(DENT, SEEN), OLD_SCORING)
    assert full.headline.quantize(D("0.001")) == D("98.154")  # (92 x 1.5 + 100 x 5) / 6.5, five assessed zones only
    old = score_mechanical([], None, OLD_MECH)
    assert old.score is None and old.deductions == ()
    assert score_mechanical([obd()], None, OLD_MECH).score == D(100)  # nothing missing is penalised
    cats = category_scores(None, old, OLD_MECH)
    assert cats.overall.basis == "none" and cats.overall.score is None
    assert category_scores(D(80), old, OLD_MECH).overall.basis == "visual_only"
    assert category_scores(D(80), score_mechanical([obd()], None, OLD_MECH), OLD_MECH).visual.reason is None


def test_stored_score_payload_from_before_the_change_still_validates_and_serialises_identically() -> None:
    stored = {"headline": 91.2, "zones": [{"zone": z, "state": "assessed", "score": 91.2, "reason": None}
                                          for z in ("driver_side", "passenger_side", "tires")],
              "config_hash": OLD_SCORING_HASH, "label": "Provisional visual-condition index"}
    result = ScoreResult.model_validate(stored)
    assert result.unassessed_zone_score is None
    assert result.model_dump(mode="json") == stored
    assert json.dumps(result.model_dump(mode="json")) == json.dumps(stored)


def test_headline_below_three_zones_needs_the_missing_input_mode_to_be_explicit() -> None:
    zones = [{"zone": "driver_side", "state": "assessed", "score": 90, "reason": None}]
    base = {"headline": 70, "config_hash": "a" * 64, "zones": zones}
    with pytest.raises(ValueError, match="three assessed zones"):
        ScoreResult.model_validate(base)
    with pytest.raises(ValueError, match="three assessed zones"):
        ScoreResult.model_validate({**base, "unassessed_zone_score": None})
    assert ScoreResult.model_validate({**base, "unassessed_zone_score": 50}).headline == D(70)


def test_narration_states_the_fill_and_old_results_keep_old_text() -> None:
    narration = load_narration_config(ROOT / "config/narration.v1.json")
    new = score_assessment(inputs(DENT, SEEN), NEW_SCORING)
    assert "a weighted mean over 6 zones; 1 not seen, each counted as 50.0" in _headline(new, narration)[0].text
    old = score_assessment(inputs(DENT, SEEN), OLD_SCORING)
    assert "a weighted mean over 5 assessed zones" in _headline(old, narration)[0].text


def test_narration_config_without_the_optional_template_still_loads_and_falls_back() -> None:
    raw = json.loads((ROOT / "config/narration.v1.json").read_text())
    raw["templates"] = [row for row in raw["templates"] if row["key"] != "paid_headline_unseen"]
    config = NarrationConfig.model_validate(raw)
    new = score_assessment(inputs((), SEEN), NEW_SCORING)
    assert "assessed zones" in _headline(new, config)[0].text  # falls back to the original sentence
