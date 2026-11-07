import numpy as np

from upclose_rig.anomaly import StreamScorer
from upclose_rig.anomaly import evaluate as ev
from upclose_rig.protocol import Record
from upclose_rig.simulator import SimConfig, simulate


def run(scorer, recs, chunk=200):
    events = []
    for i in range(0, len(recs), chunk):
        events.extend(scorer.push(recs[i:i + chunk]))
    return events


def shifted(recs, ms):
    return [Record(r.kind, r.t_ms + ms, r.values) for r in recs]


def test_stream_baselines_then_flags_a_misfire_and_clears():
    healthy = simulate(SimConfig(seconds=30, rpm=780, seed=11))
    bad = shifted(simulate(SimConfig(seconds=20, rpm=780, misfire_every=3, seed=12)), 30000)
    ok = shifted(simulate(SimConfig(seconds=20, rpm=780, seed=13)), 50000)
    events = run(StreamScorer(baseline_s=25.0), healthy + bad + ok)
    names = [e["event"] for e in events]
    assert names[0] == "baseline_ready"
    assert "anomaly_suspected" in names
    first = next(e for e in events if e["event"] == "anomaly_suspected")
    assert 25000 < first["t_ms"] <= 60000 and first["top_features"]
    assert names[-1] == "anomaly_cleared"


def test_stream_healthy_only_stays_quiet():
    recs = simulate(SimConfig(seconds=60, rpm=780, seed=14))
    events = run(StreamScorer(baseline_s=25.0), recs)
    assert [e["event"] for e in events] == ["baseline_ready"]


def test_stream_flat_baseline_reports_not_assessed_not_ready():
    recs = [Record("A", i * 5 // 2, (18, -12, 984)) for i in range(12000)]
    events = run(StreamScorer(baseline_s=25.0), recs)
    assert [e["event"] for e in events] == ["not_assessed"]
    assert events[0]["reason"] == "baseline_unusable"


def test_eval_report_is_deterministic(monkeypatch):
    monkeypatch.setattr(ev, "RPMS", (780,))
    monkeypatch.setattr(ev, "EXTRA_NOISE_MG", (0.0,))
    monkeypatch.setattr(ev, "MISFIRE_EVERY", (4,))
    monkeypatch.setattr(ev, "HEALTHY_PER_CELL", 2)
    monkeypatch.setattr(ev, "MISFIRE_PER_LEVEL", 2)
    a = ev.build_report(ev.run_cells())
    b = ev.build_report(ev.run_cells())
    assert a == b
    assert "SYNTHETIC" in a and "real-car accuracy" in a
    assert ev.roc_auc(np.array([1.0, 2.0]), np.array([3.0, 4.0])) == 1.0
    assert ev.roc_auc(np.array([1.0, 2.0]), np.array([1.0, 2.0])) == 0.5
