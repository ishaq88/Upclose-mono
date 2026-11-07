import json

import pytest
from pydantic import ValidationError

from upclose_rig.bundle import BundleError, build_rig_bundle, to_json
from upclose_rig.models import ObdSnapshotEnvelope, RigTelemetryData, RigTelemetryEnvelope
from upclose_rig.protocol import Record
from upclose_rig.recording import Recording, make_header
from upclose_rig.simulator import SimConfig, simulate

START = "2026-11-07T21:00:00Z"


def rec_of(records, source="simulator"):
    return Recording(make_header(source, START), records)


def test_bundle_matches_contract_shape():
    rec = rec_of(simulate(SimConfig(seconds=10, cold=True, seed=2)))
    body = json.loads(to_json(build_rig_bundle(rec, "synthetic")))
    assert set(body) == {"kind", "provenance", "device", "captured_at", "data"}
    assert body["kind"] == "rig_telemetry" and body["provenance"] == "synthetic"
    assert body["captured_at"] == START and body["device"] == "grove-beginner-kit"
    d = body["data"]
    assert set(d) == {"accel", "sound", "temperature", "cylinders"}
    assert d["accel"]["rate_hz"] == pytest.approx(400, abs=0.1)
    assert len(d["accel"]["x_mg"]) == len(d["accel"]["y_mg"]) == len(d["accel"]["z_mg"]) == 4000
    assert d["sound"]["rate_hz"] == pytest.approx(100, abs=0.1)
    assert d["temperature"][0]["t_ms"] == 0 and d["temperature"][1]["t_ms"] == 2000
    assert d["cylinders"] == 4


def test_window_shifts_captured_at_and_temperature_t0():
    rec = rec_of(simulate(SimConfig(seconds=20, cold=True, seed=2)))
    env = build_rig_bundle(rec, "synthetic", start_s=4, seconds=6)
    assert env.captured_at == "2026-11-07T21:00:04Z"
    assert env.data.temperature[0].t_ms == 0
    assert len(env.data.accel.x_mg) == 2400


def test_simulator_must_be_synthetic():
    rec = rec_of(simulate(SimConfig(seconds=2)))
    with pytest.raises(BundleError, match="synthetic"):
        build_rig_bundle(rec, "live")
    board = rec_of(simulate(SimConfig(seconds=2)), source="board")
    assert build_rig_bundle(board, "cached").provenance == "cached"


def test_rejects_empty_and_no_accel():
    with pytest.raises(BundleError):
        build_rig_bundle(rec_of([]), "synthetic")
    with pytest.raises(BundleError, match="accelerometer"):
        build_rig_bundle(rec_of([Record("S", 0, (5,)), Record("S", 10, (5,))]), "synthetic")


def test_oversize_rejected_client_side():
    n = 240_001
    recs = [Record("A", i * 2, (0, 0, 1000)) for i in range(n)]  # 500 Hz
    with pytest.raises(BundleError, match="exceeds"):
        build_rig_bundle(rec_of(recs), "synthetic")
    with pytest.raises(ValidationError):
        RigTelemetryData.model_validate({"accel": {"rate_hz": 400, "x_mg": [0] * n, "y_mg": [0] * n, "z_mg": [0] * n}})


def test_out_of_range_accel_rejected_not_clipped():
    recs = [Record("A", i * 2, (20000 if i == 5 else 0, 0, 1000)) for i in range(50)]
    with pytest.raises(ValidationError):
        build_rig_bundle(rec_of(recs), "synthetic")


def test_model_rejects_mismatched_axes_and_ranges():
    ok = {"rate_hz": 400.0, "x_mg": [1, 2], "y_mg": [1, 2], "z_mg": [1, 2]}
    RigTelemetryData.model_validate({"accel": ok})
    for bad in (
        {**ok, "y_mg": [1]},
        {**ok, "rate_hz": 10},
        {**ok, "x_mg": [16001, 0]},
        {**ok, "x_mg": [1.5, 0]},
        {**ok, "x_mg": []},
        {**ok, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            RigTelemetryData.model_validate({"accel": bad})
    for extra in (
        {"sound": {"rate_hz": 100, "level": [1024]}},
        {"temperature": [{"t_ms": 5, "celsius": 20}, {"t_ms": 1, "celsius": 20}]},
        {"temperature": [{"t_ms": 0, "celsius": 151}]},
        {"temperature": [{"t_ms": 0, "celsius": 20}] * 601},
        {"cylinders": 17},
    ):
        with pytest.raises(ValidationError):
            RigTelemetryData.model_validate({"accel": ok, **extra})


def test_envelope_rejects_bad_fields():
    good = {"kind": "rig_telemetry", "provenance": "synthetic", "device": "grove-beginner-kit",
            "captured_at": START, "data": {"accel": {"rate_hz": 400, "x_mg": [1], "y_mg": [1], "z_mg": [1]}}}
    RigTelemetryEnvelope.model_validate(good)
    for patch in ({"provenance": "fake"}, {"device": "Bad Device"}, {"captured_at": "2026-11-07T21:00:00"},
                  {"extra": 1}, {"kind": "obd_snapshot"}):
        with pytest.raises(ValidationError):
            RigTelemetryEnvelope.model_validate({**good, **patch})


def obd(**over):
    base = {"kind": "obd_snapshot", "provenance": "synthetic", "device": "elm327-emulator", "captured_at": START,
            "data": {"vin": "SYNTHETC00000UPCA", "mil_on": True, "dtcs": [{"code": "P0301", "status": "stored"}],
                     "readiness": {"misfire": "complete"}, "pids": {"rpm": 780.0}}}
    base["data"].update(over)
    return base


def test_obd_model_validation():
    ObdSnapshotEnvelope.model_validate(obd())
    for bad in (
        {"dtcs": [{"code": "P0401X", "status": "stored"}]},
        {"dtcs": [{"code": "P4301", "status": "stored"}]},
        {"dtcs": [{"code": "p0301", "status": "stored"}]},
        {"dtcs": [{"code": "P0301", "status": "gone"}]},
        {"dtcs": [{"code": "P0301", "status": "stored"}] * 65},
        {"vin": "SYNTHETIC000000001"},  # contains I: excluded by the contract regex
        {"vin": "ABC"},
        {"readiness": {"Misfire": "complete"}},
        {"readiness": {"misfire": "maybe"}},
        {"pids": {"rpm": float("nan")}},
    ):
        with pytest.raises(ValidationError):
            ObdSnapshotEnvelope.model_validate(obd(**bad))
