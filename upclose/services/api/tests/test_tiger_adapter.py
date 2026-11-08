"""Tiger adapter tests. No database needed except the live test, which skips cleanly
when TIGER_DATABASE_URL is unset (and when the smoke run is absent)."""

import os
from datetime import datetime, timezone

import pytest

from app.adapters.tiger import TigerSeriesResult, vibration_series
from app.adapters.tiger.series import REASONS

SECRET = "s3cr3t-fake-value"
URL = f"postgres://u:{SECRET}@host/db"
T0 = datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, script):
        self.script = list(script)
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, stmt, params=None):
        self.statements.append((stmt, params))
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action

    def fetchone(self):
        return self.script.pop(0)

    def fetchall(self):
        return self.script.pop(0)


class FakeConn:
    def __init__(self, script):
        self.cursor_obj = FakeCursor(script)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def rows():
    return [(T0, 400, 988.0, 580.0, 730), (T0.replace(second=1), 400, 990.5, 581.0, 744)]


def test_no_url_returns_none_with_code():
    result = vibration_series("some-run", environ={})
    assert result == TigerSeriesResult(None, "no_url")


def test_bad_run_id_rejected():
    assert vibration_series("", environ={"TIGER_DATABASE_URL": URL}).reason == "bad_run_id"


def test_rows_map_to_relative_series():
    conns = []

    def connect(url, **kwargs):
        assert url == URL and kwargs["connect_timeout"] == 5
        conn = FakeConn(["ok", ("run1",), "ok", rows()])
        conns.append(conn)
        return conn

    result = vibration_series("run1", environ={"TIGER_DATABASE_URL": URL}, connect=connect)
    assert result.reason is None and result.series is not None
    assert result.series.state == "assessed"
    assert [(p.t_s, p.n_accel) for p in result.series.points] == [(0.0, 400), (1.0, 400)]
    assert [p.accel_rms for p in result.series.points] == [988.0, 990.5]
    assert [p.sound_max for p in result.series.points] == [730, 744]
    assert result.series.points[0].sound_mean == 580.0
    assert conns[0].closed
    _, params = conns[0].cursor_obj.statements[1]
    assert params == ("run1", 601)


def test_unknown_run_and_empty_buckets():
    conn = lambda script: FakeConn(script)  # noqa: E731
    assert vibration_series("ghost", {"TIGER_DATABASE_URL": URL},
                            lambda url, **kw: conn(["ok", None])).reason == "unknown_run"
    assert vibration_series("run1", {"TIGER_DATABASE_URL": URL},
                            lambda url, **kw: conn(["ok", ("run1",), "ok", []])).reason == "no_buckets"


def test_failures_return_codes_never_the_url():
    def boom(url, **kwargs):
        raise OSError(f"connect failed, dsn held {SECRET}")

    result = vibration_series("run1", {"TIGER_DATABASE_URL": URL}, boom)
    assert result == TigerSeriesResult(None, "connection_failed")
    assert SECRET not in repr(result) and result.reason in REASONS

    QueryCanceled = type("QueryCanceled", (Exception,), {})
    failing = FakeConn([QueryCanceled(f"canceling statement due to timeout, {SECRET}")])
    result = vibration_series("run1", {"TIGER_DATABASE_URL": URL}, lambda url, **kw: failing)
    assert result.reason == "timeout"
    assert SECRET not in repr(result)

    broken = FakeConn([OSError(f"backend gone {SECRET}")])
    result = vibration_series("run1", {"TIGER_DATABASE_URL": URL}, lambda url, **kw: broken)
    assert result.reason == "query_failed"
    assert SECRET not in repr(result)


@pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs TIGER_DATABASE_URL")
def test_live_reads_smoke_run():
    result = vibration_series("tiger-smoke-1")
    if result.reason in ("unknown_run", "no_buckets"):
        pytest.skip("smoke run absent from the Tiger store")
    assert result.reason is None and result.series is not None
    assert result.series.state == "assessed" and len(result.series.points) >= 1
