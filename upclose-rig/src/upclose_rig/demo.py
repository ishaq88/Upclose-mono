"""`demo-run`: attach all kit evidence to one UpClose session in a single command.

Steps, each reported as one JSON line on stdout (progress goes to stderr):
  live -> rig_telemetry -> obd_snapshot -> audio_upload -> engine_audio -> tiger
The run stops at the first failed step (exit 1). The owner token is read from the
environment only and never printed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .audio import AudioConfig, write_synthetic
from .bundle import build_rig_bundle, to_json
from .live import SimPort, run_live, scripted_simulation
from .post import PostError, post_source, token_from_env
from .recording import read_recording

MAX_AUDIO_BYTES = 10 * 1024 * 1024
PIECE_BYTES = 8 * 1024 * 1024
AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}
MISFIRE_FOR_S = 12.0  # matches `live --sim-misfire-for`
MISFIRE_EVERY = 3
OBD_DEVICE_EMULATED = "elm327-emulator"


class StepFailed(RuntimeError):
    def __init__(self, message: str, http: int | None = None) -> None:
        super().__init__(message)
        self.http = http


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)[:300]


class Narrator:
    """Turns the live loop's chatter into one status line per phase on stderr:
    learning countdown (every 5 s), NORMAL, MISFIRE SUSPECTED, NOT ASSESSED."""

    def __init__(self, write: Callable[[str], None]) -> None:
        self.write = write
        self.state: str | None = None
        self.last_left: int | None = None

    def __call__(self, msg: str) -> None:
        m = re.match(r"\s*-> pod: !S,(L(\d+)|N|A|X)$", msg)
        if m:
            if m.group(2) is not None:
                left = int(m.group(2))
                if self.state != "L":
                    self.write(f"LEARNING   healthy baseline, keep the engine steady ({left} s)")
                elif left % 5 == 0 and left != self.last_left:
                    self.write(f"LEARNING   {left} s left")
                self.state, self.last_left = "L", left
            elif m.group(1) != self.state:
                self.state = m.group(1)
                self.write({"N": "NORMAL     engine sounds like its baseline",
                            "A": "MISFIRE SUSPECTED   the pod is alerting",
                            "X": "NOT ASSESSED   no usable baseline"}[self.state])
            return
        if msg.startswith("  -> pod") or re.match(r"\[\s*[\d.]+s\]", msg) or msg.startswith("wrote "):
            return  # per-second commands and raw model events: too noisy for the room
        self.write(msg)


def parse_obd(spec: str) -> tuple[str, list[str]]:
    if spec in ("clean", "cleared", "from-pod"):
        return spec, []
    if spec.startswith("dtc:") and re.fullmatch(r"[PCBU][0-3][0-9A-F]{3}", spec[4:]):
        return "dtc", [spec[4:]]
    raise ValueError(f"--obd must be from-pod, clean, cleared or dtc:<code> (e.g. dtc:P0301), not {spec!r}")


def _obd_state(mode: str, codes: list[str], rpm: float | None = None):  # type: ignore[no-untyped-def]
    from .obd_sim import CarState

    state = CarState(stored=[], mil_on=False)
    for code in codes:
        state.inject_dtc(code)
    if rpm is not None:
        state.rpm = rpm
    if mode == "cleared":
        state.clear_codes()
    elif mode == "clean":
        state.incomplete = set()  # healthy: monitors complete, MIL off
    return state


def _summary_bits(body: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    s = (body or {}).get("summary")
    if isinstance(s, dict):
        for k in ("state", "flag_count"):
            if k in s:
                out["summary_" + k] = s[k]
        if s.get("flags"):
            out["flags"] = s["flags"]
    return out


def _post_step(body: str, api: str, session: str, token: str, client: httpx.Client) -> dict[str, Any]:
    res = post_source(body, api, session, token, client)
    if not res.ok:
        raise StepFailed(res.message, res.status)
    j = res.body or {}
    return {"http": res.status, "source_id": j.get("source_id"), "kind": j.get("kind"),
            "sha256": j.get("data_sha256"), **_summary_bits(j)}


def _api(client: httpx.Client, method: str, url: str, token: str, **kw: Any) -> httpx.Response:
    try:
        resp = client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kw)
    except httpx.HTTPError as exc:
        raise StepFailed(f"could not reach {url.split('/api/')[0]}: {type(exc).__name__}") from None
    if resp.status_code >= 300:
        raise StepFailed(f"{resp.status_code}: {_flat(resp.text)}", resp.status_code)
    return resp


def upload_audio(path: Path, api: str, session: str, token: str, client: httpx.Client) -> dict[str, Any]:
    """Create, presign, PUT the pieces, complete. Returns upload_id, sha256, size."""
    ctype = AUDIO_TYPES.get(path.suffix.lower())
    if ctype is None:
        raise StepFailed(f"unsupported audio type {path.suffix!r}; use .wav, .mp3 or .m4a")
    data = path.read_bytes()
    if not data or len(data) > MAX_AUDIO_BYTES:
        raise StepFailed(f"audio must be 1 byte to {MAX_AUDIO_BYTES} bytes, got {len(data)}")
    base = api.rstrip("/") + "/api/v1"
    created = _api(client, "POST", f"{base}/sessions/{session}/audio", token,
                   json={"content_type": ctype, "content_length": len(data)}).json()
    upload_id = created["upload_id"]
    pieces = [data[i:i + PIECE_BYTES] for i in range(0, len(data), PIECE_BYTES)]
    if created.get("chunk_count") != len(pieces):
        raise StepFailed(f"server expects {created.get('chunk_count')} pieces, local file has {len(pieces)}")
    urls = _api(client, "POST", f"{base}/audio/{upload_id}/urls", token,
                json={"part_numbers": list(range(1, len(pieces) + 1))}).json()["items"]
    for item, piece in zip(sorted(urls, key=lambda i: i["part_number"]), pieces):
        try:  # presigned storage URL: no bearer token goes there
            r = client.put(item["url"], content=piece, headers=item.get("headers") or {})
        except httpx.HTTPError as exc:
            raise StepFailed(f"piece {item['part_number']} upload failed: {type(exc).__name__}") from None
        if r.status_code >= 300:
            raise StepFailed(f"piece {item['part_number']} upload failed: {r.status_code}", r.status_code)
    done = _api(client, "POST", f"{base}/audio/{upload_id}/complete", token, json={}).json()
    return {"http": 200, "upload_id": upload_id, "state": done.get("state"), "pieces": len(pieces),
            "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def run_demo(a: Any, client: httpx.Client | None = None, emit: Callable[[dict[str, Any]], None] | None = None) -> int:
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    def out(obj: dict[str, Any]) -> None:
        if obj.get("status") == "ok":
            extra = obj.get("source_id") or obj.get("upload_id") or obj.get("verdict") or ""
            http = f" HTTP {obj['http']}" if obj.get("http") else ""
            log(f"[ OK ] {obj['step']}{http} {extra}".rstrip())
        elif obj.get("status") == "failed":
            log(f"[FAIL] {obj['step']}: {obj.get('error')}")
        if emit:
            emit(obj)
        else:
            print(json.dumps(obj, separators=(",", ":")), flush=True)

    try:
        session = str(uuid.UUID(a.session))
        token = token_from_env()
        obd_arg = a.obd or ("from-pod" if a.port != "sim" else "clean")
        obd_mode, obd_codes = parse_obd(obd_arg)
        if a.real_vehicle and a.port == "sim":
            raise ValueError("--real-vehicle needs a real pod (--port COMx), not the simulator")
        if a.baseline_s < 20:
            raise ValueError("--baseline-s must be at least 20")
        misfire_at = None if str(a.misfire_at).lower() == "none" else float(a.misfire_at)
        if a.audio and a.audio != "synth" and not Path(a.audio).is_file():
            raise ValueError(f"--audio file not found: {a.audio}")
    except (PostError, ValueError) as exc:
        out({"step": "args", "status": "failed", "error": str(exc)})
        return 2

    sim = a.port == "sim"
    prov = "live" if a.real_vehicle else "synthetic"  # live = a real vehicle streaming now; bench is synthetic
    outdir = Path(a.out or Path("recordings") / session)
    rec_path, verdict_path = outdir / "run.upr", outdir / "run.verdict.json"
    own = client is None
    client = client or httpx.Client(timeout=60.0)
    step = "setup"
    try:
        outdir.mkdir(parents=True, exist_ok=True)

        step = "live"
        narrate = Narrator(log)
        pressed = False

        def watch(rec: Any) -> None:  # the pod's button, seen while the run is live
            nonlocal pressed
            if rec.kind == "B" and rec.values[0] == 1 and not pressed:
                pressed = True
                log("BUTTON PRESSED   P0301 will be reported")

        if sim:
            recs = scripted_simulation(a.seconds, misfire_at=misfire_at, misfire_for=MISFIRE_FOR_S,
                                       misfire_every=MISFIRE_EVERY, seed=a.seed)
            res = run_live(SimPort(recs, speed=a.speed), baseline_s=a.baseline_s, record_path=rec_path,
                           verdict_path=verdict_path, source="simulator", on_record=watch, out=narrate)
        else:
            from .serial_io import open_port

            with open_port(a.port, a.baud) as ser:
                res = run_live(ser, baseline_s=a.baseline_s, record_path=rec_path, verdict_path=verdict_path,
                               max_wall_s=a.seconds, source="board", on_record=watch, out=narrate)
        if res.verdict is None:
            raise StepFailed("no verdict: the baseline did not complete (need --seconds well above --baseline-s)")
        out({"step": "live", "status": "ok", "provenance": prov, "records": len(res.records),
             "verdict": res.verdict["verdict"], "anomalous_runs": len(res.verdict["anomalous_runs"]),
             "recording": str(rec_path), "verdict_file": str(verdict_path)})

        step = "rig_telemetry"
        env = build_rig_bundle(read_recording(rec_path), prov, verdict=res.verdict)  # type: ignore[arg-type]
        out({"step": step, "status": "ok", **_post_step(to_json(env), a.api, session, token, client)})

        step = "obd_snapshot"
        from .obd_sim import EmulatedEcu
        from .obd_snapshot import connect, link_from_recording, snapshot_from_connection

        obd_rpm = None
        if obd_mode == "from-pod":  # button press injects P0301, last knob value sets RPM 600-3000
            was_pressed, obd_rpm = link_from_recording(res.records)
            obd_mode, obd_codes = ("dtc", ["P0301"]) if was_pressed else ("clean", [])
            log(f"OBD from pod: {'P0301 injected' if was_pressed else 'no button press, clean'}"
                f"{'' if obd_rpm is None else f', RPM {obd_rpm:g}'}")
        with EmulatedEcu(_obd_state(obd_mode, obd_codes, obd_rpm)) as ecu:
            conn = connect(ecu.portstr)
            try:
                obd_env = snapshot_from_connection(conn, provenance="synthetic", device=OBD_DEVICE_EMULATED)
            finally:
                conn.close()
        out({"step": step, "status": "ok", "obd": f"dtc:{obd_codes[0]}" if obd_mode == "dtc" else obd_mode, **_post_step(to_json(obd_env), a.api, session, token, client)})

        if a.audio:
            step = "audio_upload"
            if a.audio == "synth":
                wav = outdir / "engine.wav"
                write_synthetic(wav, AudioConfig(
                    seconds=a.seconds, misfire_every=MISFIRE_EVERY if misfire_at is not None else 0,
                    misfire_from_s=misfire_at or 0.0,
                    misfire_until_s=None if misfire_at is None else misfire_at + MISFIRE_FOR_S, seed=a.seed))
                aprov, device = "synthetic", "mic-synth"
            else:
                wav, aprov, device = Path(a.audio), a.audio_provenance, "mic-file"
            up = upload_audio(wav, a.api, session, token, client)
            out({"step": step, "status": "ok", "provenance": aprov, **up})
            step = "engine_audio"
            body = json.dumps({"kind": "engine_audio", "provenance": aprov, "device": device, "captured_at": _now(),
                               "data": {"upload_id": up["upload_id"], "sha256": up["sha256"]}}, separators=(",", ":"))
            out({"step": step, "status": "ok", **_post_step(body, a.api, session, token, client)})
        else:
            out({"step": "engine_audio", "status": "skipped", "reason": "no --audio"})

        if a.tiger:
            step = "tiger"
            from . import tiger

            r = tiger.push(rec_path, verdict_path, "real" if a.real_vehicle else "synthetic", run_id=session)
            out({"step": step, "status": "ok", "run_id": r["run_id"], "samples": r["samples"],
                 "verdict_state": r.get("verdict_state")})
        else:
            out({"step": "tiger", "status": "skipped", "reason": "no --tiger"})
        return 0
    except StepFailed as exc:
        out({"step": step, "status": "failed", "http": exc.http, "error": str(exc)})
    except Exception as exc:  # any other step failure: report, stop, nonzero exit
        out({"step": step, "status": "failed", "error": f"{type(exc).__name__}: {_flat(str(exc))}"})
    finally:
        if own:
            client.close()
    return 1
