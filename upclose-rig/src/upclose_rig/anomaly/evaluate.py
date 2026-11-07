"""Deterministic evaluation on SYNTHETIC simulator recordings.

Every number comes from `upclose_rig.simulator` data: a healthy baseline per
(rpm, extra-noise) condition, then held-out recordings (seeds never used while
choosing constants) that are healthy or have every Nth firing dropped. The
fault generator is the simulator's own, so results measure whether the
detector finds that *kind* of change, not real-car accuracy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ..simulator import SimConfig, simulate
from .features import accel_from_records
from .model import FRACTION_THRESHOLD, THRESHOLD_PERCENTILE, _features, assess_arrays, fit_arrays

RPMS = (780, 1500, 2400, 3000)
EXTRA_NOISE_MG = (0.0, 10.0, 20.0)  # added per axis on top of the simulator's 5 mg
MISFIRE_EVERY = (2, 3, 4, 6, 8, 12)
HEALTHY_PER_CELL = 4
MISFIRE_PER_LEVEL = 4
BASELINE_S = 60.0
TEST_S = 30.0
HELD_OUT_SEED_BASE = 1000  # constants were chosen using seeds < 100 only


def roc_auc(neg: np.ndarray, pos: np.ndarray) -> float:
    """P(pos > neg) + 0.5 P(tie), by sorted search."""
    if len(neg) == 0 or len(pos) == 0:
        return float("nan")
    s = np.sort(neg)
    lo = np.searchsorted(s, pos, side="left")
    hi = np.searchsorted(s, pos, side="right")
    return float((lo + 0.5 * (hi - lo)).sum() / (len(neg) * len(pos)))


def _noisy(cfg: SimConfig, extra_mg: float, noise_seed: int) -> tuple[np.ndarray, float]:
    xyz, fs = accel_from_records(simulate(cfg))
    if extra_mg > 0:
        xyz = xyz + np.random.default_rng(noise_seed).normal(0.0, extra_mg, xyz.shape)
    return xyz, fs


def run_cells(seed_base: int = HELD_OUT_SEED_BASE) -> list[dict[str, Any]]:
    """One row per (rpm, noise, misfire_every or 0, seed) recording."""
    rows: list[dict[str, Any]] = []
    for ri, rpm in enumerate(RPMS):
        for ni, extra in enumerate(EXTRA_NOISE_MG):
            cell = seed_base + 100 * (ri * len(EXTRA_NOISE_MG) + ni)
            xyz, fs = _noisy(SimConfig(seconds=BASELINE_S, rpm=rpm, seed=cell), extra, cell + 50)
            model = fit_arrays(xyz, fs, None, "simulator")
            thr = model.threshold
            plan = [(0, s) for s in range(HEALTHY_PER_CELL)]
            plan += [(n, s) for n in MISFIRE_EVERY for s in range(MISFIRE_PER_LEVEL)]
            for n, s in plan:
                seed = cell + 1 + (0 if n == 0 else MISFIRE_EVERY.index(n) + 1) * 10 + s
                xyz_t, fs_t = _noisy(SimConfig(seconds=TEST_S, rpm=rpm, misfire_every=n, seed=seed), extra, seed + 50)
                v = assess_arrays(model, xyz_t, fs_t, None, "simulator")
                feats = _features(np.array(model.direction), model.fire_hz, xyz_t, fs_t, None)
                scores = model.score(feats)[0]
                rows.append({
                    "rpm": rpm, "noise": extra, "n": n, "verdict": v["verdict"], "reason": v["reason"],
                    "fraction": v["anomalous_fraction"], "norm_scores": scores / thr,
                })
    return rows


def _rate(rows: list[dict[str, Any]], pred: str = "anomaly_suspected") -> float:
    return sum(r["verdict"] == pred for r in rows) / len(rows) if rows else float("nan")


def _pct(x: float) -> str:
    return "n/a" if x != x else f"{100 * x:.0f}%"


def _auc_of(healthy: list[dict[str, Any]], faulty: list[dict[str, Any]]) -> tuple[float, float]:
    win = roc_auc(np.concatenate([r["norm_scores"] for r in healthy]), np.concatenate([r["norm_scores"] for r in faulty]))
    rec = roc_auc(np.array([r["fraction"] for r in healthy], float), np.array([r["fraction"] for r in faulty], float))
    return win, rec


def _window_rates(healthy: list[dict[str, Any]], faulty: list[dict[str, Any]]) -> tuple[float, float]:
    h = np.concatenate([r["norm_scores"] for r in healthy])
    f = np.concatenate([r["norm_scores"] for r in faulty])
    return float((h > 1.0).mean()), float((f > 1.0).mean())


def build_report(rows: list[dict[str, Any]]) -> str:
    healthy = [r for r in rows if r["n"] == 0]
    faulty = [r for r in rows if r["n"] != 0]
    na = [r for r in rows if r["verdict"] == "not_assessed"]
    win_auc, rec_auc = _auc_of(healthy, faulty)
    win_fpr, win_tpr = _window_rates(healthy, faulty)
    out: list[str] = []
    w = out.append
    w("# Engine anomaly model: evaluation report (H16)")
    w("")
    w("**All data here is SYNTHETIC**, produced by `upclose_rig.simulator`. This measures whether a baseline-relative")
    w("detector finds a simulator-style misfire (every Nth firing event dropped) on a simulator engine. It is **not**")
    w("real-car accuracy and must not be quoted as such.")
    w("")
    w("Regenerate: `python -m upclose_rig anomaly-eval` (fixed seeds; the output is byte-identical between runs).")
    w("")
    w("## Method")
    w("")
    w("- Features per 0.5 s window (50 % overlap) from the principal-axis vibration: RMS, crest factor, kurtosis, band energy at the")
    w("  firing frequency, its half order, sub-orders, 2x and the high band, a sub-order ratio, and firing-to-firing amplitude spread.")
    w("  The firing frequency is estimated from the baseline.")
    w("- Model: median/MAD standardization, Ledoit-Wolf-shrunk covariance (floor 0.05), Mahalanobis distance. Baseline windows are")
    w("  split in alternating 8-window blocks: even blocks fit, odd blocks calibrate the threshold")
    w(f"  (the {THRESHOLD_PERCENTILE:.0f}th percentile of held-out baseline scores).")
    w(f"- Recording verdict: `anomaly_suspected` when at least 3 windows and {FRACTION_THRESHOLD:.0%} of windows exceed the threshold.")
    w("  `not_assessed` for short, flat, clipped, wrong-rate or non-finite input; never a clean verdict on bad input.")
    w("- A small numpy autoencoder was **not** built: with 10 features and under 120 fit windows per engine there was no data to")
    w("  justify it, and it would add a second thing to explain. Mahalanobis only.")
    w("")
    w("## Data and provenance")
    w("")
    w(f"- Conditions: RPM {', '.join(map(str, RPMS))}; extra Gaussian noise {', '.join(f'{x:g}' for x in EXTRA_NOISE_MG)} mg per axis")
    w("  on top of the simulator's own 5 mg (a 12-condition grid).")
    w(f"- Per condition: one {BASELINE_S:.0f} s healthy baseline (the only thing the model sees), then {HEALTHY_PER_CELL} healthy and")
    w(f"  {MISFIRE_PER_LEVEL} misfiring {TEST_S:.0f} s test recordings at each of `--misfire-every` {', '.join(map(str, MISFIRE_EVERY))}.")
    w(f"- Total: {len(healthy)} healthy and {len(faulty)} misfiring test recordings. Seeds start at {HELD_OUT_SEED_BASE}; all constants")
    w("  (window length, bands, percentile, fraction threshold) were chosen on seeds below 100 and not changed after looking at these.")
    w("- CC0 engine clips: none evaluated. `samples/` was absent when this report was generated; the audio features are")
    w("  unit-tested on synthetic audio only. Clips are audio-only and the v1 model needs a vibration baseline, so they could not")
    w("  be scored without a new audio-only mode.")
    w("")
    w("## Headline (all conditions pooled)")
    w("")
    w("| Metric | Value |")
    w("|---|---|")
    w(f"| Window-level ROC-AUC (healthy vs misfiring windows, score / threshold) | {win_auc:.3f} |")
    w(f"| Recording-level ROC-AUC (score = fraction of anomalous windows) | {rec_auc:.3f} |")
    w(f"| Recording detection rate at the chosen threshold (misfiring flagged) | {_pct(_rate(faulty))} |")
    w(f"| Recording false-alarm rate (healthy flagged) | {_pct(_rate(healthy))} |")
    w(f"| Window false-positive rate / true-positive rate at threshold | {win_fpr:.1%} / {win_tpr:.1%} |")
    w(f"| Recordings `not_assessed` | {len(na)} of {len(rows)} |")
    w("")
    w("## By misfire severity (pooled over rpm and noise)")
    w("")
    w("`--misfire-every N`: lower N = more frequent misfire. Healthy rows are the shared comparison set.")
    w("")
    w("| misfire-every | recordings | window AUC | recording AUC | detection rate |")
    w("|---|---|---|---|---|")
    for n in MISFIRE_EVERY:
        sub = [r for r in faulty if r["n"] == n]
        a, b = _auc_of(healthy, sub)
        w(f"| {n} | {len(sub)} | {a:.3f} | {b:.3f} | {_pct(_rate(sub))} |")
    w("")
    w("## By RPM (pooled over noise and severity)")
    w("")
    w("| rpm | firing Hz | window AUC | detection rate | false-alarm rate |")
    w("|---|---|---|---|---|")
    for rpm in RPMS:
        h = [r for r in healthy if r["rpm"] == rpm]
        f = [r for r in faulty if r["rpm"] == rpm]
        a, _ = _auc_of(h, f)
        w(f"| {rpm} | {rpm / 30:.0f} | {a:.3f} | {_pct(_rate(f))} | {_pct(_rate(h))} |")
    w("")
    w("## By extra noise (pooled over rpm and severity)")
    w("")
    w("| extra noise (mg) | window AUC | detection rate | false-alarm rate |")
    w("|---|---|---|---|")
    for nz in EXTRA_NOISE_MG:
        h = [r for r in healthy if r["noise"] == nz]
        f = [r for r in faulty if r["noise"] == nz]
        a, _ = _auc_of(h, f)
        w(f"| {nz:g} | {a:.3f} | {_pct(_rate(f))} | {_pct(_rate(h))} |")
    w("")
    w("## Detection rate by condition and severity")
    w("")
    w("| rpm | noise (mg) | false alarm | " + " | ".join(f"N={n}" for n in MISFIRE_EVERY) + " |")
    w("|---|---|---|" + "---|" * len(MISFIRE_EVERY))
    for rpm in RPMS:
        for nz in EXTRA_NOISE_MG:
            cell = [r for r in rows if r["rpm"] == rpm and r["noise"] == nz]
            fa = _rate([r for r in cell if r["n"] == 0])
            cols = [_pct(_rate([r for r in cell if r["n"] == n])) for n in MISFIRE_EVERY]
            w(f"| {rpm} | {nz:g} | {_pct(fa)} | " + " | ".join(cols) + " |")
    w("")
    w("## Limits")
    w("")
    w("- Near-perfect detection mostly shows that the simulator's misfire (a pulse cut to 8 % amplitude) is a large, clean change. Treat the")
    w("  false-alarm column, not the detection column, as the informative one.")
    w("- Synthetic fault, synthetic engine, one fault family. The simulator misfire is a dropped pulse; real misfires, knock,")
    w("  worn bearings, belts and mounts look different, and a real engine cover resonates and drifts.")
    w("- The model compares against *this engine's* baseline at the baseline's speed. A baseline taken at a different RPM, or")
    w("  not healthy, makes the verdict meaningless; the model cannot detect that.")
    w("- High RPM at a 400 Hz sample rate leaves 4 samples per firing and a Nyquist of 200 Hz (2x firing is at the limit): see the RPM table.")
    w("- Model confidence is a distance, not a probability. Thresholds are calibrated to about a 1 % window false-positive rate on")
    w("  the baseline's own held-out blocks; the false-alarm rates above are from synthetic data with a stationary engine.")
    w("- Audio features exist and are tested on synthetic audio but are not part of these numbers.")
    w("")
    return "\n".join(out)


def write_report(path: str | Path) -> str:
    text = build_report(run_cells())
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8", newline="\n")
    return text
