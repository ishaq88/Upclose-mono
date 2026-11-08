"""Command line: python -m upclose_rig <command> ..."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from pydantic import TypeAdapter, ValidationError

from . import __version__
from .bundle import BundleError, build_rig_bundle, load_verdict, to_json
from .models import SourceEnvelope
from .post import PostError, post_source, token_from_env
from .protocol import BAUD, HeaderMissing, ProtocolError
from .recording import RecordingError, make_header, read_recording, write_recording
from .simulator import SimConfig, simulate

PROVENANCES = ("synthetic", "cached", "live")
OBD_DEVICE_EMULATED = "elm327-emulator"
OBD_DEVICE_REAL = "elm327"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def _write_or_print(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {out} ({len(text)} bytes)", file=sys.stderr)
    else:
        print(text)


def _validate_body(text: str) -> str:
    """Re-validate a bundle file against the contract models before sending."""
    TypeAdapter(SourceEnvelope).validate_json(text)
    return text


# --- commands -------------------------------------------------------------

def cmd_simulate(a: argparse.Namespace) -> int:
    cfg = SimConfig(
        seconds=a.seconds, rpm=a.rpm, cylinders=a.cylinders, misfire_every=a.misfire_every,
        cold=a.cold, seed=a.seed, events=tuple(a.event or ()),
    )
    recs = simulate(cfg)
    header = make_header(
        "simulator", a.started_at or _now(), seed=cfg.seed, rpm=cfg.rpm, cylinders=cfg.cylinders,
        misfire_every=cfg.misfire_every, cold=cfg.cold, seconds=cfg.seconds,
    )
    n = write_recording(a.out, header, recs)
    print(f"wrote {a.out}: {n} records (synthetic, simulator source)", file=sys.stderr)
    return 0


def cmd_simulate_audio(a: argparse.Namespace) -> int:
    from .audio import AudioConfig, write_synthetic

    cfg = AudioConfig(
        seconds=a.seconds, rpm=a.rpm, cylinders=a.cylinders, misfire_every=a.misfire_every,
        misfire_from_s=a.misfire_from, misfire_until_s=None if a.misfire_for is None else a.misfire_from + a.misfire_for,
        cold=a.cold, seed=a.seed,
    )
    side = write_synthetic(a.out, cfg)
    print(f"wrote {a.out} and {side} (16 kHz mono, synthetic)", file=sys.stderr)
    return 0


def cmd_demo_run(a: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(a)


def cmd_record(a: argparse.Namespace) -> int:
    from .serial_io import capture, iter_serial_lines, open_port

    with open_port(a.port, a.baud) as ser:
        lines = iter_serial_lines(ser, a.seconds + a.header_wait)
        rec, dropped = capture(lines, a.seconds)
    if not rec.records:
        return _err("no records captured")
    write_recording(a.out, rec.header, rec.records)
    print(f"wrote {a.out}: {len(rec.records)} records, {dropped} garbled lines skipped", file=sys.stderr)
    return 0


def cmd_monitor(a: argparse.Namespace) -> int:
    from .serial_io import iter_serial_lines, monitor, open_port

    with open_port(a.port, a.baud) as ser:
        try:
            monitor(iter_serial_lines(ser))
        except KeyboardInterrupt:
            pass
    return 0


def _rig_body(a: argparse.Namespace) -> str:
    rec = read_recording(a.recording)
    env = build_rig_bundle(
        rec, a.provenance, cylinders=a.cylinders, start_s=a.start_s, seconds=a.seconds,
        verdict=load_verdict(a.verdict) if a.verdict else None,
    )
    return to_json(env)


def cmd_bundle(a: argparse.Namespace) -> int:
    if a.kind != "rig_telemetry":
        return _err("bundle supports --kind rig_telemetry; use obd-snapshot for obd_snapshot")
    _write_or_print(_rig_body(a), a.out)
    return 0


def cmd_post(a: argparse.Namespace) -> int:
    token = token_from_env()  # raises PostError; never from argv
    if a.body:
        body = _validate_body(Path(a.body).read_text(encoding="utf-8"))
    elif a.recording:
        body = _rig_body(a)
    else:
        return _err("give --recording (rig_telemetry) or --body <bundle.json>")
    result = post_source(body, a.api, a.session, token)
    print(result.message, file=sys.stderr if not result.ok else sys.stdout)
    return 0 if result.ok else 1


def _build_state(a: argparse.Namespace):  # type: ignore[no-untyped-def]
    from .obd_sim import CarState

    dtcs: list[str] = list(a.dtc or [])
    rpm = a.rpm
    if a.from_recording:
        from .obd_snapshot import link_from_recording

        pressed, knob_rpm = link_from_recording(read_recording(a.from_recording).records)
        if not dtcs and pressed:
            dtcs = ["P0301"]
        if rpm is None and knob_rpm is not None:
            rpm = knob_rpm
    elif not dtcs and not a.cleared:
        dtcs = ["P0301"]
    state = CarState(stored=[], mil_on=False)
    for code in dtcs:
        state.inject_dtc(code)
    if rpm is not None:
        state.rpm = rpm
    if a.coolant is not None:
        state.coolant_c = a.coolant
    if a.distance is not None:
        state.distance_since_clear_km = a.distance
    if a.warmups is not None:
        state.warmups_since_clear = a.warmups
    if a.cleared:
        state.clear_codes()
    elif not state.stored:
        state.incomplete = set()  # healthy default: monitors complete, MIL off
    return state


def cmd_obd_snapshot(a: argparse.Namespace) -> int:
    from .obd_snapshot import ObdError, connect, snapshot_from_connection

    if a.obd_port:
        conn = connect(a.obd_port)
        try:
            env = snapshot_from_connection(conn, provenance=a.provenance, device=OBD_DEVICE_REAL)
        finally:
            conn.close()
    else:
        if a.provenance != "synthetic":
            return _err("emulated OBD data must be posted with provenance 'synthetic'")
        from .obd_sim import EmulatedEcu

        with EmulatedEcu(_build_state(a)) as ecu:
            conn = connect(ecu.portstr)
            try:
                env = snapshot_from_connection(conn, provenance="synthetic", device=OBD_DEVICE_EMULATED)
            except ObdError:
                raise
            finally:
                conn.close()
        print("note: emulated adapter, synthetic VIN, provenance=synthetic", file=sys.stderr)
    _write_or_print(to_json(env), a.out)
    return 0


def cmd_obd_serve(a: argparse.Namespace) -> int:
    from .obd_sim import EmulatedEcu

    with EmulatedEcu(_build_state(a), port=a.port) as ecu:
        print(f"emulated ELM327 listening; python-OBD port string: {ecu.portstr}  (Ctrl-C to stop)", file=sys.stderr)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_anomaly_fit(a: argparse.Namespace) -> int:
    from .anomaly import fit_from_records

    rec = read_recording(a.recording)
    model = fit_from_records(rec.records, a.audio, source=rec.source)
    model.save(a.out)
    print(
        f"wrote {a.out}: firing {model.fire_hz:.2f} Hz, {model.n_fit_windows}+{model.n_calibration_windows} baseline windows, "
        f"threshold {model.threshold:.3f}, audio={model.has_audio} (baseline source: {rec.source})",
        file=sys.stderr,
    )
    return 0


def cmd_anomaly_score(a: argparse.Namespace) -> int:
    from .anomaly import AnomalyModel, assess_records

    model = AnomalyModel.load(a.model)
    rec = read_recording(a.recording)
    verdict = assess_records(model, rec.records, a.audio, source=rec.source)
    print(json.dumps(verdict, indent=2))
    return 0


def cmd_anomaly_eval(a: argparse.Namespace) -> int:
    from .anomaly.evaluate import write_report

    write_report(a.out)
    print(f"wrote {a.out} (synthetic simulator data only)", file=sys.stderr)
    return 0


def cmd_live(a: argparse.Namespace) -> int:
    from .live import SimPort, run_live, scripted_simulation

    if a.baseline_s < 20:
        return _err("--baseline-s must be at least 20")
    if a.port == "sim":
        seconds = a.seconds if a.seconds is not None else 60.0
        recs = scripted_simulation(seconds, rpm=a.sim_rpm, misfire_at=a.sim_misfire_at, misfire_for=a.sim_misfire_for,
                                   misfire_every=a.sim_misfire_every, seed=a.seed)
        port = SimPort(recs, speed=a.speed)
        print("simulated pod (SYNTHETIC data); commands are printed, none reach hardware", file=sys.stderr)
        run_live(port, baseline_s=a.baseline_s, record_path=a.record, verdict_path=a.verdict_out, mute=a.mute, source="simulator")
        return 0
    from .serial_io import open_port

    with open_port(a.port, a.baud) as ser:
        run_live(ser, baseline_s=a.baseline_s, record_path=a.record, verdict_path=a.verdict_out, mute=a.mute,
                 max_wall_s=a.seconds, source="board")
    return 0


def cmd_tiger_init(a: argparse.Namespace) -> int:
    from . import tiger

    n = tiger.init()
    print(f"tiger store ready ({n} statements applied)", file=sys.stderr)
    return 0


def cmd_tiger_push(a: argparse.Namespace) -> int:
    from . import tiger

    res = tiger.push(a.recording, a.verdict, a.provenance, run_id=a.run_id, notes=a.notes)
    extra = f", verdict {res['verdict_state']}" if "verdict_state" in res else ""
    print(f"pushed {a.recording} as run {res['run_id']}: {res['samples']} samples{extra}", file=sys.stderr)
    return 0


def cmd_tiger_summary(a: argparse.Namespace) -> int:
    from . import tiger

    s = tiger.summarize(a.run_id)
    run = s["run"]
    print(f"run {run['run_id']} source={run['source']} provenance={run['provenance']} started={run['started_at']}")
    v = s["verdict"]
    print(f"verdict: {v['state']} score={v['score']}" if v else "verdict: none")
    print(f"buckets: {s['n_buckets']} (1 s)")
    for b in s["buckets"]:
        rms = f"{b['accel_rms']:.1f}" if b["accel_rms"] is not None else "-"
        smean = f"{b['sound_mean']:.0f}" if b["sound_mean"] is not None else "-"
        smax = f"{b['sound_max']}" if b["sound_max"] is not None else "-"
        print(f"{b['bucket']} accel_n={b['n_accel']} accel_rms={rms} sound_mean={smean} sound_max={smax}")
    return 0


# --- parser ---------------------------------------------------------------

def _add_obd_state_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dtc", action="append", help="DTC to inject (repeatable); default P0301 unless --cleared")
    p.add_argument("--cleared", action="store_true", help="simulate seller-cleared codes: no DTCs/MIL, counters ~0, monitors incomplete")
    p.add_argument("--rpm", type=float)
    p.add_argument("--coolant", type=float, help="coolant temperature, C")
    p.add_argument("--distance", type=int, help="km since codes cleared")
    p.add_argument("--warmups", type=int, help="warm-ups since codes cleared")
    p.add_argument("--from-recording", help="pod recording: button press injects P0301, knob sets RPM 600-3000")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="upclose_rig", description="UpClose capture-rig bridge")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate", help="write a synthetic engine recording")
    s.add_argument("--seconds", type=float, default=60.0)
    s.add_argument("--rpm", type=float, default=780.0)
    s.add_argument("--cylinders", type=int, default=4)
    s.add_argument("--misfire-every", type=int, default=0, metavar="N", help="drop every Nth firing event")
    s.add_argument("--cold", action="store_true", help="cold start (temperature rising); default already warm")
    s.add_argument("--seed", type=int, default=1234)
    s.add_argument("--event", action="append", metavar="SPEC", help="scripted event B@<sec>=<0|1> or K@<sec>=<0-1023>")
    s.add_argument("--started-at", help="header start time (UTC, ...Z); default now")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_simulate)

    sa = sub.add_parser("simulate-audio", help="write synthetic engine audio (16 kHz mono WAV plus a .json sidecar)")
    sa.add_argument("--seconds", type=float, default=60.0)
    sa.add_argument("--rpm", type=float, default=780.0)
    sa.add_argument("--cylinders", type=int, default=4)
    sa.add_argument("--misfire-every", type=int, default=0, metavar="N", help="drop every Nth firing pulse")
    sa.add_argument("--misfire-from", type=float, default=0.0, metavar="SEC", help="misfires start here")
    sa.add_argument("--misfire-for", type=float, metavar="SEC", help="misfire duration (default: to the end)")
    sa.add_argument("--cold", action="store_true", help="cold start (noisier)")
    sa.add_argument("--seed", type=int, default=1234)
    sa.add_argument("--out", required=True, help="WAV path; the sidecar is <out>.json")
    sa.set_defaults(fn=cmd_simulate_audio)

    dr = sub.add_parser("demo-run", help="attach all kit evidence (rig, OBD, audio) to an UpClose session in one go")
    dr.add_argument("--session", required=True, help="session UUID")
    dr.add_argument("--api", default="http://localhost:8000")
    dr.add_argument("--port", default="sim", help="'sim' (simulated pod) or a COM port")
    dr.add_argument("--baud", type=int, default=BAUD)
    dr.add_argument("--seconds", type=float, default=60.0)
    dr.add_argument("--baseline-s", type=float, default=20.0, help="healthy baseline length (min 20)")
    dr.add_argument("--misfire-at", default="35", metavar="SEC|none", help="sim only: misfire onset (default 35)")
    dr.add_argument("--obd", metavar="from-pod|clean|cleared|dtc:P0301",
                    help="emulated OBD state; from-pod: the pod's button injects P0301 and the knob sets RPM "
                         "(default with a COM port; the simulator defaults to clean)")
    dr.add_argument("--real-vehicle", action="store_true",
                    help="label the rig data 'live' (a real vehicle streaming now); bench runs are 'synthetic'")
    dr.add_argument("--audio", metavar="synth|PATH", help="engine audio: synthetic, or an existing WAV/MP3/M4A (default: skip)")
    dr.add_argument("--audio-provenance", choices=PROVENANCES, default="synthetic", help="provenance for a PATH clip")
    dr.add_argument("--tiger", action="store_true", help="also push the run to the Tiger Data store (run id = session id)")
    dr.add_argument("--speed", type=float, default=1.0, help="sim only: stream-time factor, 0 = as fast as possible")
    dr.add_argument("--seed", type=int, default=100, help="sim only")
    dr.add_argument("--out", help="output directory (default recordings/<session>)")
    dr.set_defaults(fn=cmd_demo_run)

    r = sub.add_parser("record", help="record the board's serial stream")
    r.add_argument("--port", required=True, help="COM port (or pyserial URL)")
    r.add_argument("--baud", type=int, default=BAUD)
    r.add_argument("--seconds", type=float, default=60.0)
    r.add_argument("--header-wait", type=float, default=10.0, help="extra seconds allowed for the board to boot")
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_record)

    m = sub.add_parser("monitor", help="live print of parsed values")
    m.add_argument("--port", required=True)
    m.add_argument("--baud", type=int, default=BAUD)
    m.set_defaults(fn=cmd_monitor)

    def rig_args(q: argparse.ArgumentParser) -> None:
        q.add_argument("--provenance", choices=PROVENANCES, default="synthetic")
        q.add_argument("--cylinders", type=int, default=4)
        q.add_argument("--start-s", type=float, default=0.0, help="window start, seconds into the recording")
        q.add_argument("--seconds", type=float, help="window length (needed for recordings over 240000 accel samples)")
        q.add_argument("--verdict", help="anomaly verdict JSON (anomaly-score / live output) to embed as data.model_verdict")

    b = sub.add_parser("bundle", help="build the contract JSON body from a recording")
    b.add_argument("--recording", required=True)
    b.add_argument("--kind", default="rig_telemetry")
    rig_args(b)
    b.add_argument("--out")
    b.set_defaults(fn=cmd_bundle)

    o = sub.add_parser("post", help="POST a bundle (token from UPCLOSE_OWNER_TOKEN)")
    o.add_argument("--recording", help="pod recording -> rig_telemetry")
    o.add_argument("--body", help="ready-made bundle JSON (e.g. from obd-snapshot)")
    o.add_argument("--kind", default="rig_telemetry")
    rig_args(o)
    o.add_argument("--api", default="http://localhost:8000")
    o.add_argument("--session", required=True)
    o.set_defaults(fn=cmd_post)

    d = sub.add_parser("obd-snapshot", help="query emulated (or real) ELM327 via python-OBD -> obd_snapshot")
    _add_obd_state_args(d)
    d.add_argument("--provenance", choices=PROVENANCES, default="synthetic")
    d.add_argument("--obd-port", help="real adapter port instead of the emulator (e.g. COM5)")
    d.add_argument("--out")
    d.set_defaults(fn=cmd_obd_snapshot)

    v = sub.add_parser("obd-serve", help="run the emulated ELM327 on a TCP port")
    _add_obd_state_args(v)
    v.add_argument("--port", type=int, default=35000)
    v.set_defaults(fn=cmd_obd_serve)

    af = sub.add_parser("anomaly-fit", help="fit an engine-anomaly model on a healthy baseline recording")
    af.add_argument("--recording", required=True, help="healthy baseline, at least 20 s")
    af.add_argument("--audio", help="optional baseline WAV (starts with the recording)")
    af.add_argument("--out", required=True, help="model JSON")
    af.set_defaults(fn=cmd_anomaly_fit)

    asc = sub.add_parser("anomaly-score", help="print the anomaly verdict JSON for a recording")
    asc.add_argument("--model", required=True)
    asc.add_argument("--recording", required=True)
    asc.add_argument("--audio", help="WAV, required if the model was fitted with audio")
    asc.set_defaults(fn=cmd_anomaly_score)

    lv = sub.add_parser("live", help="score the pod stream live and alert on the pod (or --port sim)")
    lv.add_argument("--port", required=True, help="COM port, pyserial URL, or 'sim' (simulated pod, no hardware)")
    lv.add_argument("--baud", type=int, default=BAUD)
    lv.add_argument("--baseline-s", type=float, default=20.0, help="healthy baseline length (min 20)")
    lv.add_argument("--seconds", type=float, help="stop after this long (sim default 60; board default until Ctrl-C)")
    lv.add_argument("--record", help="also record the stream to this .upr; the verdict goes next to it")
    lv.add_argument("--verdict-out", help="verdict JSON path (default <record>.verdict.json)")
    lv.add_argument("--mute", action="store_true", help="start with the pod buzzer muted")
    lv.add_argument("--speed", type=float, default=1.0, help="sim only: stream-time factor, 0 = as fast as possible")
    lv.add_argument("--seed", type=int, default=100, help="sim only")
    lv.add_argument("--sim-rpm", type=float, default=780.0)
    lv.add_argument("--sim-misfire-at", type=float, default=35.0, help="sim: misfire onset, seconds")
    lv.add_argument("--sim-misfire-for", type=float, default=12.0, help="sim: misfire duration, seconds")
    lv.add_argument("--sim-misfire-every", type=int, default=3)
    lv.set_defaults(fn=cmd_live)

    ae = sub.add_parser("anomaly-eval", help="regenerate the synthetic evaluation report")
    ae.add_argument("--out", default="eval/anomaly_report.md")
    ae.set_defaults(fn=cmd_anomaly_eval)

    ti = sub.add_parser("tiger-init", help="create the Tiger Data hypertables and rollups")
    ti.set_defaults(fn=cmd_tiger_init)

    tp = sub.add_parser("tiger-push", help="push a .upr recording to the Tiger Data store")
    tp.add_argument("recording")
    tp.add_argument("--verdict", help="anomaly verdict JSON to store alongside the run")
    tp.add_argument("--provenance", choices=("synthetic", "real"), default="synthetic")
    tp.add_argument("--run-id", help="run id (default: recording file name)")
    tp.add_argument("--notes", default="")
    tp.set_defaults(fn=cmd_tiger_push)

    ts = sub.add_parser("tiger-summary", help="summarize a run from the per-second rollups")
    ts.add_argument("run_id")
    ts.set_defaults(fn=cmd_tiger_summary)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (PostError, BundleError, RecordingError, HeaderMissing, ProtocolError, ValueError, ValidationError, OSError) as exc:
        return _err(f"{type(exc).__name__}: {exc}" if isinstance(exc, ValidationError) else str(exc))
    except Exception as exc:  # obd / serial failures
        if type(exc).__name__ in ("ObdError", "SerialException"):
            return _err(str(exc))
        raise


if __name__ == "__main__":
    sys.exit(main())
