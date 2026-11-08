"""VIN domain: normalization, check digit, model year, WMI hints, comparison. Pure."""
from __future__ import annotations

import pytest

from app.domain.vin import (VinError, compare_vins, expected_check_digit, is_north_american,
                            is_well_formed, model_year_candidates, normalize_vin, summarize_vin,
                            wmi_info)

DOC_VIN = "1HGCM82633A004352"  # documentation example (Wikipedia VIN article)
JAPAN_VIN = "JH4TB2H26CC000000"  # well-formed, non-North-American: check digit not applicable


def test_documentation_vin_is_valid_with_known_check_digit():
    assert is_well_formed(DOC_VIN)
    assert expected_check_digit(DOC_VIN) == "3" == DOC_VIN[8]
    assert summarize_vin(DOC_VIN).check == "valid"


def test_tampered_check_digit_is_invalid():
    bad = DOC_VIN[:8] + ("5" if DOC_VIN[8] != "5" else "6") + DOC_VIN[9:]
    assert summarize_vin(bad).check == "invalid"


def test_non_north_american_is_never_invalid_on_check_digit():
    assert is_north_american(JAPAN_VIN) is False
    assert is_north_american(DOC_VIN) is True
    assert summarize_vin(JAPAN_VIN).check == "not_applicable"


def test_year_code_30_year_ambiguity_is_honest():
    assert model_year_candidates(DOC_VIN) == (2003, 2033)  # position 10 is '3'
    assert model_year_candidates("1HGCM8263TA004352") == (1996, 2026)  # 'T'
    assert model_year_candidates("1HGCM8263UA004352") == ()  # 'U' is not a year code


@pytest.mark.parametrize("raw", ["1HGCM82633A00435I", "1HGCM82633A00435O", "1HGCM82633A00435Q",
                                 "1HGCM82633A00435", "1HGCM82633A0043527", "", 17 * "1" + "I"])
def test_ioq_and_bad_lengths_rejected(raw):
    assert is_well_formed(raw if isinstance(raw, str) else "") is False
    with pytest.raises(VinError):
        summarize_vin(raw)  # type: ignore[arg-type]


def test_normalize_handles_case_space_and_hyphens():
    assert normalize_vin("  1hgcm8263-3a004352 ") == DOC_VIN
    with pytest.raises(VinError):
        normalize_vin(None)  # type: ignore[arg-type]


def test_wmi_hint_table_and_unknown_wmi():
    info = wmi_info(DOC_VIN)
    assert (info.manufacturer, info.region) == ("Honda", "United States")
    assert wmi_info(JAPAN_VIN).manufacturer == "Acura"
    assert wmi_info("XXX00000000000000") == wmi_info("XXX00000000000000")
    unknown = wmi_info("XXX00000000000000")
    assert unknown.manufacturer is None and unknown.region is None


def test_compare_outcomes():
    assert compare_vins(DOC_VIN, " " + DOC_VIN.lower() + " ") == "match"
    assert compare_vins(DOC_VIN, JAPAN_VIN) == "mismatch"
    assert compare_vins(None, DOC_VIN) == "not_checked"
    assert compare_vins(DOC_VIN, "") == "not_checked"
    assert compare_vins("short", DOC_VIN) == "not_checked"
    assert compare_vins("1HGCM82633A00435I", DOC_VIN) == "not_checked"
