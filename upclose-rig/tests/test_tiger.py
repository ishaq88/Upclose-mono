"""Tiger Data store tests.

Pure tests use a fake connection and need no database. The live round-trip
runs only when TIGER_DATABASE_URL is already set, else it skips cleanly.
"""

import json
import os
from collections import deque
from datetime import datetime, timezone

import pytest

from upclose_rig import cli, tiger
from upclose_rig.protocol import Record
from upclose_rig.recording import Recording, make_header
from upclose_rig.tiger import TigerError

STARTED = "2026-11-07T21:00:00Z"


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, stmt, params=None):
        self.conn.executed.append(("execute", str(stmt), params))

    def executemany(self, stmt, rows):
        self.conn.executed.append(("executemany", str(stmt), list(rows)))

    def fetchone(self):
        return self.conn.queued.popleft()

    def fetchall(self):
        return list(self.conn.queued.popleft())


class FakeConn:
    def __init__(self, queued=()):
        self.executed = []
        self.queued = deque(queued)
        self.committed = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def tiny_recording(source="simulator"):
    return Recording(
        make_header(source, STARTED),
        [
            Record("A", 0, (1, 2, 3)),
            Record("A", 3, (4, 5, 6)),
            Record("S", 0, (500,)),
            Record("S", 10, (600,)),
            Record("T", 0, (36.5, 45.0)),
            Record("B", 5, (1,)),
            Record("K", 6, (512,)),
        ],
    )


def stmts(conn, op=None, contains=None):
    out = [e for e in conn.executed if (op is None or e[0] == op)]
    if contains is not None:
        out = [e for e in out if contains in e[1]]
    return out


# --- row mapping ------------------------------------------------------------


def test_sample_rows_mapping_skips_button_and_knob():
    rec = tiny_recording()
    rows = tiger.sample_rows(rec, "run1")
    assert len(rows) == 5
    assert [r[2] for r in rows] == ["A", "A", "S", "S", "T"]
    a0 = rows[0]
    assert a0[:3] == ("run1", datetime(2026, 11, 7, 21, 0, 0, tzinfo=timezone.utc), "A")
    assert a0[3:6] == (1, 2, 3) and a0[6:] == (None, None, None)
    assert rows[2][6] == 500 and rows[2][3:6] == (None, None, None)
    assert rows[4][7:] == (36.5, 45.0)
    # timestamps are recording start + offset from the first record
    assert (rows[1][1] - rows[0][1]).total_seconds() == pytest.approx(0.003)


def test_sample_rows_empty_recording():
    rec = Recording(make_header("simulator", STARTED), [])
    with pytest.raises(TigerError):
        tiger.sample_rows(rec, "run1")


def test_sample_rows_unknown_kind():
    rec = Recording(make_header("simulator", STARTED), [Record("Z", 0, (1,))])
    with pytest.raises(TigerError):
        tiger.sample_rows(rec, "run1")


def test_chunked_batching():
    rows = [(i,) for i in range(7)]
    chunks = list(tiger.chunked(rows, 3))
    assert [len(c) for c in chunks] == [3, 3, 1]
    assert [r for c in chunks for r in c] == rows
    with pytest.raises(TigerError):
        list(tiger.chunked(rows, 0))


# --- push -------------------------------------------------------------------


def test_push_uses_executemany_and_refreshes_rollup():
    conn = FakeConn()
    res = tiger.push_recording(conn, tiny_recording(), "run1", "synthetic")
    assert res == {"run_id": "run1", "samples": 5, "source": "simulator", "provenance": "synthetic"}
    assert conn.committed  # inserts commit before the out-of-transaction refresh
    run_stmt = stmts(conn, "execute", "INSERT INTO pod_runs")[0]
    assert run_stmt[2][0] == "run1" and run_stmt[2][3] == "synthetic"
    assert stmts(conn, "execute", "DELETE FROM pod_samples")
    assert stmts(conn, "execute", "DELETE FROM pod_verdicts")
    sample_inserts = stmts(conn, "executemany", "INSERT INTO pod_samples")
    assert sum(len(e[2]) for e in sample_inserts) == 5
    assert stmts(conn, "execute", "refresh_continuous_aggregate")


def test_push_maps_board_source_to_pod():
    conn = FakeConn()
    tiger.push_recording(conn, tiny_recording("board"), "run9", "real")
    run_stmt = stmts(conn, "execute", "INSERT INTO pod_runs")[0]
    assert run_stmt[2][2] == "pod"


def test_push_provenance_required_and_guarded():
    conn = FakeConn()
    for bad in ("", "live", "cached", "REAL"):
        with pytest.raises(TigerError):
            tiger.push_recording(conn, tiny_recording(), "r", bad)
    with pytest.raises(TigerError, match="synthetic"):
        tiger.push_recording(conn, tiny_recording("simulator"), "r", "real")
    with pytest.raises(TigerError):
        tiger.push_recording(conn, tiny_recording(), "", "synthetic")


# --- verdict ----------------------------------------------------------------


def test_insert_verdict_row_shape():
    conn = FakeConn()
    verdict = {"verdict": "anomaly_suspected", "max_score": 12.5,
               "top_features": [{"name": "rms", "share": 0.7}]}
    res = tiger.insert_verdict(conn, "run1", verdict, datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc))
    assert res == {"run_id": "run1", "state": "anomaly_suspected", "score": 12.5}
    stmt = stmts(conn, "execute", "INSERT INTO pod_verdicts")[0]
    assert stmt[2][2] == "anomaly_suspected" and stmt[2][3] == 12.5
    assert json.loads(stmt[2][4]) == [{"name": "rms", "share": 0.7}]


def test_insert_verdict_not_assessed_has_null_score():
    conn = FakeConn()
    tiger.insert_verdict(conn, "r", {"verdict": "not_assessed", "reason": "too_short"})
    stmt = stmts(conn, "execute", "INSERT INTO pod_verdicts")[0]
    assert stmt[2][2] == "not_assessed" and stmt[2][3] is None


def test_insert_verdict_rejects_bad_state():
    conn = FakeConn()
    with pytest.raises(TigerError):
        tiger.insert_verdict(conn, "r", {"verdict": "maybe"})
    with pytest.raises(TigerError):
        tiger.insert_verdict(conn, "r", ["not", "a", "dict"])


# --- summary ----------------------------------------------------------------


def test_summary_shape_over_rollup():
    bucket = datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc)
    conn = FakeConn(queued=[
        ("run1", datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc), "simulator", "synthetic", ""),
        (bucket, "no_anomaly_detected", 3.25, []),
        [(bucket, 400, 12.5, 510.0, 600)],
    ])
    s = tiger.summary(conn, "run1")
    assert s["run"] == {"run_id": "run1", "started_at": "2026-11-07T21:00:00+00:00",
                        "source": "simulator", "provenance": "synthetic", "notes": ""}
    assert s["verdict"]["state"] == "no_anomaly_detected"
    assert s["n_buckets"] == 1
    assert s["buckets"][0]["accel_rms"] == 12.5


def test_summary_without_verdict():
    conn = FakeConn(queued=[
        ("r", datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc), "pod", "real", "n"),
        None,
        [],
    ])
    s = tiger.summary(conn, "r")
    assert s["verdict"] is None and s["buckets"] == [] and s["n_buckets"] == 0


def test_summary_unknown_run():
    conn = FakeConn(queued=[None])
    with pytest.raises(TigerError, match="unknown run"):
        tiger.summary(conn, "ghost")


# --- schema file ------------------------------------------------------------


def test_schema_statements_parse():
    statements = tiger.read_schema_statements()
    assert len(statements) >= 8
    joined = "\n".join(statements)
    for name in ("pod_runs", "pod_samples", "pod_verdicts", "pod_samples_1s",
                 "create_hypertable", "add_continuous_aggregate_policy", "time_bucket"):
        assert name in joined
    assert "if_not_exists => TRUE" in joined
    assert all(s.split()[0] in ("CREATE", "SELECT") for s in statements)
    # statements are whole: the CREATE TABLE holds its columns, the view its query
    assert any("CREATE TABLE" in s and "PRIMARY KEY" in s for s in statements)
    assert any("CREATE MATERIALIZED VIEW" in s and "GROUP BY" in s for s in statements)


# --- the URL never leaks ----------------------------------------------------


SECRET = "s3cr3t-fake-value"


def test_missing_url_error_has_no_secret(monkeypatch):
    monkeypatch.delenv("TIGER_DATABASE_URL", raising=False)
    with pytest.raises(TigerError) as ei:
        tiger.database_url()
    assert SECRET not in str(ei.value)


def test_connection_failure_never_echoes_url(monkeypatch):
    import psycopg

    monkeypatch.setenv("TIGER_DATABASE_URL", f"postgres://u:{SECRET}@host/db")
    def boom(*args, **kwargs):
        raise OSError(f"connection failed, dsn held {SECRET}")
    monkeypatch.setattr(psycopg, "connect", boom)
    with pytest.raises(TigerError) as ei:
        tiger._connect()
    assert SECRET not in str(ei.value)
    assert "TIGER_DATABASE_URL" not in str(ei.value)


def test_push_failure_never_echoes_url(monkeypatch):
    monkeypatch.setenv("TIGER_DATABASE_URL", f"postgres://u:{SECRET}@host/db")

    class BadConn(FakeConn):
        def cursor(self):
            raise OSError(f"backend gone {SECRET}")

    with pytest.raises(TigerError) as ei:
        tiger.push_recording(BadConn(), tiny_recording(), "r", "synthetic")
    assert SECRET not in str(ei.value)


def test_cli_error_paths_keep_url_out_of_output(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TIGER_DATABASE_URL", f"postgres://u:{SECRET}@host/db")
    monkeypatch.delenv("TIGER_DATABASE_URL", raising=False)
    assert cli.main(["tiger-init"]) == 2
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err

    rec = tmp_path / "r.upr"
    rec.write_text(json.dumps({"format": "upclose-rig-recording"}) + "\n")
    assert cli.main(["tiger-push", str(rec), "--run-id", "x"]) == 2
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err


def test_tiger_help_works():
    for argv in (["tiger-push", "--help"], ["tiger-summary", "--help"], ["tiger-init", "--help"]):
        with pytest.raises(SystemExit) as ei:
            cli.main(argv)
        assert ei.value.code == 0


# --- live round-trip (needs TIGER_DATABASE_URL) -----------------------------


@pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs TIGER_DATABASE_URL")
def test_live_push_and_summary(tmp_path):
    from upclose_rig.recording import write_recording
    from upclose_rig.simulator import SimConfig, simulate

    recs = simulate(SimConfig(seconds=3, seed=7))
    header = make_header("simulator", STARTED, seed=7)
    path = tmp_path / "live.upr"
    n = write_recording(path, header, recs)
    assert n == len(recs) > 0

    run_id = "pytest-{}".format(int(datetime.now(timezone.utc).timestamp()))
    res = tiger.push(str(path), None, "synthetic", run_id=run_id)
    assert res["samples"] == len(recs)
    s = tiger.summarize(run_id)
    assert s["run"]["run_id"] == run_id
    assert s["run"]["provenance"] == "synthetic"
    assert s["n_buckets"] >= 2
