import numpy as np
import pytest

from upclose_rig.recording import make_header, read_recording, write_recording
from upclose_rig.simulator import SimConfig, parse_event, simulate


def zaxis(records):
    z = np.array([r.values[2] for r in records if r.kind == "A"], dtype=float)
    return z - z.mean()


def dominant_hz(records, lo=5.0, hi=60.0, rate=400.0):
    z = zaxis(records)
    spec = np.abs(np.fft.rfft(z * np.hanning(len(z))))
    f = np.fft.rfftfreq(len(z), 1 / rate)
    band = (f >= lo) & (f <= hi)
    return float(f[band][np.argmax(spec[band])])


def test_deterministic_with_seed():
    a = simulate(SimConfig(seconds=5, seed=7, misfire_every=3, cold=True))
    b = simulate(SimConfig(seconds=5, seed=7, misfire_every=3, cold=True))
    c = simulate(SimConfig(seconds=5, seed=8))
    assert a == b
    assert a != c


def test_780_rpm_dominant_frequency_is_26_hz(idle_records):
    assert dominant_hz(idle_records) == pytest.approx(26.0, abs=0.2)


def test_misfire_adds_subharmonic():
    healthy = zaxis(simulate(SimConfig(seconds=30, seed=3)))
    bad = zaxis(simulate(SimConfig(seconds=30, seed=3, misfire_every=4)))
    f = np.fft.rfftfreq(len(bad), 1 / 400)
    sub = int(np.argmin(abs(f - 26 / 4)))
    band = slice(sub - 3, sub + 4)
    assert np.abs(np.fft.rfft(bad))[band].max() > 3 * np.abs(np.fft.rfft(healthy))[band].max()


def test_cold_vs_warm_temperature_and_rates():
    cold = simulate(SimConfig(seconds=60, cold=True, seed=1))
    warm = simulate(SimConfig(seconds=60, seed=1))
    tc = [r.values[0] for r in cold if r.kind == "T"]
    tw = [r.values[0] for r in warm if r.kind == "T"]
    assert tc[0] < 25 and tc[-1] > tc[0] + 2
    assert tw[0] > 80
    assert sum(r.kind == "A" for r in cold) == 24000
    assert sum(r.kind == "S" for r in cold) == 6000
    assert all(0 <= r.values[0] <= 1023 for r in cold if r.kind == "S")


def test_events_sorted_and_validated():
    recs = simulate(SimConfig(seconds=2, events=("B@1=1", "K@0.5=700")))
    ev = [r for r in recs if r.kind in "BK"]
    assert [r.kind for r in ev] == ["K", "B"]
    with pytest.raises(ValueError):
        parse_event("B@1=2")
    with pytest.raises(ValueError):
        parse_event("X@1=1")
    with pytest.raises(ValueError):
        simulate(SimConfig(seconds=0))


def test_recording_roundtrip(tmp_path):
    recs = simulate(SimConfig(seconds=2, seed=1))
    p = tmp_path / "r.upr"
    write_recording(p, make_header("simulator", "2026-11-07T21:00:00Z"), recs)
    back = read_recording(p)
    assert back.records == recs and back.source == "simulator"
    (tmp_path / "bad").write_text("not json\n")
    with pytest.raises(ValueError):
        read_recording(tmp_path / "bad")
