"""Regressions from the first real run: a still pod on a desk delivers a quiet signal quantized
in 8 mg steps. It must never be called clipped, must baseline, and must give no alert and no
N/X flicker. Fixture: real board data (source=board, no personal data)."""

from pathlib import Path

import numpy as np
import pytest

from upclose_rig.anomaly import StreamScorer
from upclose_rig.anomaly.features import accel_from_records, quality_problem
from upclose_rig.live import SimPort, run_live
from upclose_rig.protocol import Record
from upclose_rig.recording import read_recording
from upclose_rig.simulator import SimConfig, simulate

FIXTURE = Path(__file__).parent / "fixtures" / "board_still_desk.upr"


@pytest.fixture(scope="module")
def still():
    rec = read_recording(FIXTURE)
    assert rec.source == "board"
    return rec


def test_fixture_is_the_quantized_quiet_board_signal(still):
    xyz, fs = accel_from_records(still.records)
    assert len(xyz) > 12000 and fs == pytest.approx(397, abs=5)
    for ax in range(3):
        assert set(np.unique(np.diff(np.unique(xyz[:, ax])))) == {8.0}  # 8 mg quantization
        assert xyz[:, ax].std() < 15


@pytest.mark.parametrize("window", [200, 400, 800, 2000])
def test_quiet_quantized_signal_is_never_clipped_or_flat(still, window):
    xyz, _ = accel_from_records(still.records)
    problems = {quality_problem(xyz[i:i + window]) for i in range(0, len(xyz) - window, 25)}
    assert problems == {None}


def test_true_saturation_near_full_scale_is_still_clipped():
    rng = np.random.default_rng(3)
    xyz = rng.normal(0, 30, (2000, 3)) + [10, -10, 980]
    sat = xyz.copy()
    sat[:, 2] = np.clip(sat[:, 2] * 8, -4000, 4000)  # rails at +/-4 g
    assert quality_problem(sat) == "clipped_signal"
    one_rail = xyz.copy()
    one_rail[::10, 0] = 4000.0  # 10 % of samples pinned at +4 g
    assert quality_problem(one_rail) == "clipped_signal"
    low = np.clip(xyz * 8, -300, 300)  # repeated extremes far from full scale are not saturation
    assert quality_problem(low) is None


def test_live_loop_on_still_board_data_baselines_and_stays_quiet(still):
    res = run_live(SimPort(still.records, speed=0), baseline_s=20.0, out=lambda s: None)
    assert [e["event"] for e in res.events] == ["baseline_ready"]
    states = [c for c in res.commands if not c.startswith("!S,L")]
    assert states and set(states) == {"!S,N"}  # no X, no A, no N/X alternation
    assert res.commands[0].startswith("!S,L")


def test_stream_pod_state_does_not_flicker_on_a_saturation_burst():
    recs = simulate(SimConfig(seconds=60, rpm=780, seed=14))
    out = []
    for r in recs:
        if r.kind == "A" and 40000 <= r.t_ms < 42000:  # 2 s of saturation
            out.append(Record("A", r.t_ms, (4000 if r.t_ms % 2 else -4000, 4000, 4000)))
        else:
            out.append(r)
    s = StreamScorer(baseline_s=25.0)
    seq = []
    for i in range(0, len(out), 40):
        s.push(out[i:i + 40])
        seq.append(s.pod_state)
    runs = [seq[0]] + [b for a, b in zip(seq, seq[1:]) if a != b]
    assert runs == ["L", "N", "X", "N"], runs  # one contiguous X, then back to normal
