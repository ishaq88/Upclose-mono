"""Pure advisory domain: prompt building, citable keys, strict response validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts.sources import (DtcEntry, IrregularWindow, ObdSummary, RigTelemetrySummary,
                                   TemperatureSummary)
from app.domain.advisory import (Advisory, NotAvailable, PROMPT_VERSION, AdvisoryConfig, AdvisoryError,
                                 build_prompt, citable_keys, load_advisory_config, parse_response_text,
                                 plain_text, redact_vins, validate_response)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_advisory_config(ROOT / "config/advisory.v1.json")
HASH = "0" * 64
VIN = "SYNTHETC00000UPCA"


def rig(**overrides):
    base = {"config_hash": HASH, "provenance": "synthetic", "sample_count": 4000,
            "duration_s": 10.0, "state": "assessed",
            "dominant_hz": 25.0, "rpm_estimate": 750, "cylinders": 4,
            "roughness": 0.31,
            "irregular_windows": (IrregularWindow(t_ms=2000, rms_mg=120.0),),
            "temperature": TemperatureSummary(start_c=21.4, rise_c=5.6, engine_warm_at_start=False),
            "flags": ("irregular_vibration", "rough_vibration"), "flag_count": 2}
    base.update(overrides)
    return RigTelemetrySummary(**base)


def obd(**overrides):
    base = {"config_hash": HASH, "provenance": "synthetic", "vin": VIN, "mil_on": True,
            "dtcs": (DtcEntry(code="P0301", status="stored"),),
            "incomplete_monitors": ("catalyst", "evap"), "codes_recently_cleared": True,
            "cold_start": True, "coolant_c": 24.0, "distance_since_codes_cleared_km": 12,
            "warmups_since_codes_cleared": 1,
            "flags": ("mil_on", "trouble_codes", "codes_recently_cleared"), "flag_count": 3}
    base.update(overrides)
    return ObdSummary(**base)


SUMMARIES = (rig(), obd())
CITES = citable_keys(SUMMARIES)


def payload(**overrides):
    base = {"questions_for_seller": [
                {"text": "Ask about fault code P0301.", "cites": ["obd_snapshot:dtc:P0301"]},
                {"text": "Ask about the vibration events.", "cites": ["rig_telemetry:irregular_windows"]}],
            "mechanic_checks": [
                {"text": "Recheck the monitors.", "cites": ["obd_snapshot:incomplete_monitor:catalyst"]}],
            "summary": "Simulated evidence with several flags; ask and check before deciding."}
    base.update(overrides)
    return base


def test_config_version_and_limits():
    assert CONFIG.prompt_version == PROMPT_VERSION == "advisory-v1"
    assert (CONFIG.max_items_per_list, CONFIG.max_item_chars, CONFIG.max_summary_chars) == (5, 240, 600)
    assert CONFIG.timeout_seconds <= 30.0 and CONFIG.max_attempts == 2
    with pytest.raises(AdvisoryError):
        load_advisory_config(ROOT / "config/sources.v1.json")


def test_citable_keys_cover_contract_examples():
    for key in ("obd_snapshot:dtc:P0301", "obd_snapshot:codes_recently_cleared", "obd_snapshot:mil_on",
                "rig_telemetry:irregular_windows", "rig_telemetry:engine_warm_at_start",
                "rig_telemetry:rpm_estimate"):
        assert key in CITES


def test_citable_keys_absent_when_fact_missing():
    clean = citable_keys((obd(mil_on=False, dtcs=(), incomplete_monitors=(),
                              codes_recently_cleared=None, cold_start=None, coolant_c=None,
                              flags=(), flag_count=0),))
    assert "obd_snapshot:mil_on" not in clean and "obd_snapshot:codes_recently_cleared" not in clean
    assert not any(key.startswith("obd_snapshot:dtc:") for key in clean)
    assessed_none = rig(dominant_hz=None, rpm_estimate=None, roughness=None, irregular_windows=(),
                        temperature=None, flags=(), flag_count=0)
    assert "rig_telemetry:rpm_estimate" not in citable_keys((assessed_none,))
    assert "rig_telemetry:not_assessed" in citable_keys((rig(state="not_assessed"),))


def test_valid_response_accepted_with_wire_shape():
    result = validate_response(payload(), CITES, provider="fixture", model="m", config=CONFIG)
    assert isinstance(result, Advisory)
    wire = result.to_dict()
    assert set(wire) == {"questions_for_seller", "mechanic_checks", "summary",
                         "provider", "model", "prompt_version"}
    assert wire["prompt_version"] == "advisory-v1" and wire["provider"] == "fixture"
    assert [item["cites"] for item in wire["questions_for_seller"]] == [
        ["obd_snapshot:dtc:P0301"], ["rig_telemetry:irregular_windows"]]


def test_uncited_or_foreign_items_dropped_valid_kept():
    result = validate_response(payload(
        questions_for_seller=[
            {"text": "No cites at all.", "cites": []},
            {"text": "Foreign key.", "cites": ["engine_audio:boom"]},
            {"text": "Absent fact.", "cites": ["obd_snapshot:dtc:P9999"]},
            {"text": "Good one.", "cites": ["obd_snapshot:mil_on"]}],
        mechanic_checks=[]), CITES, provider="fixture", model="m", config=CONFIG)
    assert isinstance(result, Advisory)
    assert [item.text for item in result.questions_for_seller] == ["Good one."]


def test_nothing_valid_is_not_available_never_all_clear():
    for bad in (payload(questions_for_seller=[], mechanic_checks=[]),
                payload(questions_for_seller=[{"text": "x", "cites": ["nope"]}], mechanic_checks=[]),
                "not a dict", {"questions_for_seller": [], "mechanic_checks": "nope"}):
        result = validate_response(bad, CITES, provider="fixture", model="m", config=CONFIG)
        assert isinstance(result, NotAvailable)
        assert result.reason and "all clear" not in result.reason.lower()
        assert result.to_dict()["status"] == "not_available"


def test_malformed_json_is_not_available():
    result = parse_response_text("{oops", CITES, provider="meta", model="m", config=CONFIG)
    assert isinstance(result, NotAvailable) and result.provider == "meta"


def test_code_fence_tolerated_but_content_validated():
    raw = "```json\n" + json.dumps(payload()) + "\n```"
    result = parse_response_text(raw, CITES, provider="gemini", model="m", config=CONFIG)
    assert isinstance(result, Advisory)


def test_overlength_trimmed_and_lists_capped():
    many = [{"text": "q", "cites": ["obd_snapshot:mil_on"]} for _ in range(7)]
    result = validate_response(payload(questions_for_seller=many,
                                       summary="s " * 400,
                                       mechanic_checks=[{"text": "t " * 200,
                                                         "cites": ["rig_telemetry:rpm_estimate"]}]),
                               CITES, provider="fixture", model="m", config=CONFIG)
    assert isinstance(result, Advisory)
    assert len(result.questions_for_seller) == 5
    assert len(result.summary) <= 600 and len(result.mechanic_checks[0].text) <= 240


def test_markup_stripped_and_empty_text_dropped():
    assert plain_text("**bold** <b>x</b> [y](http://example.com/x) `c`") == "bold x y c"
    result = validate_response(payload(
        questions_for_seller=[{"text": "   <br>  ", "cites": ["obd_snapshot:mil_on"]}],
        mechanic_checks=[{"text": "Keep.", "cites": ["obd_snapshot:mil_on"]}]),
        CITES, provider="fixture", model="m", config=CONFIG)
    assert isinstance(result, Advisory) and result.questions_for_seller == ()


def test_all_clear_summary_replaced_not_passed_through():
    result = validate_response(payload(summary="Everything is all clear, no problems!"),
                               CITES, provider="fixture", model="m", config=CONFIG)
    assert isinstance(result, Advisory)
    assert "all clear" not in result.summary.lower()


def test_prompt_states_caveats_and_carries_no_vin():
    prompt = build_prompt(SUMMARIES, CONFIG)
    lowered = prompt.lower()
    assert "uncalibrated" in lowered and "simulat" in lowered
    assert "question" in lowered and "mechanic" in lowered
    assert VIN not in prompt and "SYNTHETC" not in prompt
    assert "vin_present=true" in prompt


def test_redact_vins():
    assert VIN not in redact_vins(f"vin {VIN} here")
    assert "MODEL_API_KEY" not in redact_vins("nothing secret here")


def test_empty_input_has_no_citable_keys():
    assert citable_keys(()) == frozenset()
