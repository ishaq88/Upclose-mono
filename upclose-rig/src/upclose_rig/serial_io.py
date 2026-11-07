"""Serial capture from the pod. `serial_for_url` also accepts loop://, socket://."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator

from .protocol import BAUD, ProtocolError, Record, parse_stream
from .recording import Recording, make_header

Clock = Callable[[], float]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_port(port: str, baud: int = BAUD, timeout: float = 0.5):  # type: ignore[no-untyped-def]
    import serial

    return serial.serial_for_url(port, baudrate=baud, timeout=timeout)


def iter_serial_lines(ser, max_wall_s: float | None = None, clock: Clock = time.monotonic) -> Iterator[str]:  # type: ignore[no-untyped-def]
    """Yield decoded lines; yields '' on read timeout so callers can check deadlines."""
    end = None if max_wall_s is None else clock() + max_wall_s
    while end is None or clock() < end:
        raw = ser.readline()
        yield raw.decode("ascii", errors="replace") if raw else ""


def capture(lines: Iterable[str], seconds: float, clock: Clock = time.monotonic) -> tuple[Recording, int]:
    """Collect records until `seconds` have elapsed since the first record.

    Garbled lines are counted and skipped. Raises HeaderMissing if the stream
    has no protocol header. Returns (recording, dropped_line_count).
    """
    records: list[Record] = []
    dropped = 0
    t_start: float | None = None
    started_at = ""
    for item in parse_stream(lines):
        if isinstance(item, ProtocolError):
            dropped += 1
            continue
        if t_start is None:
            t_start = clock()
            started_at = _now_iso()
        if clock() - t_start >= seconds:
            break
        records.append(item)
    rec = Recording(make_header("board", started_at or _now_iso()), records)
    rec.header["dropped_lines"] = dropped
    return rec, dropped


def monitor(lines: Iterable[str], out=sys.stdout, clock: Clock = time.monotonic) -> None:  # type: ignore[no-untyped-def]
    """Print the latest parsed values twice a second plus per-type rates."""
    last: dict[str, tuple[float, ...]] = {}
    counts: dict[str, int] = {}
    bad = 0
    t_mark = clock()
    for item in parse_stream(lines):
        if isinstance(item, ProtocolError):
            bad += 1
        else:
            last[item.kind] = item.values
            counts[item.kind] = counts.get(item.kind, 0) + 1
        now = clock()
        if now - t_mark >= 0.5:
            dt = now - t_mark
            parts = [f"{k}={'/'.join(f'{v:g}' for v in vals)}" for k, vals in sorted(last.items())]
            rates = " ".join(f"{k}:{counts.get(k, 0) / dt:.0f}/s" for k in sorted(counts))
            print(f"{' '.join(parts)} | {rates} | bad={bad}", file=out, flush=True)
            counts.clear()
            t_mark = now


def send_line(ser, text: str) -> None:  # type: ignore[no-untyped-def]
    """Write one host -> pod command line (ASCII, newline-terminated)."""
    if "\n" in text or "\r" in text:
        raise ValueError("a command is a single line")
    ser.write(text.encode("ascii") + b"\n")
    flush = getattr(ser, "flush", None)
    if flush:
        flush()
