import json

import pytest
from pydantic import ValidationError

from upclose_rig.bundle import build_rig_bundle, load_verdict, to_json
from upclose_rig.cli import main
from upclose_rig.live import SimPort, encode_mute, encode_state, run_live, scripted_simulation
from upclose_rig.models import RigTelemetryEnvelope
from upclose_rig.recording import read_recording

ONSET_S, MISFIRE_FOR_S = 35.0, 12.0


def quiet(_: str) -> None:
    pass


@pytest.fixture(scope="module")
def sim_records():
    return scripted_simulation(60.0, misfire_at=ONSET_S, misfire_for=MISFIRE_FOR_S)


@pytest.fixture(scope="module")
def run(sim_records, tmp_path_factory):
    d = tmp_path_factory.mktemp("live")
    port = SimPort(sim_records, speed=0)
    res = run_live(port, baseline_s=20.0, record_path=d / "rec.upr", source="simulator", out=quiet)
    return res, port, d


# --- command encoding ---

def test_command_encoding():
    assert encode_state("L", 12) == "!S,L12"
    assert encode_state("L", 0) == "!S,L0"
    assert [encode_state(s) for s in "NAX"] == ["!S,N", "!S,A", "!S,X"]
    assert encode_mute(True) == "!M,1" and encode_mute(False) == "!M,0"
    for bad in (lambda: encode_state("L"), lambda: encode_state("L", 1000), lambda: encode_state("Q")):
        with pytest.raises(ValueError):
            bad()


def test_sim_port_acknowledges_like_the_pod():
    p = SimPort([], speed=0)
    p.write(b"!S,A\n!M,1\n")
    assert p.sent == ["!S,A", "!M,1"]
    assert p.readline() == b"#ack,S,A\n" and p.readline() == b"#ack,M,1\n"


# --- the live loop over the simulator ---

def test_baseline_then_normal_then_misfire_alert_then_clear(run):
    res, port, _ = run
    names = [e["event"] for e in res.events]
    assert names == ["baseline_ready", "anomaly_suspected", "anomaly_cleared"]
    ready, alert, cleared = res.events
    assert ready["t_ms"] == pytest.approx(20000, abs=300)
    assert ONSET_S * 1000 <= alert["t_ms"] <= (ONSET_S + 6) * 1000  # within a few seconds of onset
    assert (ONSET_S + MISFIRE_FOR_S) * 1000 <= cleared["t_ms"] <= (ONSET_S + MISFIRE_FOR_S + 8) * 1000
    assert alert["top_features"]


def test_pod_commands_follow_the_states(run):
    res, port, _ = run
    cmds = res.commands
    assert port.sent == cmds  # what the loop says it sent is what the pod got
    assert cmds[0].startswith("!S,L") and "!S,L1" in cmds or "!S,L2" in cmds
    first_a = cmds.index("!S,A")
    assert "!S,N" in cmds[:first_a]  # normal before the misfire
    assert "!S,N" in cmds[first_a:]  # and back to normal after it clears
    assert cmds[-1] == "!S,N"
    assert res.acks > 0
    # learning countdown only decreases
    left = [int(c[4:]) for c in cmds if c.startswith("!S,L")]
    assert left == sorted(left, reverse=True) and left[0] >= 19


def test_mute_command_is_sent_first(sim_records):
    port = SimPort(sim_records[:12000], speed=0)
    res = run_live(port, mute=True, out=quiet)
    assert res.commands[0] == "!M,1"


# --- robustness ---

class NoisyPort(SimPort):
    """Every 40th read returns junk the way a noisy serial line would."""

    JUNK = [b"\xff\xfeA,1\n", b"garbage\n", b"A,12,xx,3,4\n", b"Z,1,2\n", b"S,5\n", b"#err accel not found at 0x19\n", b"\n"]

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._n = 0

    def readline(self):
        self._n += 1
        if self._n > 1 and self._n % 40 == 0:
            return self.JUNK[(self._n // 40) % len(self.JUNK)]
        return super().readline()


def test_malformed_board_lines_are_tolerated(sim_records, run):
    clean, _, _ = run
    res = run_live(NoisyPort(sim_records, speed=0), baseline_s=20.0, source="simulator", out=quiet)
    assert res.dropped_lines > 100
    assert [e["event"] for e in res.events] == [e["event"] for e in clean.events]


def test_boot_noise_before_header_is_skipped(sim_records):
    class Boot(SimPort):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._boot = [b"\x00\x01rubbish\n", b"#upclose-pod vX\n"]

        def readline(self):
            return self._boot.pop(0) if self._boot else super().readline()

    res = run_live(Boot(sim_records[:3000], speed=0), out=quiet)
    assert len(res.records) > 2000


def test_flat_sensor_is_not_assessed_never_normal():
    from upclose_rig.protocol import Record

    recs = [Record("A", i * 5 // 2, (18, -12, 984)) for i in range(12000)]
    port = SimPort(recs, speed=0)
    res = run_live(port, baseline_s=20.0, out=quiet)
    assert [e["event"] for e in res.events] == ["not_assessed"]
    assert "!S,X" in res.commands and "!S,N" not in res.commands and "!S,A" not in res.commands


# --- verdict file ---

def test_verdict_file_is_written_next_to_the_recording_and_embeds_in_a_bundle(run):
    res, _, d = run
    vp = d / "rec.verdict.json"
    assert res.verdict_path == str(vp) and vp.exists()
    v = json.loads(vp.read_text())
    assert v["verdict"] == "anomaly_suspected" and v["data_source"] == "simulator"
    assert v["model"]["baseline_seconds"] == pytest.approx(20.0, abs=0.5)
    assert len(v["model"]["config_hash"]) == 64
    assert v["windows"] > 100 and 0 < v["anomalous_windows"] < v["windows"]
    assert v["duration_s"] == pytest.approx(60.0, abs=0.1)
    runs = v["anomalous_runs"]
    assert any(ONSET_S - 2 <= r["t_start_s"] <= ONSET_S + 3 for r in runs)  # relative to recording start
    env = build_rig_bundle(read_recording(d / "rec.upr"), "synthetic", verdict=load_verdict(vp))
    assert env.data.model_verdict is not None and env.data.model_verdict.verdict == "anomaly_suspected"


def test_no_verdict_when_baseline_never_completes(sim_records, tmp_path):
    port = SimPort(sim_records[:3000], speed=0)  # a few seconds only
    res = run_live(port, baseline_s=20.0, record_path=tmp_path / "r.upr", source="simulator", out=quiet)
    assert res.verdict is None and not (tmp_path / "r.verdict.json").exists()
    assert (tmp_path / "r.upr").exists()
    assert set(c[:4] for c in res.commands) == {"!S,L"}


def test_cli_live_sim(tmp_path, capsys):
    rec = tmp_path / "cli.upr"
    code = main(["live", "--port", "sim", "--speed", "0", "--seconds", "45", "--sim-misfire-at", "28",
                 "--sim-misfire-for", "8", "--record", str(rec)])
    assert code == 0
    out = capsys.readouterr().out
    assert "baseline_ready" in out and "anomaly_suspected" in out and "-> pod: !S,A" in out
    assert (tmp_path / "cli.verdict.json").exists()
    assert main(["live", "--port", "sim", "--baseline-s", "5"]) == 2


# --- bundle / post --verdict (contract: rig_telemetry.data.model_verdict) ---

def test_bundle_without_verdict_is_unchanged_and_with_verdict_validates(run):
    res, _, d = run
    rec = read_recording(d / "rec.upr")
    plain = build_rig_bundle(rec, "synthetic")
    assert plain.data.model_verdict is None
    assert "model_verdict" not in to_json(plain)
    assert to_json(plain) == plain.model_dump_json(exclude_none=True)  # same bytes as before the field existed
    with_v = build_rig_bundle(rec, "synthetic", verdict=res.verdict)
    body = json.loads(to_json(with_v))
    assert body["data"]["model_verdict"]["schema"] == "upclose-anomaly-verdict/1"
    assert body["data"]["accel"] == json.loads(to_json(plain))["data"]["accel"]
    RigTelemetryEnvelope.model_validate_json(to_json(with_v))  # round-trips


def test_not_assessed_verdict_keeps_its_nulls_in_the_body(run):
    res, _, d = run
    na = json.loads(json.dumps(res.verdict))
    na.update(verdict="not_assessed", reason="flat_signal", windows=None, anomalous_windows=None,
              anomalous_fraction=None, max_score=None, median_score=None, top_features=[], anomalous_runs=[])
    body = json.loads(to_json(build_rig_bundle(read_recording(d / "rec.upr"), "synthetic", verdict=na)))
    mv = body["data"]["model_verdict"]
    assert mv["windows"] is None and mv["anomalous_fraction"] is None and mv["reason"] == "flat_signal"


def test_inconsistent_or_malformed_verdicts_are_rejected(run):
    res, _, d = run
    rec = read_recording(d / "rec.upr")

    def bad(**changes):
        v = json.loads(json.dumps(res.verdict))
        v.update(changes)
        with pytest.raises(ValidationError):
            build_rig_bundle(rec, "synthetic", verdict=v)

    bad(anomalous_fraction=0.01)                      # != anomalous_windows / windows
    bad(verdict="no_anomaly_detected")                # counts say suspected
    bad(verdict="not_assessed")                       # not_assessed needs reason and null counts
    bad(extra_field=1)                                # strict shape
    bad(top_features=[{"name": "vib_crest", "share": 0.1}] * 6)  # bounded list
    bad(anomalous_runs=[{"t_start_s": 1.0, "t_end_s": 999.0, "peak_score": 5.0, "top_features": []}])  # outside recording
    bad(duration_s=float("nan"))


def test_cli_bundle_with_verdict_flag(run, tmp_path, capsys):
    res, _, d = run
    out = tmp_path / "b.json"
    assert main(["bundle", "--recording", str(d / "rec.upr"), "--verdict", str(d / "rec.verdict.json"), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["data"]["model_verdict"]["verdict"] == "anomaly_suspected"
    out2 = tmp_path / "plain.json"
    assert main(["bundle", "--recording", str(d / "rec.upr"), "--out", str(out2)]) == 0
    assert "model_verdict" not in out2.read_text()
    bad = tmp_path / "notjson.json"
    bad.write_text("nope")
    assert main(["bundle", "--recording", str(d / "rec.upr"), "--verdict", str(bad)]) == 2
