import json

import httpx

from upclose_rig import cli, post


def run(*argv):
    return cli.main([str(a) for a in argv])


def test_simulate_bundle_end_to_end(tmp_path, capsys):
    rec, out = tmp_path / "r.jsonl", tmp_path / "b.json"
    assert run("simulate", "--seconds", 5, "--out", rec, "--started-at", "2026-11-07T21:00:00Z") == 0
    assert run("bundle", "--recording", rec, "--provenance", "synthetic", "--out", out) == 0
    body = json.loads(out.read_text())
    assert body["provenance"] == "synthetic" and len(body["data"]["accel"]["x_mg"]) == 2000
    # simulator data cannot be labelled live
    assert run("bundle", "--recording", rec, "--provenance", "live") == 2
    assert "synthetic" in capsys.readouterr().err


def test_post_cli_paths(tmp_path, monkeypatch, capsys):
    rec = tmp_path / "r.jsonl"
    run("simulate", "--seconds", 3, "--out", rec)
    sid = "123e4567-e89b-42d3-a456-426614174000"
    monkeypatch.delenv("UPCLOSE_OWNER_TOKEN", raising=False)
    assert run("post", "--recording", rec, "--session", sid) == 2  # no token

    monkeypatch.setenv("UPCLOSE_OWNER_TOKEN", "t0k")
    status = {"v": 201}
    real = httpx.Client

    def handler(req):
        if status["v"] == 201:
            return httpx.Response(201, json={"source_id": "s", "kind": "rig_telemetry", "data_sha256": "h", "summary": {}})
        return httpx.Response(status["v"], json={"detail": "no"})

    monkeypatch.setattr(post.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))
    assert run("post", "--recording", rec, "--session", sid) == 0
    status["v"] = 409
    assert run("post", "--recording", rec, "--session", sid) == 1
    assert "finalized" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"kind": "obd_snapshot", "provenance": "synthetic"}))
    assert run("post", "--body", bad, "--session", sid) == 2  # fails client-side validation


def test_obd_snapshot_cli_cleared_and_provenance_guard(tmp_path):
    out = tmp_path / "o.json"
    assert run("obd-snapshot", "--cleared", "--out", out) == 0
    d = json.loads(out.read_text())["data"]
    assert d["dtcs"] == [] and d["warmups_since_codes_cleared"] == 0 and d["mil_on"] is False
    assert run("obd-snapshot", "--provenance", "live") == 2
    out2 = tmp_path / "o2.json"
    assert run("obd-snapshot", "--dtc", "P0301", "--dtc", "P0420", "--rpm", "1500", "--out", out2) == 0
    d2 = json.loads(out2.read_text())["data"]
    assert [x["code"] for x in d2["dtcs"]] == ["P0301", "P0420"] and d2["pids"]["rpm"] == 1500.0
    assert run("obd-snapshot", "--dtc", "BAD", "--out", tmp_path / "x.json") == 2


def test_obd_snapshot_from_recording_link(tmp_path):
    rec, out = tmp_path / "r.jsonl", tmp_path / "o.json"
    run("simulate", "--seconds", 2, "--event", "B@1=1", "--event", "K@0.5=1023", "--out", rec)
    assert run("obd-snapshot", "--from-recording", rec, "--out", out) == 0
    d = json.loads(out.read_text())["data"]
    assert d["dtcs"][0]["code"] == "P0301" and d["pids"]["rpm"] == 3000.0
