"""Live Sense -> Understand -> Act loop (H17).

Reads the pod stream, feeds the H16 `StreamScorer`, and sends state commands
back so the pod itself alerts the inspector (OLED, LED, buzzer).

Host -> pod commands (one line each; the pod answers `#ack,<body>`):

    !S,L<seconds_left>   baseline running
    !S,N                 normal
    !S,A                 anomaly suspected
    !S,X                 not assessed (bad input or failed baseline)
    !M,<0|1>             buzzer mute

The pod reverts to idle if no state command arrives for 5 s, so the state is
re-sent about once a second of stream time.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .anomaly import AnomalyError, StreamScorer
from .anomaly.features import accel_from_records
from .anomaly.model import assess_arrays
from .protocol import HEADER_LINE, PROTOCOL_VERSION, ProtocolError, Record, parse_header, parse_line
from .recording import make_header, write_recording
from .serial_io import send_line
from .simulator import SimConfig, simulate

STATE_RESEND_MS = 1000
BATCH_MS = 100  # feed the scorer this often (stream time)
POD_STATES = ("L", "N", "A", "X")


# --- command encoding ------------------------------------------------------

def encode_state(state: str, seconds_left: int | None = None) -> str:
    if state == "L":
        if seconds_left is None or not 0 <= seconds_left <= 999:
            raise ValueError("learning state needs seconds_left in 0..999")
        return f"!S,L{seconds_left}"
    if state in ("N", "A", "X"):
        return f"!S,{state}"
    raise ValueError(f"unknown pod state {state!r}")


def encode_mute(muted: bool) -> str:
    return f"!M,{1 if muted else 0}"


# --- simulated pod ---------------------------------------------------------

def format_line(r: Record) -> str:
    return f"{r.kind},{r.t_ms}," + ",".join(f"{v:g}" if r.kind == "T" else str(int(v)) for v in r.values)


def scripted_simulation(
    seconds: float, rpm: float = 780.0, misfire_at: float | None = 35.0, misfire_for: float = 12.0,
    misfire_every: int = 3, seed: int = 100,
) -> list[Record]:
    """Healthy engine with a misfire between `misfire_at` and `misfire_at + misfire_for` seconds. SYNTHETIC."""
    if misfire_at is None or misfire_at >= seconds:
        return simulate(SimConfig(seconds=seconds, rpm=rpm, seed=seed))
    end = min(seconds, misfire_at + misfire_for)
    parts = [(0.0, misfire_at, 0), (misfire_at, end, misfire_every)]
    if end < seconds:
        parts.append((end, seconds, 0))
    out: list[Record] = []
    for i, (a, b, n) in enumerate(parts):
        for r in simulate(SimConfig(seconds=b - a, rpm=rpm, misfire_every=n, seed=seed + i)):
            out.append(Record(r.kind, r.t_ms + int(round(a * 1000)), r.values))
    order = {"A": 0, "S": 1, "T": 2, "B": 3, "K": 4}
    out.sort(key=lambda r: (r.t_ms, order[r.kind]))
    return out


class SimPort:
    """Duck-typed stand-in for the serial port: emits the pod line protocol from
    simulator records (optionally paced in real time) and records commands.
    Like the pod, it acknowledges well-formed commands with `#ack,<body>`."""

    def __init__(self, records: list[Record], speed: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._lines = [HEADER_LINE] + [format_line(r) for r in records]
        self._ts = [0] + [r.t_ms for r in records]
        self._i = 0
        self._speed = speed
        self._clock, self._sleep = clock, sleep
        self._t0: float | None = None
        self._acks: list[str] = []
        self.sent: list[str] = []
        self.eof = False

    def readline(self) -> bytes:
        if self._acks:
            return (self._acks.pop(0) + "\n").encode("ascii")
        if self._i >= len(self._lines):
            self.eof = True
            return b""
        if self._speed > 0:
            now = self._clock()
            if self._t0 is None:
                self._t0 = now
            wait = self._t0 + self._ts[self._i] / 1000.0 / self._speed - now
            if wait > 0.005:
                self._sleep(wait)
        line = self._lines[self._i]
        self._i += 1
        return (line + "\n").encode("ascii")

    def write(self, data: bytes) -> int:
        for raw in data.decode("ascii").splitlines():
            self.sent.append(raw)
            if raw.startswith("!") and len(raw) >= 4 and raw[2] == ",":
                self._acks.append(f"#ack,{raw[1]},{raw[3:]}")
        return len(data)


# --- the loop --------------------------------------------------------------

@dataclass
class LiveResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    dropped_lines: int = 0
    acks: int = 0
    verdict: dict[str, Any] | None = None
    verdict_path: str | None = None
    recording_path: str | None = None


def build_verdict(scorer: StreamScorer, records: list[Record], source: str) -> dict[str, Any] | None:
    """Verdict over the post-baseline part of the recording; run times are
    relative to the recording start. None if the baseline never completed."""
    model = scorer.model
    if model is None or scorer.state == "baselining":
        return None
    xyz, fs = accel_from_records(records)
    post = xyz[scorer.baseline_n:]
    v = assess_arrays(model, post, fs, None, source)
    offset = scorer.baseline_n / fs
    for run in v["anomalous_runs"]:
        run["t_start_s"] = round(run["t_start_s"] + offset, 2)
        run["t_end_s"] = round(run["t_end_s"] + offset, 2)
    v["duration_s"] = round(len(xyz) / fs, 2)
    return v


def run_live(
    port: Any, *, baseline_s: float = 20.0, record_path: str | Path | None = None,
    verdict_path: str | Path | None = None, mute: bool = False, max_wall_s: float | None = None,
    source: str = "board", out: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic,
) -> LiveResult:
    res = LiveResult()
    scorer = StreamScorer(baseline_s=baseline_s)
    wall_end = None if max_wall_s is None else clock() + max_wall_s
    seen_header = False
    batch: list[Record] = []
    t_first: int | None = None
    t_batch = 0
    last_sent_ms: int | None = None
    last_state: str | None = None

    def send(cmd: str) -> None:
        send_line(port, cmd)
        res.commands.append(cmd)
        out(f"  -> pod: {cmd}")

    def state_cmd(t_ms: int) -> str:
        st = scorer.pod_state
        if st == "L":
            left = baseline_s - ((t_ms - (t_ms if t_first is None else t_first)) / 1000.0)
            return encode_state("L", max(0, min(999, math.ceil(left))))
        return encode_state(st)

    if mute:
        send(encode_mute(True))
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        while wall_end is None or clock() < wall_end:
            raw = port.readline()
            if not raw:
                if getattr(port, "eof", False):
                    break
                continue
            line = raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else raw
            if not seen_header:
                try:
                    ver = parse_header(line)
                except ProtocolError:
                    continue
                if ver is None:
                    continue  # boot noise before the header
                if ver != PROTOCOL_VERSION:
                    raise ProtocolError(f"unsupported protocol v{ver}")
                seen_header = True
                continue
            if line.startswith("#ack"):
                res.acks += 1
                continue
            try:
                rec = parse_line(line)
            except ProtocolError:
                res.dropped_lines += 1  # garbled line: counted, skipped
                continue
            if rec is None:
                continue
            res.records.append(rec)
            batch.append(rec)
            if rec.kind != "A":
                continue
            if t_first is None:
                t_first = rec.t_ms
                t_batch = rec.t_ms
            if rec.t_ms - t_batch < BATCH_MS:
                continue
            t_batch = rec.t_ms
            for ev in scorer.push(batch):
                res.events.append(ev)
                extra = " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("event", "t_ms", "top_features"))
                top = ev.get("top_features")
                top_s = f" top={top[0]['name']}" if top else ""
                out(f"[{ev['t_ms'] / 1000.0:7.2f}s] {ev['event']} {extra}{top_s}")
            batch = []
            st = scorer.pod_state
            if st != last_state or last_sent_ms is None or rec.t_ms - last_sent_ms >= STATE_RESEND_MS:
                send(state_cmd(rec.t_ms))
                last_state, last_sent_ms = st, rec.t_ms
    except KeyboardInterrupt:
        out("interrupted")
    if batch:
        for ev in scorer.push(batch):
            res.events.append(ev)

    if record_path:
        hdr = make_header(source, started_at, live=True, baseline_s=baseline_s)
        hdr["dropped_lines"] = res.dropped_lines
        write_recording(record_path, hdr, res.records)
        res.recording_path = str(record_path)
        out(f"wrote {record_path}: {len(res.records)} records, {res.dropped_lines} garbled lines skipped")
        if verdict_path is None:
            verdict_path = Path(record_path).with_suffix(".verdict.json")
    if verdict_path:
        try:
            res.verdict = build_verdict(scorer, res.records, source)
        except AnomalyError as exc:
            out(f"no verdict: {exc}")
        if res.verdict is None:
            out("no verdict written: the baseline did not complete")
        else:
            Path(verdict_path).write_text(json.dumps(res.verdict, indent=2), encoding="utf-8", newline="\n")
            res.verdict_path = str(verdict_path)
            out(f"wrote {verdict_path}: {res.verdict['verdict']}"
                f"{'' if res.verdict['reason'] is None else ' (' + res.verdict['reason'] + ')'}")
    return res
