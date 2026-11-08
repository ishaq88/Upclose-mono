"""VIN adapters: fixture and vPIC. Static fixtures and mocked transports only; no live calls."""
from __future__ import annotations

import json

import pytest

from app.adapters.vin import (TIMEOUT_SECONDS, VinConfigError, VinFetchError, get_vin_adapter)
from app.adapters.vin.config import load_env
from app.adapters.vin.fixture import FixtureVin
from app.adapters.vin.nhtsa import NhtsaVin

DOC_VIN = "1HGCM82633A004352"  # documentation example (Wikipedia VIN article)


def vpic_payload(**row_overrides):
    row = {"VIN": DOC_VIN, "Make": "HONDA", "Manufacturer": "HONDA", "Model": "Accord",
           "ModelYear": "2003", "BodyClass": "Coupe", "EngineCylinders": "6",
           "DisplacementCC": "2998.832712", "DisplacementCI": "", "DisplacementL": "2.998832712",
           "FuelTypePrimary": "Gasoline", "ErrorCode": "0",
           "ErrorText": "0 - VIN decoded clean. Check Digit (9th position) is correct"}
    row.update(row_overrides)
    return {"Count": 1, "Message": "Results returned successfully",
            "SearchCriteria": f"VIN:{DOC_VIN}", "Results": [row]}


def test_default_is_fixture_and_unknown_rejected():
    assert isinstance(get_vin_adapter({}), FixtureVin)
    assert isinstance(get_vin_adapter({"VIN_PROVIDER": "nhtsa"}), NhtsaVin)
    with pytest.raises(VinConfigError):
        load_env({"VIN_PROVIDER": "other"})


def test_fixture_returns_local_make_only():
    outcome = FixtureVin().decode(" " + DOC_VIN.lower() + " ")
    assert outcome.reason is None
    assert outcome.decode is not None and outcome.decode.source == "fixture"
    assert outcome.decode.make == "Honda" and outcome.decode.model is None
    assert outcome.decode.model_year is None and outcome.decode.cylinders is None


def test_fixture_rejects_malformed_without_network():
    outcome = FixtureVin().decode("1HGCM82633A00435I")
    assert outcome.decode is None and outcome.reason == "malformed VIN"


def test_nhtsa_request_shape_and_full_parse():
    seen = {}

    def run(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return 200, json.dumps(vpic_payload()).encode()

    outcome = NhtsaVin(run).decode(DOC_VIN)
    assert seen["url"] == ("https://vpic.nhtsa.dot.gov/api/vehicles/"
                           f"DecodeVinValues/{DOC_VIN}?format=json")
    assert seen["timeout"] == TIMEOUT_SECONDS <= 5.0
    assert "modelyear" not in seen["url"]  # never guessed from the 30-year-ambiguous code
    decode = outcome.decode
    assert outcome.reason is None and decode is not None
    assert (decode.make, decode.model, decode.model_year) == ("HONDA", "Accord", 2003)
    assert (decode.cylinders, decode.displacement_l) == (6, 2.999)
    assert (decode.fuel_type, decode.body_class) == ("Gasoline", "Coupe")
    assert decode.source == "nhtsa-vpic"


def test_nhtsa_displacement_falls_back_to_cc():
    row = vpic_payload(DisplacementL="", DisplacementCC="2354", DisplacementCI="")
    outcome = NhtsaVin(lambda u, t: (200, json.dumps(row).encode())).decode(DOC_VIN)
    assert outcome.decode is not None and outcome.decode.displacement_l == pytest.approx(2.354)


def test_nhtsa_blanks_become_none():
    row = vpic_payload(Make="", Model="", ModelYear="", EngineCylinders="", DisplacementL="",
                       DisplacementCC="", DisplacementCI="", FuelTypePrimary="", BodyClass="",
                       ErrorCode="0", ErrorText="")
    outcome = NhtsaVin(lambda u, t: (200, json.dumps(row).encode())).decode(DOC_VIN)
    assert outcome.decode is not None and outcome.reason is None
    assert outcome.decode.make is None and outcome.decode.model_year is None


def test_nhtsa_undecodable_vin_returns_reason_not_raw_text():
    row = vpic_payload(Make="", Model="", ModelYear="", ErrorCode="7",
                       ErrorText="7 - VIN is valid but not decoded in the database")
    outcome = NhtsaVin(lambda u, t: (200, json.dumps(row).encode())).decode(DOC_VIN)
    assert outcome.decode is None and outcome.reason is not None
    assert "could not decode" in outcome.reason


def test_nhtsa_failure_modes_return_none_with_reason():
    cases = [
        (lambda u, t: (500, b"{}"), "HTTP 500"),
        (lambda u, t: (200, b"{oops"), "malformed"),
        (lambda u, t: (200, json.dumps({"Results": []}).encode()), "no results"),
    ]
    for transport, fragment in cases:
        outcome = NhtsaVin(transport).decode(DOC_VIN)
        assert outcome.decode is None and fragment in (outcome.reason or "")


def test_nhtsa_timeout_returns_none():
    def run(url, timeout):
        raise VinFetchError("request timed out")

    outcome = NhtsaVin(run).decode(DOC_VIN)
    assert outcome.decode is None and outcome.reason == "vpic request timed out"


def test_nhtsa_unexpected_transport_error_sanitized():
    def run(url, timeout):
        raise RuntimeError("boom")

    outcome = NhtsaVin(run).decode(DOC_VIN)
    assert outcome.decode is None and outcome.reason == "vpic request failed"


def test_nhtsa_rejects_malformed_before_network():
    calls = []

    def run(url, timeout):
        calls.append(url)
        return 200, b"{}"

    outcome = NhtsaVin(run).decode("too short")
    assert outcome.decode is None and outcome.reason == "malformed VIN" and calls == []
