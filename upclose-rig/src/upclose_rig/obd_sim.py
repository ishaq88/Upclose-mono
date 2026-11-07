"""Emulated ELM327 adapter for python-OBD (no physical car computer needed).

Chosen path: the `ELM327-emulator` package (module `elm`), run in-process on a
loopback TCP port. python-OBD connects with ``socket://127.0.0.1:<port>``.
We build a private scenario ("upclose") from the package's "car" scenario and
override only the requests whose answers must follow our mutable CarState:
mode 01 PID 01 (MIL, DTC count, readiness), 05 (coolant), 0C (rpm), 30
(warm-ups), 31 (distance), mode 03/07/0A (DTCs), mode 04 (clear), mode 09 02
(VIN).
"""

from __future__ import annotations

import copy
import os
import socket
import threading
import time
from dataclasses import dataclass, field

# Keep the emulator from writing elm.log into the working directory.
os.environ.setdefault("ELM_LOG_CFG", "__upclose_no_log_cfg__")

# 17 chars, no I/O/Q (contract regex). Must not start/end with 0,1,2,x,\ because
# python-OBD's encoded-string decoder strips those characters from both ends.
SYNTHETIC_VIN = "SYNTHETC00000UPCA"

# bit mask in the PID 01 C/D bytes (spark ignition) per readiness monitor
NON_CONTINUOUS_MONITORS: dict[str, int] = {
    "catalyst": 0x01,
    "heated_catalyst": 0x02,
    "evap": 0x04,
    "secondary_air": 0x08,
    "oxygen_sensor": 0x20,
    "oxygen_sensor_heater": 0x40,
    "egr": 0x80,
}
# continuous monitors: (supported mask, incomplete mask) in byte B
CONTINUOUS_MONITORS: dict[str, tuple[int, int]] = {
    "misfire": (0x01, 0x10),
    "fuel_system": (0x02, 0x20),
    "components": (0x04, 0x40),
}
ALL_MONITORS = [*CONTINUOUS_MONITORS, *NON_CONTINUOUS_MONITORS]
# Monitors that a real ECU reports as supported on a typical gasoline car.
SUPPORTED_MONITORS = [
    "misfire", "fuel_system", "components", "catalyst", "evap",
    "oxygen_sensor", "oxygen_sensor_heater",
]


def encode_dtc(code: str) -> str:
    """P0301 -> '0301' (two SAE J2012 bytes as 4 hex digits)."""
    code = code.upper()
    if len(code) != 5 or code[0] not in "PCBU" or code[1] not in "0123":
        raise ValueError(f"bad DTC {code!r}")
    value = ("PCBU".index(code[0]) << 14) | (int(code[1]) << 12) | int(code[2:], 16)
    return f"{value:04X}"


@dataclass
class CarState:
    """Mutable fake ECU state. All writes go through methods that lock."""

    vin: str = SYNTHETIC_VIN
    stored: list[str] = field(default_factory=lambda: ["P0301"])
    pending: list[str] = field(default_factory=list)
    permanent: list[str] = field(default_factory=list)
    mil_on: bool = True
    rpm: float = 780.0
    coolant_c: float = 24.0
    distance_since_clear_km: int = 12
    warmups_since_clear: int = 1
    incomplete: set[str] = field(
        default_factory=lambda: {"catalyst", "evap"}
    )
    supported: list[str] = field(default_factory=lambda: list(SUPPORTED_MONITORS))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inject_dtc(self, code: str, *, mil: bool = True) -> None:
        encode_dtc(code)  # validate
        with self._lock:
            if code not in self.stored:
                self.stored.append(code)
            if mil:
                self.mil_on = True

    def clear_codes(self) -> None:
        """Seller-cleared-codes trick: wipe DTCs/MIL, counters ~0, monitors incomplete."""
        with self._lock:
            self.stored.clear()
            self.pending.clear()
            self.permanent.clear()
            self.mil_on = False
            self.distance_since_clear_km = 0
            self.warmups_since_clear = 0
            self.incomplete = {
                m for m in self.supported if m in NON_CONTINUOUS_MONITORS
            }

    def status_bytes(self) -> str:
        with self._lock:
            count = min(len(self.stored), 0x7F)
            a = (0x80 if self.mil_on else 0) | count
            b = 0
            for name, (sup, inc) in CONTINUOUS_MONITORS.items():
                if name in self.supported:
                    b |= sup
                    if name in self.incomplete:
                        b |= inc
            c = d = 0
            for name, mask in NON_CONTINUOUS_MONITORS.items():
                if name in self.supported:
                    c |= mask
                    if name in self.incomplete:
                        d |= mask
            return f"{a:02X} {b:02X} {c:02X} {d:02X}"


def _dtc_response(codes: list[str], sid: str) -> str:
    from elm.obd_message import iso_tp_frames

    data = [sid, f"{len(codes):02X}"]  # python-OBD strips mode + count bytes
    for code in codes:
        enc = encode_dtc(code)
        data += [enc[:2], enc[2:]]
    return iso_tp_frames(data)


def build_scenario(base_car: dict, state: CarState) -> dict:
    """Copy the package's 'car' scenario and add state-driven overrides.

    Overrides use Priority 1 so they match before the stock entries.
    """
    from elm.obd_message import DT, ECU_ADDR_E, ECU_R_ADDR_E, ELM_FOOTER, HD, PA, SZ

    def hdr(n: int) -> str:
        return HD(ECU_R_ADDR_E) + SZ(f"{n:02X}")

    def mode1(pid: str, fn):  # type: ignore[no-untyped-def]
        return {
            "Request": "^01" + pid + ELM_FOOTER,
            "Descr": f"upclose override {pid}",
            "Header": ECU_ADDR_E,
            "Priority": 1,
            "ResponseFooter": lambda self, cmd, pid_, uc_val: fn(),
        }

    def rpm() -> str:
        raw = int(round(state.rpm * 4)) & 0xFFFF
        return hdr(4) + DT(f"41 0C {raw >> 8:02X} {raw & 0xFF:02X}")

    def clear() -> str:
        state.clear_codes()
        return HD(ECU_R_ADDR_E) + SZ("01") + DT("44")

    def dist() -> str:
        raw = max(0, min(65535, int(state.distance_since_clear_km)))
        return hdr(4) + DT(f"41 31 {raw >> 8:02X} {raw & 0xFF:02X}")

    def vin() -> str:
        return PA("01 " + " ".join(f"{b:02X}" for b in state.vin.encode("ascii")))

    def dtcs(codes_attr: str, sid: str):  # type: ignore[no-untyped-def]
        return lambda self, cmd, pid_, uc_val: _dtc_response(
            list(getattr(state, codes_attr)), sid
        )

    sc = copy.deepcopy(base_car)
    sc.update(
        {
            "UC_STATUS": mode1("01", lambda: hdr(6) + DT("41 01 " + state.status_bytes())),
            "UC_COOLANT": mode1(
                "05", lambda: hdr(3) + DT(f"41 05 {int(round(state.coolant_c)) + 40 & 0xFF:02X}")
            ),
            "UC_RPM": mode1("0C", rpm),
            "UC_WARMUPS": mode1(
                "30", lambda: hdr(3) + DT(f"41 30 {min(255, state.warmups_since_clear):02X}")
            ),
            "UC_DISTANCE": mode1("31", dist),
            "UC_DTC": {
                "Request": "^03" + ELM_FOOTER, "Header": ECU_ADDR_E, "Priority": 1,
                "Descr": "stored DTCs", "ResponseFooter": dtcs("stored", "43"),
            },
            "UC_DTC_PENDING": {
                "Request": "^07" + ELM_FOOTER, "Header": ECU_ADDR_E, "Priority": 1,
                "Descr": "pending DTCs", "ResponseFooter": dtcs("pending", "47"),
            },
            "UC_DTC_PERMANENT": {
                "Request": "^0A" + ELM_FOOTER, "Header": ECU_ADDR_E, "Priority": 1,
                "Descr": "permanent DTCs", "ResponseFooter": dtcs("permanent", "4A"),
            },
            "UC_CLEAR": {
                "Request": "^04" + ELM_FOOTER, "Header": ECU_ADDR_E, "Priority": 1,
                "Descr": "clear DTCs (resets counters, monitors incomplete)",
                "ResponseFooter": lambda self, cmd, pid_, uc_val: clear(),
            },
            "UC_VIN": {
                "Request": "^0902" + ELM_FOOTER, "Priority": 1,
                "Descr": "synthetic VIN", "ResponseFooter": lambda self, cmd, pid_, uc_val: vin(),
            },
        }
    )
    return sc


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class EmulatedEcu:
    """Context manager running the ELM327 emulator on a loopback TCP port."""

    def __init__(self, state: CarState | None = None, port: int | None = None) -> None:
        self.state = state or CarState()
        self.port = port or free_port()
        self._elm = None

    @property
    def portstr(self) -> str:
        return f"socket://127.0.0.1:{self.port}"

    def __enter__(self) -> "EmulatedEcu":
        from elm import Elm
        from elm.obd_message import ObdMessage

        elm = Elm(net_port=self.port, net_interface="127.0.0.1", batch_mode=True)
        elm.ObdMessage = {**ObdMessage, "upclose": build_scenario(ObdMessage["car"], self.state)}
        elm.set_sorted_obd_msg("upclose")
        import logging

        class _Reconnect(logging.Filter):
            """The emulator spins forever after an aborted TCP client (Windows
            WinError 10053) because it never drops the dead connection. Drop it
            here so the next client can connect, and silence the log spam."""

            def filter(self, record: logging.LogRecord) -> bool:
                if record.getMessage().startswith("Error while reading from network"):
                    elm.sock_conn = None
                    elm.sock_addr = None
                    return False
                return True

        self._filter = _Reconnect()
        logging.getLogger().addFilter(self._filter)
        elm.__enter__()
        self._elm = elm
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # wait for the listener
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        import logging

        if self._elm is not None:
            self._elm.terminate()
            self._elm = None
            logging.getLogger().removeFilter(self._filter)
