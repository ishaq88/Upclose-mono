"""Dash assessment domain: vocabulary, states, deductions, OBD cross-check. Synthetic data."""
from decimal import Decimal

import pytest

from app.domain.dash_lights import (INDICATORS, DashReading, LitIndicator, ReadingRejected, assess,
                                    load_dash_config, parse_reading)

CONFIG = load_dash_config()


def reading(lit=(), readable=True, engine="tach_above_zero"):
    return DashReading(readable, engine, tuple(LitIndicator(n, c) for n, c in lit), "")


def test_config_loads_and_is_unvalidated():
    assert CONFIG.validated is False
    assert {i.id for i in CONFIG.indicators} == set(INDICATORS)
    assert len(CONFIG.content_hash) == 64


def test_bad_vocabulary_rejected():
    data = CONFIG.model_dump(mode="json")
    data["indicators"] = data["indicators"][:-1]
    with pytest.raises(ValueError):
        type(CONFIG).model_validate(data)


def test_not_checked_is_never_clean():
    result = assess(None, CONFIG)
    assert result.state == "not_checked" and not result.is_clean and result.deduction == 0


@pytest.mark.parametrize("rd,reason", [
    (reading(readable=False), "cluster_not_readable"),
    (reading(engine="off"), "engine_off"),
    (reading(engine="unknown"), "engine_running_not_evident"),
])
def test_not_readable_states_never_clean(rd, reason):
    result = assess(rd, CONFIG, obd_mil_on=True)
    assert (result.state, result.reason) == ("not_readable", reason)
    assert not result.is_clean and result.flags == () and result.lit == ()


def test_unreadable_reason_from_pipeline():
    result = assess(None, CONFIG, unreadable_reason="cluster_not_found")
    assert result.state == "not_readable" and not result.is_clean


def test_assessed_clean_only_when_nothing_lit():
    assert assess(reading(), CONFIG).is_clean
    assert not assess(reading([("tpms", 4)]), CONFIG).is_clean


def test_deductions_by_severity():
    result = assess(reading([("oil_pressure", 5), ("check_engine", 3), ("tpms", 2)]), CONFIG)
    assert result.state == "assessed"
    assert result.raw_deduction == Decimal(40) and result.deduction == Decimal(40)
    assert {f.indicator: f.deduction for f in result.lit} == {
        "oil_pressure": Decimal(20), "check_engine": Decimal(15), "tpms": Decimal(5)}


def test_deduction_cap():
    result = assess(reading([("oil_pressure", 5), ("coolant_temp", 5), ("brake_system", 5), ("abs", 5)]), CONFIG)
    assert result.raw_deduction == Decimal(75) and result.deduction == Decimal(40)


def test_duplicates_counted_once_with_best_confidence():
    result = assess(reading([("tpms", 2), ("tpms", 5)]), CONFIG)
    assert len(result.lit) == 1 and result.lit[0].confidence == 5 and result.deduction == Decimal(5)


def test_mil_mismatch():
    result = assess(reading([("check_engine", 4)]), CONFIG, obd_mil_on=False)
    assert result.flags == ("mil_mismatch",)


def test_mil_not_visible():
    result = assess(reading([("tpms", 4)]), CONFIG, obd_mil_on=True)
    assert result.flags == ("mil_not_visible",) and not result.is_clean


def test_consistent_or_absent_obd_has_no_flag():
    assert assess(reading([("check_engine", 4)]), CONFIG, obd_mil_on=True).flags == ()
    assert assess(reading(), CONFIG, obd_mil_on=False).flags == ()
    assert assess(reading(), CONFIG, obd_mil_on=None).flags == ()


def test_assessment_is_uncalibrated_and_carries_config_hash():
    result = assess(reading(), CONFIG)
    assert result.uncalibrated and result.config_hash == CONFIG.content_hash


GOOD = ('{"cluster_readable":true,"engine_running_evidence":"tach_above_zero",'
        '"lit_indicators":[{"indicator":"tpms","confidence":4}],"notes":"ok"}')


def test_parse_reading_accepts_valid():
    parsed = parse_reading(GOOD)
    assert parsed.lit_indicators == (LitIndicator("tpms", 4),)


@pytest.mark.parametrize("text", [
    "not json", "[]", "{}",
    GOOD.replace('"tpms"', '"fog_lamp"'),
    GOOD.replace('"confidence":4', '"confidence":9'),
    GOOD.replace('"confidence":4', '"confidence":true'),
    GOOD.replace('"cluster_readable":true', '"cluster_readable":"yes"'),
    GOOD.replace("tach_above_zero", "revving"),
    GOOD.replace('"notes":"ok"', '"notes":"' + "x" * 201 + '"'),
    GOOD.replace('"notes":"ok"', '"notes":"ok","extra":1'),
])
def test_parse_reading_rejects_invalid(text):
    with pytest.raises(ReadingRejected):
        parse_reading(text)
