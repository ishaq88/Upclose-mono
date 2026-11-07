"""Fit / score / save / load. Robust standardization + shrinkage Mahalanobis.

Models are plain JSON (no pickle). Everything is relative to the healthy
baseline the model was fitted on: it says "unlike this engine's baseline",
not "faulty".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ..protocol import Record
from .features import (
    AUDIO_FEATURES, AUDIO_SR, VIB_FEATURES, VIB_MIN_SCALE, AnomalyError, accel_from_records, audio_features,
    estimate_fire_hz, load_wav, n_windows, principal_direction, quality_problem, resample_16k,
    vib_features, window_shape,
)

MODEL_SCHEMA = "upclose-anomaly-model/1"
VERDICT_SCHEMA = "upclose-anomaly-verdict/1"
MIN_BASELINE_S = 20.0
MIN_SCORE_WINDOWS = 11  # about 3 s
BLOCK = 8  # windows per fit/calibration block (overlapping windows are correlated)
THRESHOLD_PERCENTILE = 99.0
FRACTION_THRESHOLD = 0.15
MIN_ANOMALOUS_WINDOWS = 3
MIN_SHRINKAGE = 0.05
AUDIO_MIN_SCALE = 0.25
RATE_TOLERANCE = 0.2

VERDICTS = ("anomaly_suspected", "no_anomaly_detected", "not_assessed")


@dataclass
class AnomalyModel:
    fs_hz: float
    fire_hz: float
    direction: list[float]
    feature_names: list[str]
    has_audio: bool
    median: np.ndarray
    scale: np.ndarray
    center: np.ndarray  # mean of standardized fit windows
    precision: np.ndarray
    shrinkage: float
    threshold: float
    threshold_percentile: float
    fraction_threshold: float
    n_fit_windows: int
    n_calibration_windows: int
    baseline_seconds: float
    baseline_source: str
    extra: dict[str, Any] = field(default_factory=dict)

    # --- persistence ---
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MODEL_SCHEMA,
            "fs_hz": self.fs_hz, "fire_hz": self.fire_hz, "direction": self.direction,
            "feature_names": self.feature_names, "has_audio": self.has_audio,
            "median": self.median.tolist(), "scale": self.scale.tolist(), "center": self.center.tolist(),
            "precision": self.precision.tolist(), "shrinkage": self.shrinkage,
            "threshold": self.threshold, "threshold_percentile": self.threshold_percentile,
            "fraction_threshold": self.fraction_threshold,
            "n_fit_windows": self.n_fit_windows, "n_calibration_windows": self.n_calibration_windows,
            "baseline_seconds": self.baseline_seconds, "baseline_source": self.baseline_source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AnomalyModel":
        if d.get("schema") != MODEL_SCHEMA:
            raise AnomalyError(f"not an {MODEL_SCHEMA} model")
        try:
            m = cls(
                fs_hz=float(d["fs_hz"]), fire_hz=float(d["fire_hz"]), direction=[float(v) for v in d["direction"]],
                feature_names=[str(s) for s in d["feature_names"]], has_audio=bool(d["has_audio"]),
                median=np.array(d["median"], float), scale=np.array(d["scale"], float),
                center=np.array(d["center"], float), precision=np.array(d["precision"], float),
                shrinkage=float(d["shrinkage"]), threshold=float(d["threshold"]),
                threshold_percentile=float(d["threshold_percentile"]),
                fraction_threshold=float(d["fraction_threshold"]),
                n_fit_windows=int(d["n_fit_windows"]), n_calibration_windows=int(d["n_calibration_windows"]),
                baseline_seconds=float(d["baseline_seconds"]), baseline_source=str(d["baseline_source"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AnomalyError(f"bad model file: {exc}") from exc
        p = len(m.feature_names)
        if m.median.shape != (p,) or m.scale.shape != (p,) or m.center.shape != (p,) or m.precision.shape != (p, p):
            raise AnomalyError("bad model file: inconsistent dimensions")
        return m

    def config_hash(self) -> str:
        """sha256 of the canonical model JSON: identifies the exact fitted model."""
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), separators=(",", ":")), encoding="utf-8", newline="\n")

    @classmethod
    def load(cls, path: str | Path) -> "AnomalyModel":
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise AnomalyError(f"{path}: not JSON") from exc

    # --- scoring ---
    def z(self, feats: np.ndarray) -> np.ndarray:
        return (feats - self.median) / self.scale - self.center

    def score(self, feats: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(distance per window, per-feature contribution to distance^2)."""
        z = self.z(feats)
        pz = z @ self.precision
        contrib = z * pz
        d2 = np.maximum(contrib.sum(axis=1), 0.0)
        return np.sqrt(d2), contrib


def _ledoit_wolf(z: np.ndarray) -> float:
    """Shrinkage intensity toward a scaled identity (Ledoit-Wolf 2004)."""
    n, p = z.shape
    x = z - z.mean(axis=0)
    s = x.T @ x / n
    mu = np.trace(s) / p
    d2 = float(((s - mu * np.eye(p)) ** 2).sum())
    if d2 <= 0:
        return 1.0
    b2 = (float(((x ** 2).sum(axis=1) ** 2).sum()) - n * float((s ** 2).sum())) / (n * n)
    return float(min(max(b2, 0.0), d2) / d2)


def _prepare_audio(audio: tuple[np.ndarray, int] | None) -> np.ndarray | None:
    if audio is None:
        return None
    samples, sr = audio
    return resample_16k(np.asarray(samples, float), int(sr))


def _features(direction: np.ndarray, f0: float, xyz: np.ndarray, fs: float, a16: np.ndarray | None) -> np.ndarray:
    sig = xyz @ direction
    f = vib_features(sig, fs, f0)
    if a16 is not None:
        f = np.concatenate([f, audio_features(a16, len(f), f0)], axis=1)
    return f


def fit_arrays(xyz: np.ndarray, fs: float, audio: tuple[np.ndarray, int] | None = None, source: str = "unknown") -> AnomalyModel:
    if len(xyz) < MIN_BASELINE_S * fs * 0.98:
        raise AnomalyError(f"baseline_too_short: need at least {MIN_BASELINE_S:.0f} s of healthy data")
    problem = quality_problem(xyz)
    if problem:
        raise AnomalyError(f"baseline_unusable: {problem}")
    direction = principal_direction(xyz)
    f0 = estimate_fire_hz(xyz @ direction, fs)
    a16 = _prepare_audio(audio)
    feats = _features(direction, f0, xyz, fs, a16)
    names = list(VIB_FEATURES) + (list(AUDIO_FEATURES) if a16 is not None else [])
    nw = len(feats)
    block = np.arange(nw) // BLOCK
    fit_rows, cal_rows = feats[block % 2 == 0], feats[block % 2 == 1]
    if len(fit_rows) < 30 or len(cal_rows) < 30:
        raise AnomalyError("baseline_too_short: too few windows for fit and calibration")
    median = np.median(fit_rows, axis=0)
    mad = np.median(np.abs(fit_rows - median), axis=0) * 1.4826
    floor = np.concatenate([np.array(VIB_MIN_SCALE), np.full(len(names) - len(VIB_MIN_SCALE), AUDIO_MIN_SCALE)])
    scale = np.maximum(mad, floor)
    zf = (fit_rows - median) / scale
    center = zf.mean(axis=0)
    lam = max(_ledoit_wolf(zf), MIN_SHRINKAGE)
    xc = zf - center
    s = xc.T @ xc / len(xc)
    p = s.shape[0]
    cov = (1.0 - lam) * s + lam * (np.trace(s) / p) * np.eye(p)
    precision = np.linalg.inv(cov)
    model = AnomalyModel(
        fs_hz=float(fs), fire_hz=float(f0), direction=[float(v) for v in direction], feature_names=names,
        has_audio=a16 is not None, median=median, scale=scale, center=center, precision=precision,
        shrinkage=float(lam), threshold=0.0, threshold_percentile=THRESHOLD_PERCENTILE,
        fraction_threshold=FRACTION_THRESHOLD, n_fit_windows=len(fit_rows), n_calibration_windows=len(cal_rows),
        baseline_seconds=float(len(xyz) / fs), baseline_source=source,
    )
    model.threshold = float(np.percentile(model.score(cal_rows)[0], THRESHOLD_PERCENTILE))
    return model


def fit_from_records(records: Sequence[Record], audio_path: str | Path | None = None, source: str = "unknown") -> AnomalyModel:
    xyz, fs = accel_from_records(records)
    audio = load_wav(audio_path) if audio_path else None
    return fit_arrays(xyz, fs, audio, source)


def _top_features(model: AnomalyModel, contrib: np.ndarray, zs: np.ndarray | None = None, k: int = 3) -> list[dict[str, Any]]:
    pos = np.maximum(contrib, 0.0)
    tot = float(pos.sum())
    order = np.argsort(-pos, kind="stable")[:k]
    out = []
    for i in order:
        if pos[i] <= 0:
            continue
        item: dict[str, Any] = {"name": model.feature_names[i], "share": round(float(pos[i] / tot), 3)}
        if zs is not None:
            item["z"] = round(float(zs[i]), 2)
        out.append(item)
    return out


def _not_assessed(model: AnomalyModel, reason: str, source: str, duration_s: float = 0.0) -> dict[str, Any]:
    return {
        "schema": VERDICT_SCHEMA, "verdict": "not_assessed", "reason": reason, "data_source": source,
        "duration_s": round(duration_s, 2), "windows": None, "anomalous_windows": None, "anomalous_fraction": None,
        "max_score": None, "median_score": None, "threshold": round(model.threshold, 4),
        "fraction_threshold": model.fraction_threshold, "min_anomalous_windows": MIN_ANOMALOUS_WINDOWS,
        "top_features": [], "anomalous_runs": [],
        "model": _model_info(model),
        "note": "relative to the baseline this model was fitted on; not a diagnosis and not calibrated accuracy",
    }


def _model_info(model: AnomalyModel) -> dict[str, Any]:
    return {
        "fire_hz": round(model.fire_hz, 3), "fs_hz": round(model.fs_hz, 2), "has_audio": model.has_audio,
        "baseline_seconds": round(model.baseline_seconds, 1), "baseline_source": model.baseline_source,
        "threshold_percentile": model.threshold_percentile, "config_hash": model.config_hash(),
    }


def assess_arrays(model: AnomalyModel, xyz: np.ndarray, fs: float, audio: tuple[np.ndarray, int] | None = None,
                  source: str = "unknown") -> dict[str, Any]:
    """Recording-level verdict. Never returns a clean verdict on bad input."""
    duration = len(xyz) / fs if fs > 0 else 0.0
    if abs(fs - model.fs_hz) > RATE_TOLERANCE * model.fs_hz:
        return _not_assessed(model, "sample_rate_mismatch", source, duration)
    problem = quality_problem(xyz)
    if problem:
        return _not_assessed(model, problem, source, duration)
    if n_windows(len(xyz), fs) < MIN_SCORE_WINDOWS:
        return _not_assessed(model, "too_short", source, duration)
    a16 = _prepare_audio(audio)
    if model.has_audio and a16 is None:
        return _not_assessed(model, "audio_required_by_model", source, duration)
    try:
        feats = _features(np.array(model.direction), model.fire_hz, xyz, fs, a16 if model.has_audio else None)
    except AnomalyError as exc:
        return _not_assessed(model, str(exc).split(":")[0], source, duration)
    if not np.all(np.isfinite(feats)):
        return _not_assessed(model, "non_finite_features", source, duration)
    scores, contrib = model.score(feats)
    flags = scores > model.threshold
    k = int(flags.sum())
    frac = k / len(scores)
    suspected = k >= MIN_ANOMALOUS_WINDOWS and frac >= model.fraction_threshold
    w, h = window_shape(fs)
    zs = model.z(feats)
    runs: list[dict[str, Any]] = []
    i = 0
    while i < len(flags):
        if not flags[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(flags) and flags[j + 1]:
            j += 1
        peak = i + int(np.argmax(scores[i: j + 1]))
        runs.append({
            "t_start_s": round(i * h / fs, 2), "t_end_s": round((j * h + w) / fs, 2),
            "peak_score": round(float(scores[peak]), 3),
            "top_features": _top_features(model, contrib[peak], zs[peak]),
        })
        i = j + 1
    agg = contrib[flags].sum(axis=0) if k else np.zeros(contrib.shape[1])
    return {
        "schema": VERDICT_SCHEMA,
        "verdict": "anomaly_suspected" if suspected else "no_anomaly_detected",
        "reason": None, "data_source": source, "duration_s": round(duration, 2),
        "windows": int(len(scores)), "anomalous_windows": k, "anomalous_fraction": round(frac, 4),
        "max_score": round(float(scores.max()), 3), "median_score": round(float(np.median(scores)), 3),
        "threshold": round(model.threshold, 4), "fraction_threshold": model.fraction_threshold,
        "min_anomalous_windows": MIN_ANOMALOUS_WINDOWS,
        "top_features": _top_features(model, agg) if suspected else [],
        "anomalous_runs": runs[:10],
        "model": _model_info(model),
        "note": "relative to the baseline this model was fitted on; not a diagnosis and not calibrated accuracy",
    }


def assess_records(model: AnomalyModel, records: Sequence[Record], audio_path: str | Path | None = None,
                   source: str = "unknown") -> dict[str, Any]:
    try:
        xyz, fs = accel_from_records(records)
    except AnomalyError as exc:
        return _not_assessed(model, str(exc).split(":")[0], source)
    audio = load_wav(audio_path) if audio_path else None
    return assess_arrays(model, xyz, fs, audio, source)
