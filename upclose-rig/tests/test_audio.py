import hashlib
import json
import wave

import numpy as np

from upclose_rig import audio
from upclose_rig.audio import AudioConfig, misfired, pulse_times, synthesize, write_synthetic


def _spectrum(x):
    x = x.astype(float)
    x -= x.mean()
    mag = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / audio.SAMPLE_RATE), mag


def test_spectral_peak_at_firing_frequency():
    cfg = AudioConfig(seconds=20, rpm=900, cylinders=4)  # 30 Hz
    f, m = _spectrum(synthesize(cfg))
    band = (f > 5) & (f < 400)
    assert abs(f[band][np.argmax(m[band])] - cfg.fire_hz) < 0.5


def test_peak_tracks_rpm_and_cylinders():
    cfg = AudioConfig(seconds=20, rpm=1200, cylinders=6)  # 60 Hz
    f, m = _spectrum(synthesize(cfg))
    band = (f > 5) & (f < 400)
    assert abs(f[band][np.argmax(m[band])] - 60.0) < 0.5


def test_misfire_lowers_energy_of_matching_pulses():
    cfg = AudioConfig(seconds=10, rpm=780, cylinders=4, misfire_every=3)
    x = synthesize(cfg).astype(float)
    drop, times = misfired(cfg), pulse_times(cfg)
    half = int(0.012 * audio.SAMPLE_RATE)  # the pulse itself, not the previous one's tail

    def energy(k):
        i = int(times[k] * audio.SAMPLE_RATE)
        return float(np.mean(x[i:i + half] ** 2))

    idx = [k for k in range(2, len(times) - 2)]
    d = np.mean([energy(k) for k in idx if drop[k]])
    ok = np.mean([energy(k) for k in idx if not drop[k]])
    assert drop.sum() > 10 and d < 0.25 * ok


def test_misfire_window_limits_dropped_pulses():
    cfg = AudioConfig(seconds=10, misfire_every=2, misfire_from_s=4, misfire_until_s=6)
    d, t = misfired(cfg), pulse_times(cfg)
    assert d.any() and t[d].min() >= 4 and t[d].max() < 6


def test_same_seed_same_bytes_and_other_seed_differs(tmp_path):
    cfg = AudioConfig(seconds=3, misfire_every=3, seed=7)
    a, b, c = tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "c.wav"
    write_synthetic(a, cfg)
    write_synthetic(b, cfg)
    write_synthetic(c, AudioConfig(seconds=3, misfire_every=3, seed=8))
    assert a.read_bytes() == b.read_bytes()
    assert (tmp_path / "a.wav.json").read_bytes() == (tmp_path / "b.wav.json").read_bytes()
    assert hashlib.sha256(a.read_bytes()).digest() != hashlib.sha256(c.read_bytes()).digest()


def test_wav_format_and_sidecar(tmp_path):
    cfg = AudioConfig(seconds=2, rpm=800, cylinders=6, misfire_every=4, cold=True, seed=5)
    p = tmp_path / "e.wav"
    side = write_synthetic(p, cfg)
    with wave.open(str(p)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 32000)
    j = json.loads(side.read_text())
    assert side.name == "e.wav.json" and j["provenance"] == "synthetic"
    assert j["params"]["rpm"] == 800 and j["params"]["cylinders"] == 6 and j["params"]["seed"] == 5
    assert j["params"]["misfire_every"] == 4 and j["params"]["cold"] is True and j["params"]["seconds"] == 2
