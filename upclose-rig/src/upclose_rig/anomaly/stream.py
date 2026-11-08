"""Streaming scorer for the bridge: baseline the first N seconds, then score live.

Feed it parsed pod records as they arrive (``push``). It returns events the
"Act" step can send back to the pod. Vibration only (no live audio).

Event dicts: ``{"event": <name>, "t_ms": int, ...}`` with names
``baseline_ready``, ``anomaly_suspected``, ``anomaly_cleared``, ``not_assessed``.
Assumes the baseline period is a healthy engine at the working speed; if it
was not, the model is wrong and the scorer cannot know.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

import numpy as np

from ..protocol import Record
from .features import AnomalyError, quality_problem, vib_features, window_shape
from .model import AnomalyModel, _top_features, fit_arrays

RECOVERY_SAMPLES = 400  # about 1 s of clean signal before not_assessed is lifted
QUALITY_SAMPLES = 800   # about 2 s at 400 Hz
MIN_MEAN_RATIO = 1.5   # mean score/threshold of the flagged windows needed to alert
ON_COUNT, ON_OF = 3, 5   # suspected when 3 of the last 5 windows are anomalous
OFF_COUNT = 8            # cleared after 8 consecutive normal windows


class StreamScorer:
    def __init__(self, baseline_s: float = 30.0, model: AnomalyModel | None = None) -> None:
        if model is None and baseline_s < 20.0:
            raise ValueError("baseline_s must be at least 20 s")
        self.baseline_s = baseline_s
        self.model = model
        self.state = "scoring" if model else "baselining"
        self.suspected = False
        self._t: list[int] = []
        self._xyz: list[tuple[float, ...]] = []
        self._qbuf = np.zeros((0, 3))  # last ~2 s, for the quality check (a 0.5 s window is too short for it)
        self._buf = np.zeros((0, 3))  # samples not yet fully consumed by a window
        self._t0: int | None = None
        self._recent: deque[bool] = deque(maxlen=ON_OF)
        self._recent_ratio: deque[float] = deque(maxlen=ON_OF)  # score / threshold of the recent windows
        self._normal_run = 0
        self.problem: str | None = None  # input quality problem right now (flat/clipped), else None
        self._clean_samples = 0
        self.baseline_n = 0              # accel samples consumed by the baseline fit

    @property
    def pod_state(self) -> str:
        """The one source of truth for what the pod should show: L learning,
        X not assessed (held until quality recovers), A anomaly, N normal."""
        if self.state == "baselining":
            return "L"
        if self.state == "failed" or self.problem:
            return "X"
        return "A" if self.suspected else "N"

    def push(self, records: Iterable[Record]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        new: list[tuple[float, ...]] = []
        last_t = 0
        for r in records:
            if r.kind != "A":
                continue
            if self._t0 is None:
                self._t0 = r.t_ms
            new.append(r.values)
            last_t = r.t_ms
            if self.state == "baselining":
                self._t.append(r.t_ms)
        if not new or self.state == "failed":
            return events
        arr = np.array(new, float)
        if self.state == "baselining":
            self._xyz.extend(new)
            if (self._t[-1] - self._t[0]) / 1000.0 >= self.baseline_s:
                events.extend(self._finish_baseline(last_t))
            return events
        events.extend(self._score(arr, last_t))
        return events

    def _finish_baseline(self, t_ms: int) -> list[dict[str, Any]]:
        xyz = np.array(self._xyz, float)
        fs = (len(self._t) - 1) / ((self._t[-1] - self._t[0]) / 1000.0)
        self.baseline_n = len(xyz)
        self._xyz = []
        self._t = []
        try:
            self.model = fit_arrays(xyz, fs, None, "stream")
        except AnomalyError as exc:
            self.state = "failed"
            return [{"event": "not_assessed", "t_ms": t_ms, "reason": str(exc).split(":")[0]}]
        self.state = "scoring"
        return [{"event": "baseline_ready", "t_ms": t_ms, "fire_hz": round(self.model.fire_hz, 2),
                 "threshold": round(self.model.threshold, 3)}]

    def _strong_enough(self) -> bool:
        """Marginal exceedances (a few % over the threshold) are what a healthy engine
        produces by chance; require the flagged windows to be clearly above it."""
        hot = [r for f, r in zip(self._recent, self._recent_ratio) if f]
        return bool(hot) and sum(hot) / len(hot) >= MIN_MEAN_RATIO

    def _score(self, arr: np.ndarray, t_ms: int) -> list[dict[str, Any]]:
        m = self.model
        assert m is not None
        events: list[dict[str, Any]] = []
        w, h = window_shape(m.fs_hz)
        self._buf = np.concatenate([self._buf, arr])
        self._qbuf = np.concatenate([self._qbuf, arr])[-QUALITY_SAMPLES:]
        if len(self._buf) < w:
            return events
        problem = quality_problem(self._qbuf)
        if problem:
            was_ok = self.problem is None
            self.problem = problem  # latched: "not assessed" holds until the signal has been clean for a while
            self._clean_samples = 0
            self._buf = self._buf[-w:]
            self._recent.clear()
            self._recent_ratio.clear()
            if was_ok:
                events.append({"event": "not_assessed", "t_ms": t_ms, "reason": problem})
            return events
        if self.problem is not None:
            self._clean_samples += len(arr)
            if self._clean_samples < RECOVERY_SAMPLES:
                self._buf = self._buf[-w:]
                return events
            self.problem = None
        feats = vib_features(self._buf @ np.array(m.direction), m.fs_hz, m.fire_hz)
        nw = len(feats)
        scores, contrib = m.score(feats)
        zs = m.z(feats)
        consumed = nw * h
        self._buf = self._buf[consumed:]
        for i in range(nw):
            flag = bool(scores[i] > m.threshold)
            self._recent.append(flag)
            self._recent_ratio.append(float(scores[i] / m.threshold))
            self._normal_run = 0 if flag else self._normal_run + 1
            if not self.suspected and sum(self._recent) >= ON_COUNT and self._strong_enough():
                self.suspected = True
                events.append({"event": "anomaly_suspected", "t_ms": t_ms, "score": round(float(scores[i]), 3),
                               "threshold": round(m.threshold, 3), "top_features": _top_features(m, contrib[i], zs[i])})
            elif self.suspected and self._normal_run >= OFF_COUNT:
                self.suspected = False
                self._recent.clear()
                self._recent_ratio.clear()
                events.append({"event": "anomaly_cleared", "t_ms": t_ms})
        return events
