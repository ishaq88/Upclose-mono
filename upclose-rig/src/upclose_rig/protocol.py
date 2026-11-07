"""Pod line protocol v1 (see README). Pure parsing, no I/O.

Records are ``(kind, t_ms, values)`` tuples:

    A,t,x_mg,y_mg,z_mg   accelerometer (ints)
    S,t,level            sound (int 0..1023)
    T,t,celsius,rh       temperature (floats)
    B,t,0|1              button edge
    K,t,0..1023          knob
"""

from __future__ import annotations

import math
from typing import Iterable, Iterator, NamedTuple

PROTOCOL_NAME = "upclose-pod"
PROTOCOL_VERSION = 1
HEADER_LINE = f"#{PROTOCOL_NAME} v{PROTOCOL_VERSION}"
BAUD = 250000
HEADER_SEARCH_LINES = 200  # board reboots on connect; allow some boot noise


class Record(NamedTuple):
    kind: str
    t_ms: int
    values: tuple[float, ...]


class ProtocolError(ValueError):
    """A line (or stream) does not follow the pod protocol."""


class HeaderMissing(ProtocolError):
    """No `#upclose-pod vN` header found."""


_FIELDS = {"A": 3, "S": 1, "T": 2, "B": 1, "K": 1}


def parse_line(line: str) -> Record | None:
    """Parse one data line. Returns None for blank/comment lines.

    Raises ProtocolError for garbled data lines.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(",")
    kind = parts[0]
    if kind not in _FIELDS:
        raise ProtocolError(f"unknown record type {kind!r}")
    if len(parts) != 2 + _FIELDS[kind]:
        raise ProtocolError(f"{kind}: expected {2 + _FIELDS[kind]} fields, got {len(parts)}")
    try:
        t_ms = int(parts[1])
        if kind == "T":
            vals: tuple[float, ...] = tuple(float(p) for p in parts[2:])
            if not all(math.isfinite(v) for v in vals):
                raise ValueError("non-finite")
        else:
            vals = tuple(int(p) for p in parts[2:])
    except ValueError as exc:
        raise ProtocolError(f"{kind}: bad number in {line!r}") from exc
    if t_ms < 0:
        raise ProtocolError("negative timestamp")
    if kind == "B" and vals[0] not in (0, 1):
        raise ProtocolError("B value must be 0 or 1")
    if kind in ("S", "K") and not 0 <= vals[0] <= 1023:
        raise ProtocolError(f"{kind} value out of 0..1023")
    return Record(kind, t_ms, vals)


def parse_header(line: str) -> int | None:
    """Return the protocol version if `line` is a header line, else None."""
    line = line.strip()
    prefix = f"#{PROTOCOL_NAME} v"
    if not line.startswith(prefix):
        return None
    try:
        return int(line[len(prefix):])
    except ValueError:
        raise ProtocolError(f"bad header {line!r}") from None


def parse_stream(lines: Iterable[str], *, require_header: bool = True) -> Iterator[Record | ProtocolError]:
    """Yield Records; garbled lines are yielded as ProtocolError (not raised).

    Raises HeaderMissing if no header appears within HEADER_SEARCH_LINES lines
    and `require_header` is set; raises ProtocolError on an unsupported version.
    """
    seen_header = not require_header
    for n, raw in enumerate(lines):
        if not seen_header:
            version = parse_header(raw)
            if version is None:
                if n >= HEADER_SEARCH_LINES:
                    raise HeaderMissing(f"no {HEADER_LINE!r} header in first {n} lines")
                continue
            if version != PROTOCOL_VERSION:
                raise ProtocolError(f"unsupported protocol v{version}, expected v{PROTOCOL_VERSION}")
            seen_header = True
            continue
        try:
            rec = parse_line(raw)
        except ProtocolError as exc:
            yield exc
            continue
        if rec is not None:
            yield rec
    if not seen_header:
        raise HeaderMissing(f"no {HEADER_LINE!r} header found")
