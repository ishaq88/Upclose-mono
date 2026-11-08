"""Evidence-source contracts and pure derived summaries (no database)."""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.contracts.sources import (ObdSnapshotData, ObdSnapshotSource, RigTelemetryData, RigTelemetrySource,
                                   SourceRequest)
from app.domain.sources import load_sources_config, summarize_obd, summarize_rig

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_sources_config(ROOT / "config/sources.v1.json")
REQUEST = TypeAdapter(SourceRequest)


def rig_data(freq: float | None = 25.0, *, rate: float = 400.0, seconds: float = 10.0, amp: int = 200,
             cylinders: int = 4, dropouts: tuple[tuple[float, float], ...] = (), noise_only: bool = False,
             temperature: list[dict] | None = None) -> dict:
    rng = random.Random(7)
    count = int(rate * seconds)
    x, y, z = [], [], []
    for i in range(count):
        t = i / rate
        if noise_only or freq is None:
            x.append(rng.randint(-amp, amp))
            y.append(rng.randint(-amp, amp))
            z.append(1000 + rng.randint(-amp, amp))
            continue
        silent = any(start <= t < end for start, end in dropouts)
        wave = 0 if silent else round(amp * math.sin(2 * math.pi * freq * t))
        x.append(wave + rng.randint(-3, 3))
        y.append(rng.randint(-3, 3))
        z.append(1000 + rng.randint(-3, 3))
    data: dict = {"accel": {"rate_hz": rate, "x_mg": x, "y_mg": y, "z_mg": z}, "cylinders": cylinders}
    if temperature is not None:
        data["temperature"] = temperature
    return data


def rig_summary(**kwargs):
    return summarize_rig(RigTelemetryData.model_validate(rig_data(**kwargs)), "synthetic", CONFIG)


def obd_data(**overrides) -> dict:
    return {"vin": "SYNTHETC00000UPCA", "mil_on": True,
            "dtcs": [{"code": "P0301", "status": "stored"}],
            "readiness": {"misfire": "complete", "catalyst": "incomplete", "evap": "incomplete"},
            "distance_since_codes_cleared_km": 12, "warmups_since_codes_cleared": 1,
            "pids": {"rpm": 780.0, "coolant_c": 24.0}, **overrides}


# ---- rig telemetry ---------------------------------------------------------------------------------


@pytest.mark.parametrize("freq,cylinders,rpm", [(25.0, 4, 750), (30.0, 6, 600), (13.0, 2, 780), (23.37, 4, 701)])
def test_known_sine_gives_expected_frequency_and_rpm(freq, cylinders, rpm):
    summary = rig_summary(freq=freq, cylinders=cylinders)
    assert summary.state == "assessed" and summary.reason is None
    assert summary.dominant_hz == pytest.approx(freq, abs=0.1)
    assert summary.rpm_estimate == pytest.approx(rpm, abs=3)
    assert summary.cylinders == cylinders and summary.provenance == "synthetic"
    assert "uncalibrated" in summary.uncalibrated_note
    assert summary.irregular_windows == () and summary.flag_count == 0
    assert summary.roughness is not None and summary.roughness < 0.05


def test_high_sample_rate_is_downsampled_and_still_correct():
    summary = rig_summary(freq=40.0, rate=1000.0, seconds=8.0)
    assert summary.state == "assessed" and summary.dominant_hz == pytest.approx(40.0, abs=0.1)
    assert summary.sample_count == 8000


def test_noise_only_is_not_assessed_with_a_reason():
    summary = rig_summary(noise_only=True)
    assert summary.state == "not_assessed"
    assert "dominant frequency" in (summary.reason or "")
    assert summary.dominant_hz is None and summary.rpm_estimate is None
    assert summary.flag_count == 0 and summary.irregular_windows == ()


def test_too_few_samples_and_flat_signal_are_not_assessed():
    short = rig_summary(freq=25.0, seconds=1.0)
    assert short.state == "not_assessed" and "seconds" in (short.reason or "")
    flat = summarize_rig(RigTelemetryData.model_validate(
        {"accel": {"rate_hz": 400.0, "x_mg": [0] * 4000, "y_mg": [0] * 4000, "z_mg": [1000] * 4000}}),
        "synthetic", CONFIG)
    assert flat.state == "not_assessed" and "No measurable vibration" in (flat.reason or "")


def test_misfire_like_dropouts_become_unverified_irregular_windows():
    summary = rig_summary(freq=25.0, dropouts=((2.0, 2.5), (5.0, 5.5), (7.5, 8.0)))
    assert summary.state == "assessed"
    assert [row.t_ms for row in summary.irregular_windows] == [2000, 5000, 7500]
    assert {row.status for row in summary.irregular_windows} == {"unverified"}
    assert "irregular_vibration" in summary.flags and summary.flag_count == len(summary.flags) >= 1
    assert summary.roughness is not None and summary.roughness > 0.1


def test_irregular_windows_are_capped_at_twenty():
    quiet = tuple((float(second), second + 0.5) for second in range(0, 19, 1))  # 19 s of alternating gaps
    summary = rig_summary(freq=25.0, seconds=20.0, dropouts=quiet)
    assert summary.state == "assessed" and len(summary.irregular_windows) <= 20
    assert summary.irregular_windows == tuple(sorted(summary.irregular_windows, key=lambda row: row.t_ms))


def test_temperature_start_rise_and_warm_start():
    cold = rig_summary(temperature=[{"t_ms": 0, "celsius": 21.44}, {"t_ms": 5000, "celsius": 27.0}])
    assert cold.temperature is not None
    assert (cold.temperature.start_c, cold.temperature.rise_c, cold.temperature.engine_warm_at_start) == (21.4, 5.6, False)
    warm = rig_summary(temperature=[{"t_ms": 0, "celsius": 70.0}])
    assert warm.temperature is not None and warm.temperature.engine_warm_at_start is True
    assert warm.temperature.rise_c is None
    assert rig_summary().temperature is None


def test_temperature_is_kept_even_when_vibration_is_not_assessed():
    summary = rig_summary(noise_only=True, temperature=[{"t_ms": 0, "celsius": 30.0}])
    assert summary.state == "not_assessed" and summary.temperature is not None


def test_summary_is_deterministic_and_fast_for_the_maximum_trace():
    started = time.perf_counter()
    first = rig_summary(freq=25.0, rate=1000.0, seconds=240.0, dropouts=((10.0, 10.5),))
    elapsed = time.perf_counter() - started
    assert first.sample_count == 240000 and first.state == "assessed"
    assert elapsed < 5.0, f"240k-sample summary took {elapsed:.2f}s"
    again = rig_summary(freq=25.0, rate=1000.0, seconds=240.0, dropouts=((10.0, 10.5),))
    assert first.model_dump_json() == again.model_dump_json()


def test_config_hash_is_stable_and_carried_by_summaries():
    assert load_sources_config(ROOT / "config/sources.v1.json").content_hash == CONFIG.content_hash
    assert rig_summary().config_hash == CONFIG.content_hash == summarize_obd(
        ObdSnapshotData.model_validate(obd_data()), "cached", CONFIG).config_hash


# ---- OBD ------------------------------------------------------------------------------------------


def obd(**overrides):
    return summarize_obd(ObdSnapshotData.model_validate(obd_data(**overrides)), "synthetic", CONFIG)


def test_obd_summary_flags_codes_mil_and_recent_clear():
    summary = obd()
    assert summary.mil_on and [row.code for row in summary.dtcs] == ["P0301"]
    assert summary.incomplete_monitors == ("catalyst", "evap")
    assert summary.codes_recently_cleared is True and summary.cold_start is True and summary.coolant_c == 24.0
    assert summary.flags == ("mil_on", "trouble_codes", "codes_recently_cleared") and summary.flag_count == 3


def test_obd_recent_clear_rules_and_unknown_basis():
    far = dict(distance_since_codes_cleared_km=5000, warmups_since_codes_cleared=120)
    done = {"misfire": "complete", "catalyst": "complete"}
    assert obd(**far, readiness=done).codes_recently_cleared is False
    assert obd(distance_since_codes_cleared_km=5000, warmups_since_codes_cleared=3, readiness=done).codes_recently_cleared is True
    many = {name: "incomplete" for name in ("a", "b", "c")}
    assert obd(**far, readiness=many).codes_recently_cleared is True
    unknown = obd(distance_since_codes_cleared_km=None, warmups_since_codes_cleared=None, readiness={}, mil_on=False, dtcs=[])
    assert unknown.codes_recently_cleared is None and unknown.flag_count == 0
    assert obd(pids={}).cold_start is None and obd(pids={"coolant_c": 90.0}).cold_start is False


# ---- contracts --------------------------------------------------------------------------------------


def envelope(kind="rig_telemetry", data=None, **overrides) -> dict:
    return {"kind": kind, "provenance": "synthetic", "device": "grove-beginner-kit",
            "captured_at": "2026-11-07T21:14:03Z", "data": data if data is not None else rig_data(seconds=1.0), **overrides}


def test_valid_envelopes_parse_to_the_right_kind():
    assert isinstance(REQUEST.validate_python(envelope()), RigTelemetrySource)
    assert isinstance(REQUEST.validate_python(envelope("obd_snapshot", obd_data())), ObdSnapshotSource)


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(extra="x"),
    lambda e: e.update(kind="engine_audio"),
    lambda e: e.update(provenance="measured"),
    lambda e: e.update(device="Bad Device"),
    lambda e: e.update(device=""),
    lambda e: e.update(captured_at="2026-11-07T21:14:03"),
    lambda e: e["data"].update(unexpected=1),
    lambda e: e["data"]["accel"].update(y_mg=[0]),
    lambda e: e["data"]["accel"].update(rate_hz=10.0),
    lambda e: e["data"]["accel"].update(x_mg=[20000] + e["data"]["accel"]["x_mg"][1:]),
    lambda e: e["data"].update(cylinders=0),
    lambda e: e["data"].update(cylinders=17),
    lambda e: e["data"].update(sound={"rate_hz": 100.0, "level": [2000]}),
    lambda e: e["data"].update(temperature=[{"t_ms": 10, "celsius": 20.0}, {"t_ms": 5, "celsius": 21.0}]),
    lambda e: e["data"].update(temperature=[{"t_ms": 0, "celsius": 200.0}]),
])
def test_rig_contract_rejects(mutate):
    body = envelope()
    mutate(body)
    with pytest.raises(ValidationError):
        REQUEST.validate_python(body)


@pytest.mark.parametrize("override", [
    {"vin": "SHORT"}, {"vin": "ABCDEFGHJKLMNPRSI"}, {"vin": "abcdefghjklmnprst"},
    {"dtcs": [{"code": "X0301", "status": "stored"}]}, {"dtcs": [{"code": "P0401a", "status": "stored"}]},
    {"dtcs": [{"code": "P4301", "status": "stored"}]}, {"dtcs": [{"code": "P0301", "status": "gone"}]},
    {"dtcs": [{"code": "P0301", "status": "stored", "x": 1}]},
    {"dtcs": [{"code": "P0301", "status": "stored"}] * 65},
    {"readiness": {"Bad Name": "complete"}}, {"readiness": {"misfire": "maybe"}},
    {"pids": {"rpm": float("inf")}}, {"pids": {"RPM": 1.0}}, {"mil_on": "yes"},
    {"distance_since_codes_cleared_km": -1},
])
def test_obd_contract_rejects(override):
    with pytest.raises(ValidationError):
        REQUEST.validate_python(envelope("obd_snapshot", obd_data(**override)))


def test_obd_vin_optional_and_json_round_trip():
    data = obd_data()
    data.pop("vin")
    source = REQUEST.validate_python(envelope("obd_snapshot", data))
    assert source.data.vin is None
    assert json.loads(source.data.model_dump_json())["dtcs"][0]["code"] == "P0301"


# ---- model verdict ----------------------------------------------------------------------------

import copy  # noqa: E402

VERDICTS = Path(__file__).parent / "fixtures" / "verdicts"


def verdict(name: str) -> dict:
    return json.loads((VERDICTS / f"{name}.json").read_text(encoding="utf-8"))


def with_verdict(name: str = "misfire", seconds: float = 40.0, **kwargs) -> dict:
    data = rig_data(seconds=seconds, **kwargs)
    data["model_verdict"] = verdict(name)
    return data


@pytest.mark.parametrize("name,expected", [("misfire", "anomaly_suspected"), ("ok", "no_anomaly_detected"),
                                           ("short", "not_assessed")])
def test_generated_verdicts_are_accepted(name, expected):
    data = RigTelemetryData.model_validate(with_verdict(name))
    assert data.model_verdict is not None and data.model_verdict.verdict == expected
    assert json.loads(data.model_dump_json())["model_verdict"]["schema"] == "upclose-anomaly-verdict/1"


def test_suspected_verdict_adds_flag_and_travels_in_the_summary():
    summary = summarize_rig(RigTelemetryData.model_validate(with_verdict("misfire")), "synthetic", CONFIG)
    assert summary.state == "assessed" and "anomaly_suspected" in summary.flags
    assert summary.flag_count == len(summary.flags) and summary.model_verdict is not None
    assert summary.model_verdict.model.baseline_seconds == 60.0
    for name in ("ok", "short"):
        quiet = summarize_rig(RigTelemetryData.model_validate(with_verdict(name)), "synthetic", CONFIG)
        assert "anomaly_suspected" not in quiet.flags and quiet.flag_count == 0


def test_flag_survives_when_the_spectral_search_is_not_assessed():
    summary = summarize_rig(RigTelemetryData.model_validate(with_verdict("misfire", noise_only=True)), "synthetic", CONFIG)
    assert summary.state == "not_assessed" and summary.flags == ("anomaly_suspected",) and summary.flag_count == 1


def test_recording_without_a_verdict_is_unchanged():
    data = RigTelemetryData.model_validate(rig_data())
    summary = summarize_rig(data, "synthetic", CONFIG)
    assert data.model_verdict is None and summary.model_verdict is None and summary.flags == ()


def _mut(path_fn):
    def apply(data: dict) -> None:
        path_fn(data["model_verdict"])
    return apply


INCONSISTENT = {
    "fraction": _mut(lambda v: v.update(anomalous_fraction=0.5)),
    "verdict_flipped": _mut(lambda v: v.update(verdict="no_anomaly_detected")),
    "more_anomalous_than_windows": _mut(lambda v: v.update(anomalous_windows=v["windows"] + 1, anomalous_fraction=1.0)),
    "bad_schema": _mut(lambda v: v.update(schema="upclose-anomaly-verdict/2")),
    "extra_field": _mut(lambda v: v.update(extra=1)),
    "nan_score": _mut(lambda v: v.update(max_score=float("nan"))),
    "bad_config_hash": _mut(lambda v: v["model"].update(config_hash="xyz")),
    "six_features": _mut(lambda v: v.update(top_features=[{"name": f"f{i}", "share": 0.1} for i in range(6)])),
    "share_over_one": _mut(lambda v: v.update(top_features=[{"name": "a", "share": 0.7}, {"name": "b", "share": 0.7}])),
    "eleven_runs": _mut(lambda v: v.update(anomalous_runs=[
        {"t_start_s": float(i), "t_end_s": i + 0.5, "peak_score": 5.0} for i in range(11)])),
    "run_past_recording": _mut(lambda v: v["anomalous_runs"][0].update(t_end_s=41.0)),
    "run_backwards": _mut(lambda v: v["anomalous_runs"][0].update(t_start_s=30.0, t_end_s=10.0)),
    "runs_overlap": _mut(lambda v: v.update(anomalous_runs=[
        {"t_start_s": 0.0, "t_end_s": 20.0, "peak_score": 5.0}, {"t_start_s": 10.0, "t_end_s": 30.0, "peak_score": 5.0}])),
    "run_peak_over_max": _mut(lambda v: v["anomalous_runs"][0].update(peak_score=v["max_score"] + 5)),
    "no_runs_but_anomalous": _mut(lambda v: v.update(anomalous_runs=[])),
    "too_many_windows": _mut(lambda v: v.update(windows=500, anomalous_windows=500)),
    "median_over_max": _mut(lambda v: v.update(median_score=v["max_score"] + 1)),
    "reason_on_assessed": _mut(lambda v: v.update(reason="too_short")),
}


@pytest.mark.parametrize("name", sorted(INCONSISTENT))
def test_inconsistent_verdicts_are_rejected(name):
    data = with_verdict("misfire")
    INCONSISTENT[name](data)
    with pytest.raises(ValidationError):
        RigTelemetryData.model_validate(data)


def test_below_threshold_verdicts_cannot_claim_suspected_and_vice_versa():
    data = with_verdict("ok")
    data["model_verdict"]["verdict"] = "anomaly_suspected"  # 4 windows, 2.5 percent: below the 15 percent threshold
    with pytest.raises(ValidationError):
        RigTelemetryData.model_validate(data)
    few = with_verdict("misfire")
    few["model_verdict"].update(windows=10, anomalous_windows=2, anomalous_fraction=0.2)  # fewer than 3 windows
    with pytest.raises(ValidationError):
        RigTelemetryData.model_validate(few)
    quiet = with_verdict("ok")
    quiet["model_verdict"].update(top_features=[{"name": "vib_crest", "share": 0.5}])
    with pytest.raises(ValidationError):
        RigTelemetryData.model_validate(quiet)


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(reason=None),
    lambda v: v.update(windows=3),
    lambda v: v.update(anomalous_fraction=0.0),
    lambda v: v.update(max_score=1.0),
    lambda v: v.update(top_features=[{"name": "a", "share": 0.1}]),
])
def test_not_assessed_must_carry_a_reason_and_null_counts(mutate):
    data = with_verdict("short", seconds=10.0)
    mutate(data["model_verdict"])
    with pytest.raises(ValidationError):
        RigTelemetryData.model_validate(data)


def test_verdict_cannot_cover_more_time_than_the_recording():
    with pytest.raises(ValidationError):
        RigTelemetryData.model_validate(with_verdict("misfire", seconds=10.0))
