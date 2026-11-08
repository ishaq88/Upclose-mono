import json
import uuid

import httpx
import pytest

from upclose_rig import cli
from upclose_rig.demo import PIECE_BYTES, parse_obd

SID = str(uuid.uuid4())
TOKEN = "tok-secret-xyz"
STORE = "http://storage.test/put"


class FakeApi:
    """In-memory stand-in for the UpClose API and its presigned storage."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self.sources: list[dict] = []
        self.puts: list[bytes] = []
        self.fail_on = fail_on
        self.upload_id = str(uuid.uuid4())

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if req.url.host == "storage.test":
            self.puts.append(req.content)
            assert "authorization" not in req.headers  # no bearer to storage
            return httpx.Response(200)
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        if self.fail_on and path.endswith(self.fail_on):
            return httpx.Response(422, json={"detail": "nope"})
        if path.endswith(f"/sessions/{SID}/sources"):
            body = json.loads(req.content)
            self.sources.append(body)
            return httpx.Response(201, json={"source_id": f"src-{len(self.sources)}", "kind": body["kind"],
                                             "data_sha256": "ab" * 32,
                                             "summary": {"state": "assessed", "flag_count": 1, "flags": ["x"]}})
        if path.endswith(f"/sessions/{SID}/audio"):
            n = json.loads(req.content)["content_length"]
            return httpx.Response(201, json={"upload_id": self.upload_id, "chunk_count": -(-n // PIECE_BYTES)})
        if path.endswith("/urls"):
            nums = json.loads(req.content)["part_numbers"]
            return httpx.Response(200, json={"items": [
                {"part_number": k, "url": f"{STORE}/{k}", "expires_at": "2030-01-01T00:00:00Z", "headers": {}} for k in nums]})
        if path.endswith("/complete"):
            return httpx.Response(200, json={"upload_id": self.upload_id, "state": "accepted"})
        return httpx.Response(404, json={"detail": "unexpected " + path})


def args(tmp_path, **kw):
    ns = dict(session=SID, api="http://api.test", port="sim", baud=250000, seconds=40.0, baseline_s=20.0,
              misfire_at="25", obd="clean", audio=None, audio_provenance="synthetic", tiger=False,
              speed=0.0, seed=100, out=str(tmp_path / "run"), real_vehicle=False)
    ns.update(kw)
    return type("A", (), ns)()


def run(tmp_path, monkeypatch, api=None, **kw):
    monkeypatch.setenv("UPCLOSE_OWNER_TOKEN", TOKEN)
    api = api or FakeApi()
    lines: list[dict] = []
    from upclose_rig.demo import run_demo

    code = run_demo(args(tmp_path, **kw), client=api.client(), emit=lines.append)
    return code, lines, api


def test_full_run_posts_rig_obd_audio(tmp_path, monkeypatch):
    code, lines, api = run(tmp_path, monkeypatch, audio="synth")
    assert code == 0, lines
    steps = {s["step"]: s for s in lines}
    assert [s["step"] for s in lines] == ["live", "rig_telemetry", "obd_snapshot", "audio_upload", "engine_audio", "tiger"]
    assert steps["live"]["verdict"] == "anomaly_suspected" and steps["live"]["provenance"] == "synthetic"
    assert steps["rig_telemetry"]["http"] == 201 and steps["rig_telemetry"]["source_id"] == "src-1"
    assert steps["obd_snapshot"]["obd"] == "clean" and steps["audio_upload"]["pieces"] == 1
    assert steps["engine_audio"]["kind"] == "engine_audio" and steps["tiger"]["status"] == "skipped"
    kinds = [s["kind"] for s in api.sources]
    assert kinds == ["rig_telemetry", "obd_snapshot", "engine_audio"]
    assert api.sources[0]["data"]["model_verdict"]["verdict"] == "anomaly_suspected"
    assert api.sources[1]["data"]["mil_on"] is False and api.sources[1]["data"]["dtcs"] == []
    audio = api.sources[2]
    assert audio["data"]["upload_id"] == api.upload_id and audio["provenance"] == "synthetic"
    assert len(api.puts) == 1 and len(audio["data"]["sha256"]) == 64
    assert (tmp_path / "run" / "engine.wav.json").exists()
    assert TOKEN not in json.dumps(lines)


def test_audio_sha_matches_uploaded_bytes(tmp_path, monkeypatch):
    import hashlib

    _, _, api = run(tmp_path, monkeypatch, audio="synth")
    assert hashlib.sha256(api.puts[0]).hexdigest() == api.sources[2]["data"]["sha256"]


def test_without_audio_step_is_skipped(tmp_path, monkeypatch):
    code, lines, api = run(tmp_path, monkeypatch)
    assert code == 0 and [s["status"] for s in lines][-2:] == ["skipped", "skipped"]
    assert [s["kind"] for s in api.sources] == ["rig_telemetry", "obd_snapshot"]


def test_obd_dtc_and_cleared(tmp_path, monkeypatch):
    _, _, api = run(tmp_path, monkeypatch, obd="dtc:P0301")
    d = api.sources[1]["data"]
    assert d["mil_on"] is True and [x["code"] for x in d["dtcs"]] == ["P0301"]
    _, _, api = run(tmp_path, monkeypatch, obd="cleared")
    d = api.sources[1]["data"]
    assert d["mil_on"] is False and d["dtcs"] == []
    assert "incomplete" in d["readiness"].values()


def test_stops_at_first_failure_and_exits_nonzero(tmp_path, monkeypatch):
    code, lines, api = run(tmp_path, monkeypatch, api=FakeApi(fail_on="/sources"), audio="synth")
    assert code == 1
    assert [s["step"] for s in lines] == ["live", "rig_telemetry"]
    assert lines[-1]["status"] == "failed" and lines[-1]["http"] == 422
    assert not api.sources and not api.puts


def test_audio_failure_stops_before_source(tmp_path, monkeypatch):
    code, lines, api = run(tmp_path, monkeypatch, api=FakeApi(fail_on="/complete"), audio="synth")
    assert code == 1 and lines[-1]["step"] == "audio_upload" and lines[-1]["status"] == "failed"
    assert [s["kind"] for s in api.sources] == ["rig_telemetry", "obd_snapshot"]


def test_audio_file_path(tmp_path, monkeypatch):
    from upclose_rig.audio import AudioConfig, write_synthetic

    wav = tmp_path / "clip.wav"
    write_synthetic(wav, AudioConfig(seconds=2))
    code, lines, api = run(tmp_path, monkeypatch, audio=str(wav), audio_provenance="cached")
    assert code == 0 and api.sources[2]["provenance"] == "cached"
    assert api.puts[0] == wav.read_bytes()


def test_no_misfire_gives_no_anomaly(tmp_path, monkeypatch):
    code, lines, _ = run(tmp_path, monkeypatch, misfire_at="none")
    assert code == 0 and lines[0]["verdict"] == "no_anomaly_detected"


def test_baseline_never_completes_fails_live_step(tmp_path, monkeypatch):
    code, lines, api = run(tmp_path, monkeypatch, seconds=10.0)
    assert code == 1 and lines[-1]["step"] == "live" and lines[-1]["status"] == "failed" and not api.calls


def test_token_required_and_never_in_output(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UPCLOSE_OWNER_TOKEN", raising=False)
    from upclose_rig.demo import run_demo

    assert run_demo(args(tmp_path), client=FakeApi().client()) == 2
    out = capsys.readouterr()
    assert "UPCLOSE_OWNER_TOKEN" in out.out and json.loads(out.out)["step"] == "args"


def test_bad_args_rejected(tmp_path, monkeypatch):
    for kw in ({"session": "nope"}, {"obd": "weird"}, {"baseline_s": 5.0}, {"audio": str(tmp_path / "missing.wav")}):
        code, lines, api = run(tmp_path, monkeypatch, **kw)
        assert code == 2 and lines[0]["status"] == "failed" and not api.calls


def test_parse_obd():
    assert parse_obd("clean") == ("clean", []) and parse_obd("dtc:P0301") == ("dtc", ["P0301"])
    with pytest.raises(ValueError):
        parse_obd("dtc:zzz")


def test_cli_help():
    with pytest.raises(SystemExit) as e:
        cli.main(["demo-run", "--help"])
    assert e.value.code == 0


def test_tiger_step_uses_session_as_run_id(tmp_path, monkeypatch):
    from upclose_rig import tiger

    seen = {}

    def fake_push(rec, verdict, prov, run_id=None, notes=""):
        seen.update(rec=rec, verdict=verdict, prov=prov, run_id=run_id)
        return {"run_id": run_id, "samples": 7, "verdict_state": "anomaly_suspected"}

    monkeypatch.setattr(tiger, "push", fake_push)
    code, lines, _ = run(tmp_path, monkeypatch, tiger=True)
    assert code == 0 and lines[-1]["step"] == "tiger" and lines[-1]["run_id"] == SID
    assert seen["prov"] == "synthetic" and seen["run_id"] == SID


# --- pod-driven OBD, provenance, terminal output ----------------------------

def _pod_records(press=True):
    from upclose_rig.live import scripted_simulation
    from upclose_rig.protocol import Record

    recs = scripted_simulation(40.0, misfire_at=25.0)
    extra = [Record("K", 5000, (512,))] + ([Record("B", 26000, (1,))] if press else [])
    return sorted(recs + extra, key=lambda r: r.t_ms)


def _fake_pod(monkeypatch, press=True):
    import contextlib

    from upclose_rig import demo, serial_io
    from upclose_rig.live import SimPort

    recs = _pod_records(press)

    @contextlib.contextmanager
    def fake_open(port, baud):
        yield SimPort(recs, speed=0)

    monkeypatch.setattr(serial_io, "open_port", fake_open)


def test_from_pod_button_injects_dtc_and_knob_sets_rpm(tmp_path, monkeypatch, capsys):
    _fake_pod(monkeypatch)
    code, lines, api = run(tmp_path, monkeypatch, port="COM9", obd=None, seconds=40.0)
    assert code == 0, lines
    d = api.sources[1]["data"]
    assert d["mil_on"] is True and [x["code"] for x in d["dtcs"]] == ["P0301"]
    assert {s["step"]: s for s in lines}["obd_snapshot"]["obd"] == "dtc:P0301"
    assert any(abs(v - 1800.0) < 5 for v in d["pids"].values())  # knob 512 -> about 1800 rpm
    err = capsys.readouterr().err
    assert "BUTTON PRESSED   P0301 will be reported" in err and err.count("BUTTON PRESSED") == 1


def test_from_pod_without_press_is_clean(tmp_path, monkeypatch):
    _fake_pod(monkeypatch, press=False)
    code, lines, api = run(tmp_path, monkeypatch, port="COM9", obd=None, seconds=40.0)
    d = api.sources[1]["data"]
    assert code == 0 and d["mil_on"] is False and d["dtcs"] == []


def test_explicit_obd_overrides_pod(tmp_path, monkeypatch):
    _fake_pod(monkeypatch, press=True)
    code, lines, api = run(tmp_path, monkeypatch, port="COM9", obd="clean", seconds=40.0)
    assert code == 0 and api.sources[1]["data"]["dtcs"] == []


def test_bench_pod_is_synthetic_and_real_vehicle_is_live(tmp_path, monkeypatch):
    _fake_pod(monkeypatch)
    code, lines, api = run(tmp_path, monkeypatch, port="COM9", obd=None, seconds=40.0)
    assert code == 0 and lines[0]["provenance"] == "synthetic"
    assert [s["provenance"] for s in api.sources] == ["synthetic", "synthetic"]
    _fake_pod(monkeypatch)
    code, lines, api = run(tmp_path, monkeypatch, port="COM9", obd=None, seconds=40.0, real_vehicle=True)
    assert code == 0 and lines[0]["provenance"] == "live"
    assert [s["provenance"] for s in api.sources] == ["live", "synthetic"]  # the emulated ECU never says live


def test_real_vehicle_needs_a_real_pod(tmp_path, monkeypatch):
    code, lines, api = run(tmp_path, monkeypatch, real_vehicle=True)
    assert code == 2 and "real-vehicle" in lines[0]["error"] and not api.calls


def test_tiger_provenance_follows_real_vehicle(tmp_path, monkeypatch):
    from upclose_rig import tiger

    seen = []
    monkeypatch.setattr(tiger, "push", lambda rec, v, prov, run_id=None, notes="": seen.append(prov) or {"run_id": run_id, "samples": 1})
    _fake_pod(monkeypatch)
    run(tmp_path, monkeypatch, port="COM9", obd=None, seconds=40.0, tiger=True)
    _fake_pod(monkeypatch)
    run(tmp_path, monkeypatch, port="COM9", obd=None, seconds=40.0, tiger=True, real_vehicle=True)
    assert seen == ["synthetic", "real"]


def test_narrator_one_line_per_phase():
    from upclose_rig.demo import Narrator

    out = []
    n = Narrator(out.append)
    for m in ["  -> pod: !S,L20", "  -> pod: !S,L19", "  -> pod: !S,L15", "  -> pod: !S,L14", "  -> pod: !S,L10",
              "[   20.00s] baseline_ready x=1", "  -> pod: !S,N", "  -> pod: !S,N", "  -> pod: !S,A", "  -> pod: !S,A",
              "  -> pod: !S,N", "wrote x.upr: 5 records", "no verdict: boom"]:
        n(m)
    assert out[0].startswith("LEARNING") and "20 s" in out[0]
    assert [o.split()[0] for o in out] == ["LEARNING", "LEARNING", "LEARNING", "NORMAL", "MISFIRE", "NORMAL", "no"]


def test_status_lines_on_stderr_without_token(tmp_path, monkeypatch, capsys):
    code, lines, _ = run(tmp_path, monkeypatch, audio="synth")
    err = capsys.readouterr().err
    assert code == 0 and "[ OK ] rig_telemetry HTTP 201" in err and "[ OK ] engine_audio" in err
    assert "LEARNING" in err and "MISFIRE SUSPECTED" in err and TOKEN not in err


def test_failure_prints_fail_line(tmp_path, monkeypatch, capsys):
    run(tmp_path, monkeypatch, api=FakeApi(fail_on="/sources"))
    assert "[FAIL] rig_telemetry" in capsys.readouterr().err
