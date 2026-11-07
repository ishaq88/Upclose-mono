"""Read an ELM327 adapter (emulated or real) through python-OBD into an obd_snapshot envelope."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import ObdSnapshotEnvelope, Provenance
from .protocol import Record

STATUS_NAMES = {
    "MISFIRE_MONITORING": "misfire",
    "FUEL_SYSTEM_MONITORING": "fuel_system",
    "COMPONENT_MONITORING": "components",
    "CATALYST_MONITORING": "catalyst",
    "HEATED_CATALYST_MONITORING": "heated_catalyst",
    "EVAPORATIVE_SYSTEM_MONITORING": "evap",
    "SECONDARY_AIR_SYSTEM_MONITORING": "secondary_air",
    "OXYGEN_SENSOR_MONITORING": "oxygen_sensor",
    "OXYGEN_SENSOR_HEATER_MONITORING": "oxygen_sensor_heater",
    "EGR_VVT_SYSTEM_MONITORING": "egr_vvt",
}
KNOB_RPM_MIN, KNOB_RPM_MAX = 600.0, 3000.0


class ObdError(RuntimeError):
    pass


def knob_to_rpm(knob: int) -> float:
    return round(KNOB_RPM_MIN + (KNOB_RPM_MAX - KNOB_RPM_MIN) * knob / 1023.0, 1)


def link_from_recording(records: list[Record]) -> tuple[bool, float | None]:
    """Button press (B,1) => inject P0301; last knob value => RPM 600..3000."""
    pressed = any(r.kind == "B" and r.values[0] == 1 for r in records)
    knobs = [r for r in records if r.kind == "K"]
    return pressed, (knob_to_rpm(int(knobs[-1].values[0])) if knobs else None)


def _q(conn: Any, cmd: Any) -> Any:
    r = conn.query(cmd, force=True)
    return None if r.is_null() else r.value


def snapshot_from_connection(conn: Any, *, provenance: Provenance, device: str) -> ObdSnapshotEnvelope:
    """Query python-OBD `conn` (an obd.OBD). Raises ObdError if not connected."""
    import obd

    if not conn.is_connected():
        raise ObdError("python-OBD is not connected to the adapter/ECU")
    c = obd.commands
    status = _q(conn, c.STATUS)
    if status is None:
        raise ObdError("no answer to STATUS (mode 01 PID 01)")
    dtcs = [{"code": code, "status": "stored"} for code, _ in (_q(conn, c.GET_DTC) or [])]
    stored = {d["code"] for d in dtcs}
    dtcs += [{"code": code, "status": "pending"} for code, _ in (_q(conn, c.GET_CURRENT_DTC) or []) if code not in stored]
    readiness: dict[str, str] = {}
    for attr, name in STATUS_NAMES.items():
        test = getattr(status, attr, None)
        if test is None or not getattr(test, "name", ""):
            continue
        readiness[name] = "unsupported" if not test.available else ("complete" if test.complete else "incomplete")
    data: dict[str, Any] = {"mil_on": bool(status.MIL), "dtcs": dtcs, "readiness": readiness}
    vin = _q(conn, c.VIN)
    if vin:
        data["vin"] = bytes(vin).decode("ascii", errors="replace").strip()
    dist = _q(conn, c.DISTANCE_SINCE_DTC_CLEAR)
    if dist is not None:
        data["distance_since_codes_cleared_km"] = int(round(dist.to("kilometer").magnitude))
    warm = _q(conn, c.WARMUPS_SINCE_DTC_CLEAR)
    if warm is not None:
        data["warmups_since_codes_cleared"] = int(round(warm.magnitude))
    pids: dict[str, float] = {}
    rpm = _q(conn, c.RPM)
    if rpm is not None:
        pids["rpm"] = float(rpm.magnitude)
    coolant = _q(conn, c.COOLANT_TEMP)
    if coolant is not None:
        pids["coolant_c"] = float(coolant.to("celsius").magnitude)
    if pids:
        data["pids"] = pids
    return ObdSnapshotEnvelope.model_validate(
        {
            "kind": "obd_snapshot",
            "provenance": provenance,
            "device": device,
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": data,
        }
    )


def connect(portstr: str, timeout: float = 5.0) -> Any:
    """python-OBD connection with a fixed baud: auto-baud probing sends bytes the
    emulator rejects (it would never connect)."""
    import logging

    logging.getLogger("pint").setLevel(logging.ERROR)  # noisy unit redefinition warnings
    import obd

    obd.logger.setLevel(logging.CRITICAL)
    return obd.OBD(portstr, baudrate=38400, fast=False, timeout=timeout)
