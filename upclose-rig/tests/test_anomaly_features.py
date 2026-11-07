import wave

import numpy as np
import pytest

from upclose_rig.anomaly.features import (
    AUDIO_SR, VIB_FEATURES, AnomalyError, cycle_cv, estimate_fire_hz, load_wav, quality_problem, resample_16k,
    vib_features,
)

FS = 400.0


def tone(f, seconds=10.0, amp=100.0, fs=FS):
    t = np.arange(int(seconds * fs)) / fs
    return amp * np.sin(2 * np.pi * f * t)


def col(name):
    return VIB_FEATURES.index(name)


def test_fire_frequency_estimate_on_known_tone():
    assert estimate_fire_hz(tone(26.3, 30.0), FS) == pytest.approx(26.3, abs=0.05)


def test_features_on_pure_tone():
    f = vib_features(tone(26.0), FS, 26.0)
    assert f.shape[1] == len(VIB_FEATURES)
    assert np.all(np.isfinite(f))
    # rms of a sine = A/sqrt(2); crest sqrt(2); excess kurtosis -1.5
    assert np.exp(f[:, col("vib_log_rms")]) == pytest.approx(100 / np.sqrt(2), rel=0.01)
    assert f[:, col("vib_crest")] == pytest.approx(np.sqrt(2), rel=0.05)
    assert f[:, col("vib_kurtosis")] == pytest.approx(-1.5, abs=0.15)
    # nearly all energy sits in the firing band; almost none at half order
    assert np.exp(f[:, col("vib_log_frac_fire")]).min() > 0.9
    assert np.exp(f[:, col("vib_log_frac_half")]).max() < 0.02
    assert f[:, col("vib_cycle_cv")].max() < 0.1


def test_half_order_energy_shows_sub_harmonic():
    sig = tone(26.0) + tone(13.0, amp=60.0)
    f = vib_features(sig, FS, 26.0)
    assert np.exp(f[:, col("vib_log_frac_half")]).min() > 0.2


def test_amplitude_irregularity_raises_cycle_cv():
    steady = np.tile(np.concatenate([[1.0], np.zeros(15)]), 200)[None, :]
    uneven = steady.copy()
    uneven[0, ::32] = 0.1
    assert cycle_cv(steady, FS, FS / 16)[0] < 0.01
    assert cycle_cv(uneven, FS, FS / 16)[0] > 0.3


def test_band_fractions_do_not_depend_on_gain():
    a = vib_features(tone(26.0) + tone(13.0, amp=30.0), FS, 26.0)
    b = vib_features(3.0 * (tone(26.0) + tone(13.0, amp=30.0)), FS, 26.0)
    for name in ("vib_log_frac_fire", "vib_log_frac_half", "vib_sub_ratio", "vib_crest"):
        assert a[:, col(name)] == pytest.approx(b[:, col(name)], abs=1e-6)


def test_quality_problems():
    rng = np.random.default_rng(0)
    ok = rng.normal(0, 30, (4000, 3)) + [10, -10, 980]
    assert quality_problem(ok) is None
    assert quality_problem(np.tile([10.0, -10.0, 980.0], (4000, 1))) == "flat_signal"
    clipped = np.clip(ok * 8, -4000, 4000)
    assert quality_problem(clipped) == "clipped_signal"
    assert quality_problem(np.where(np.arange(4000)[:, None] % 2, np.nan, ok)) == "non_finite_signal"


def test_wav_roundtrip_and_resample(tmp_path):
    sr = 8000
    x = (0.5 * np.sin(2 * np.pi * 1000 * np.arange(sr) / sr) * 32767).astype("<i2")
    p = tmp_path / "t.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())
    a, rate = load_wav(p)
    assert rate == sr and len(a) == sr
    r = resample_16k(a, rate)
    assert len(r) == AUDIO_SR
    peak = np.fft.rfftfreq(len(r), 1 / AUDIO_SR)[np.argmax(np.abs(np.fft.rfft(r)))]
    assert peak == pytest.approx(1000, abs=2)
    assert r.std() == pytest.approx(a.std(), rel=0.02)


def test_not_a_wav_is_an_error(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(b"nope")
    with pytest.raises(AnomalyError):
        load_wav(p)
