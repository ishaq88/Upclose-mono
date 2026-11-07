"""Recording file: JSON Lines. Line 1 is a header object, then one JSON array
per record: ["A", t_ms, x, y, z]. Simulator and board recordings share it."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .protocol import PROTOCOL_NAME, PROTOCOL_VERSION, Record

FORMAT = "upclose-rig-recording"
FORMAT_VERSION = 1


class RecordingError(ValueError):
    pass


@dataclass
class Recording:
    header: dict[str, Any]
    records: list[Record] = field(default_factory=list)

    @property
    def source(self) -> str:
        return str(self.header.get("source", "unknown"))

    def of(self, kind: str) -> list[Record]:
        return [r for r in self.records if r.kind == kind]


def make_header(source: str, started_at: str, **extra: Any) -> dict[str, Any]:
    if source not in ("board", "simulator"):
        raise RecordingError("source must be board or simulator")
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "source": source,
        "protocol": f"{PROTOCOL_NAME} v{PROTOCOL_VERSION}",
        "started_at": started_at,
        **extra,
    }


def write_recording(path: str | Path, header: dict[str, Any], records: Iterable[Record]) -> int:
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(header, separators=(",", ":")) + "\n")
        for r in records:
            fh.write(json.dumps([r.kind, r.t_ms, *r.values], separators=(",", ":")) + "\n")
            n += 1
    return n


def read_recording(path: str | Path) -> Recording:
    with open(path, "r", encoding="utf-8") as fh:
        first = fh.readline()
        try:
            header = json.loads(first)
        except json.JSONDecodeError as exc:
            raise RecordingError(f"{path}: first line is not a JSON header") from exc
        if not isinstance(header, dict) or header.get("format") != FORMAT:
            raise RecordingError(f"{path}: not an {FORMAT} file")
        if header.get("format_version") != FORMAT_VERSION:
            raise RecordingError(f"{path}: unsupported format_version {header.get('format_version')}")
        recs: list[Record] = []
        for lineno, line in enumerate(fh, 2):
            if not line.strip():
                continue
            try:
                arr = json.loads(line)
                recs.append(Record(str(arr[0]), int(arr[1]), tuple(arr[2:])))
            except (json.JSONDecodeError, IndexError, TypeError, ValueError) as exc:
                raise RecordingError(f"{path}:{lineno}: bad record") from exc
    return Recording(header, recs)
