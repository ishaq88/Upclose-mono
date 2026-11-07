"""Turn a recording into a rig_telemetry envelope per the wire contract."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from pathlib import Path

from .models import (
    MAX_ACCEL_SAMPLES, MAX_SOUND_SAMPLES, MAX_TEMPERATURE, Provenance,
    RigTelemetryData, RigTelemetryEnvelope,
)
from .recording import Recording

DEVICE = "grove-beginner-kit"


class BundleError(ValueError):
    pass


def _rate(times: list[int], what: str) -> float:
    if len(times) < 2 or times[-1] <= times[0]:
        raise BundleError(f"cannot estimate {what} rate from {len(times)} samples")
    return round((len(times) - 1) / ((times[-1] - times[0]) / 1000.0), 3)


def check_provenance(rec: Recording, provenance: Provenance) -> None:
    if rec.source == "simulator" and provenance != "synthetic":
        raise BundleError("simulator recordings must be posted with provenance 'synthetic'")


def build_rig_bundle(
    rec: Recording,
    provenance: Provenance,
    *,
    cylinders: int | None = 4,
    start_s: float = 0.0,
    seconds: float | None = None,
    device: str = DEVICE,
    verdict: dict[str, object] | None = None,
) -> RigTelemetryEnvelope:
    """Rates come from timestamps: (n-1)/(t_last-t_first). Samples are not
    resampled (the pod samples on a fixed schedule). Temperature t_ms is
    relative to the window start (= recording start by default), which is also captured_at."""
    check_provenance(rec, provenance)
    if not rec.records:
        raise BundleError("recording is empty")
    t0 = min(r.t_ms for r in rec.records)
    lo = t0 + int(start_s * 1000)
    hi = None if seconds is None else lo + int(seconds * 1000)

    def win(kind: str):  # type: ignore[no-untyped-def]
        return [r for r in rec.of(kind) if r.t_ms >= lo and (hi is None or r.t_ms < hi)]

    a = win("A")
    if not a:
        raise BundleError("no accelerometer (A) records in the selected window")
    if len(a) > MAX_ACCEL_SAMPLES:
        raise BundleError(f"{len(a)} accel samples exceeds {MAX_ACCEL_SAMPLES}; use --seconds to window")
    data: dict[str, object] = {
        "accel": {
            "rate_hz": _rate([r.t_ms for r in a], "accel"),
            "x_mg": [int(r.values[0]) for r in a],
            "y_mg": [int(r.values[1]) for r in a],
            "z_mg": [int(r.values[2]) for r in a],
        }
    }
    s = win("S")
    if s:
        if len(s) > MAX_SOUND_SAMPLES:
            raise BundleError(f"{len(s)} sound samples exceeds {MAX_SOUND_SAMPLES}")
        data["sound"] = {"rate_hz": _rate([r.t_ms for r in s], "sound"), "level": [int(r.values[0]) for r in s]}
    t = win("T")
    if t:
        if len(t) > MAX_TEMPERATURE:
            raise BundleError(f"{len(t)} temperature readings exceeds {MAX_TEMPERATURE}; use --seconds")
        data["temperature"] = [{"t_ms": r.t_ms - lo, "celsius": float(r.values[0])} for r in t]
    if cylinders is not None:
        data["cylinders"] = cylinders
    if verdict is not None:
        data["model_verdict"] = verdict

    started = datetime.fromisoformat(str(rec.header["started_at"]).replace("Z", "+00:00"))
    captured = (started + timedelta(milliseconds=lo - t0)).astimezone(timezone.utc)
    return RigTelemetryEnvelope.model_validate(
        {
            "kind": "rig_telemetry",
            "provenance": provenance,
            "device": device,
            "captured_at": captured.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": data,
        }
    )


def load_verdict(path: str | Path) -> dict[str, object]:
    try:
        v = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"{path}: verdict file is not JSON") from exc
    if not isinstance(v, dict):
        raise BundleError(f"{path}: verdict file must hold a JSON object")
    return v


def to_json(envelope: object) -> str:
    """Compact JSON. None fields are dropped except inside model_verdict, where
    nulls are meaningful (a not_assessed verdict carries null counts)."""
    d = envelope.model_dump(mode="json", exclude_none=True, by_alias=True)  # type: ignore[attr-defined]
    mv = getattr(getattr(envelope, "data", None), "model_verdict", None)
    if mv is not None:
        d["data"]["model_verdict"] = mv.model_dump(mode="json", by_alias=True)
    return json.dumps(d, separators=(",", ":"))
