import json
import time
import wave

import numpy as np
import pytest

from upclose_rig.anomaly import AnomalyError, AnomalyModel, assess_records, fit_from_records
from upclose_rig.anomaly.features import AUDIO_SR
from upclose_rig.cli import main
from upclose_rig.protocol import Record
from upclose_rig.recording import make_header, write_recording
from upclose_rig.simulator import SimConfig, simulate


@pytest.fixture(scope="module")
def baseline():
    return simulate(SimConfig(seconds=60, rpm=780, seed=11))


@pytest.fixture(scope="module")
def model(baseline):
    return fit_from_records(baseline, source="simulator")


def accel(xyz, fs=400):
    return [Record("A", int(round(i * 1000 / fs)), tuple(int(v) for v in row)) for i, row in enumerate(xyz)]


def test_fit_estimates_firing_frequency_and_threshold(model):
    assert model.fire_hz == pytest.approx(26.0, abs=0.05)
    assert model.threshold > 0 and model.n_fit_windows >= 30 and model.n_calibration_windows >= 30
    assert model.baseline_source == "simulator"


def test_save_load_roundtrip_gives_identical_verdict(tmp_path, model):
    path = tmp_path / "m.json"
    model.save(path)
    json.loads(path.read_text())
    loaded = AnomalyModel.load(path)
    recs = simulate(SimConfig(seconds=20, rpm=780, misfire_every=4, seed=31))
    assert assess_records(loaded, recs, source="simulator") == assess_records(model, recs, source="simulator")


def test_bad_model_file_rejected(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"schema": "other"}))
    with pytest.raises(AnomalyError):
        AnomalyModel.load(p)
    p.write_text("not json")
    with pytest.raises(AnomalyError):
        AnomalyModel.load(p)


def test_healthy_heldout_recordings_score_mostly_normal(model):
    for seed in (21, 23, 24):
        v = assess_records(model, simulate(SimConfig(seconds=30, rpm=780, seed=seed)), source="simulator")
        assert v["verdict"] == "no_anomaly_detected", v
        assert v["anomalous_fraction"] < 0.1


def test_strong_misfire_is_flagged_with_evidence(model):
    v = assess_records(model, simulate(SimConfig(seconds=30, rpm=780, misfire_every=4, seed=41)), source="simulator")
    assert v["verdict"] == "anomaly_suspected"
    assert v["anomalous_fraction"] > 0.9
    assert v["anomalous_runs"] and v["top_features"]
    assert v["schema"] == "upclose-anomaly-verdict/1" and v["data_source"] == "simulator"


def test_not_assessed_on_short_flat_clipped_and_other_bad_input(model):
    short = simulate(SimConfig(seconds=2, rpm=780, seed=5))
    v = assess_records(model, short)
    assert (v["verdict"], v["reason"]) == ("not_assessed", "too_short")

    flat = accel(np.tile([18, -12, 984], (6000, 1)))
    v = assess_records(model, flat)
    assert (v["verdict"], v["reason"]) == ("not_assessed", "flat_signal")

    xyz = np.array([r.values for r in simulate(SimConfig(seconds=20, rpm=780, seed=6)) if r.kind == "A"])
    clipped = accel(np.clip(984 + (xyz - 984) * 40, -4000, 4000))
    v = assess_records(model, clipped)
    assert (v["verdict"], v["reason"]) == ("not_assessed", "clipped_signal")

    v = assess_records(model, accel(xyz, fs=100))
    assert (v["verdict"], v["reason"]) == ("not_assessed", "sample_rate_mismatch")

    v = assess_records(model, [Record("S", 0, (100,))])
    assert (v["verdict"], v["reason"]) == ("not_assessed", "no_accel")
    assert v["anomalous_fraction"] is None  # never a clean-looking number


def test_fit_refuses_short_or_flat_baselines():
    with pytest.raises(AnomalyError, match="baseline_too_short"):
        fit_from_records(simulate(SimConfig(seconds=10, seed=1)))
    with pytest.raises(AnomalyError, match="baseline_unusable"):
        fit_from_records(accel(np.tile([18, -12, 984], (10000, 1))))


def test_scoring_60s_of_400hz_takes_under_one_second(model):
    recs = simulate(SimConfig(seconds=60, rpm=780, misfire_every=6, seed=51))
    assess_records(model, recs)  # warm numpy
    t0 = time.perf_counter()
    v = assess_records(model, recs)
    elapsed = time.perf_counter() - t0
    assert v["windows"] > 200
    assert elapsed < 1.0, elapsed


# --- audio (synthetic) ---

def engine_audio(seconds, fire_hz, seed, misfire_every=0, hiss=0.0):
    rng = np.random.default_rng(seed)
    n = int(seconds * AUDIO_SR)
    a = rng.normal(0, 0.01, n)
    t_env = np.arange(int(0.012 * AUDIO_SR)) / AUDIO_SR
    burst = np.exp(-t_env / 0.003)
    k = 0
    while True:
        i0 = int((k / fire_hz) * AUDIO_SR)
        if i0 + len(burst) >= n:
            break
        amp = 0.05 if misfire_every and (k + 1) % misfire_every == 0 else 1.0
        a[i0:i0 + len(burst)] += amp * 0.4 * burst * np.sin(2 * np.pi * 220 * t_env)
        k += 1
    if hiss:
        a += rng.normal(0, hiss, n)
    return a


def write_wav(path, a):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(AUDIO_SR)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())


def test_audio_model_requires_audio_and_flags_audio_only_change(tmp_path, baseline):
    base_wav = tmp_path / "base.wav"
    write_wav(base_wav, engine_audio(60, 26.0, 1))
    m = fit_from_records(baseline, base_wav, source="simulator")
    assert m.has_audio and len(m.feature_names) == 10 + 26

    healthy = simulate(SimConfig(seconds=30, rpm=780, seed=61))
    good = tmp_path / "good.wav"
    write_wav(good, engine_audio(30, 26.0, 2))
    assert assess_records(m, healthy, good)["verdict"] == "no_anomaly_detected"

    v = assess_records(m, healthy)  # no audio for an audio model
    assert (v["verdict"], v["reason"]) == ("not_assessed", "audio_required_by_model")

    hissy = tmp_path / "hiss.wav"
    write_wav(hissy, engine_audio(30, 26.0, 3, hiss=0.15))  # vibration healthy, audio broadband change
    v = assess_records(m, healthy, hissy)
    assert v["verdict"] == "anomaly_suspected"
    assert any(f["name"].startswith("aud_") for f in v["top_features"])

    short = tmp_path / "short.wav"
    write_wav(short, engine_audio(5, 26.0, 4))
    assert assess_records(m, healthy, short)["verdict"] == "not_assessed"


# --- CLI ---

def test_cli_fit_and_score(tmp_path, capsys, baseline):
    base = tmp_path / "healthy.upr"
    write_recording(base, make_header("simulator", "2026-11-07T00:00:00Z"), baseline)
    bad = tmp_path / "bad.upr"
    write_recording(bad, make_header("simulator", "2026-11-07T00:00:00Z"), simulate(SimConfig(seconds=20, rpm=780, misfire_every=3, seed=71)))
    mpath = tmp_path / "model.json"
    assert main(["anomaly-fit", "--recording", str(base), "--out", str(mpath)]) == 0
    capsys.readouterr()
    assert main(["anomaly-score", "--model", str(mpath), "--recording", str(bad)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "anomaly_suspected" and out["data_source"] == "simulator"


def test_cli_fit_on_short_recording_is_an_error(tmp_path, capsys):
    short = tmp_path / "short.upr"
    write_recording(short, make_header("simulator", "2026-11-07T00:00:00Z"), simulate(SimConfig(seconds=5, seed=1)))
    assert main(["anomaly-fit", "--recording", str(short), "--out", str(tmp_path / "m.json")]) == 2
    assert "baseline_too_short" in capsys.readouterr().err
