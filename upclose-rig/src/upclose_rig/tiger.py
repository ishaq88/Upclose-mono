"""Tiger Data (Timescale) store for Grove pod telemetry.

Raw pod samples and anomaly verdicts are stored as time series in a Tiger
Cloud database, separate from the UpClose platform database, so a run can be
queried per second (vibration RMS, sound level, anomaly score).

The connection string is read from the ``TIGER_DATABASE_URL`` environment
variable only. It is never printed, never taken from argv, and never included
in exception or log text.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .bundle import load_verdict
from .recording import Recording, read_recording

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "sql" / "tiger_schema.sql"
ENV_VAR = "TIGER_DATABASE_URL"
PROVENANCES = ("synthetic", "real")
# Recording sources ("board" | "simulator") mapped onto pod_runs.source.
SOURCES = {"board": "pod", "simulator": "simulator"}
VERDICT_STATES = ("anomaly_suspected", "no_anomaly_detected", "not_assessed")
INSERT_BATCH = 5000


class TigerError(ValueError):
    """Tiger store failure. Never carries the connection string."""


def database_url() -> str:
    """Return the Tiger connection string, or raise without echoing it."""
    url = os.environ.get(ENV_VAR, "")
    if not url:
        raise TigerError(f"{ENV_VAR} is not set")
    return url


def split_statements(sql: str) -> list[str]:
    """Split schema SQL into executable statements (strips `--` comments)."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def read_schema_statements() -> list[str]:
    try:
        return split_statements(SCHEMA_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TigerError(f"could not read schema file: {exc.strerror or type(exc).__name__}") from exc


def _connect() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise TigerError('psycopg is not installed; run pip install "psycopg[binary]>=3.1"') from exc
    try:
        return psycopg.connect(database_url())
    except Exception as exc:
        raise TigerError(f"could not connect to the Tiger Data store ({type(exc).__name__})") from exc


def apply_schema(conn: Any) -> int:
    """Apply every statement of the schema file. Returns the statement count."""
    statements = read_schema_statements()
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
    except Exception as exc:
        raise TigerError(f"could not apply the Tiger schema ({type(exc).__name__})") from exc
    return len(statements)


def recording_start(rec: Recording) -> datetime:
    try:
        return datetime.fromisoformat(str(rec.header["started_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise TigerError("recording header has no usable started_at") from exc


def sample_rows(
    rec: Recording, run_id: str, start: datetime | None = None
) -> list[tuple[Any, ...]]:
    """Map recording records onto pod_samples rows.

    Button (B) and knob (K) records have no columns and are skipped; every
    other kind must be A, S or T. Timestamps are absolute (recording start +
    the record's offset from the first record, mirroring the bundle builder).
    """
    if not rec.records:
        raise TigerError("recording is empty")
    start = start or recording_start(rec)
    t0 = min(r.t_ms for r in rec.records)
    rows: list[tuple[Any, ...]] = []
    for r in rec.records:
        ts = start + timedelta(milliseconds=r.t_ms - t0)
        if r.kind == "A":
            rows.append((run_id, ts, "A", int(r.values[0]), int(r.values[1]), int(r.values[2]), None, None, None))
        elif r.kind == "S":
            rows.append((run_id, ts, "S", None, None, None, int(r.values[0]), None, None))
        elif r.kind == "T":
            rows.append((run_id, ts, "T", None, None, None, None, float(r.values[0]), float(r.values[1])))
        elif r.kind in ("B", "K"):
            continue
        else:
            raise TigerError(f"unsupported record kind {r.kind!r}")
    return rows


def chunked(rows: Sequence[tuple[Any, ...]], size: int = INSERT_BATCH) -> Iterator[Sequence[tuple[Any, ...]]]:
    if size <= 0:
        raise TigerError("batch size must be positive")
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


SAMPLE_COLUMNS = "(run_id, ts, kind, ax_mg, ay_mg, az_mg, sound, temp_c, rh)"


def push_recording(
    conn: Any, rec: Recording, run_id: str, provenance: str, notes: str = ""
) -> dict[str, Any]:
    """Store a run and all its samples. Re-pushing a run replaces its rows."""
    if provenance not in PROVENANCES:
        raise TigerError(f"provenance must be one of {PROVENANCES}")
    source = SOURCES.get(rec.source)
    if source is None:
        raise TigerError(f"recording source {rec.source!r} has no Tiger source mapping")
    if rec.source == "simulator" and provenance != "synthetic":
        raise TigerError("simulator recordings must be pushed with provenance 'synthetic'")
    if not run_id:
        raise TigerError("run_id is required")
    started = recording_start(rec)
    rows = sample_rows(rec, run_id, started)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pod_runs (run_id, started_at, source, provenance, notes)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (run_id) DO UPDATE SET started_at = EXCLUDED.started_at,"
                " source = EXCLUDED.source, provenance = EXCLUDED.provenance, notes = EXCLUDED.notes",
                (run_id, started, source, provenance, notes),
            )
            cur.execute("DELETE FROM pod_samples WHERE run_id = %s", (run_id,))
            cur.execute("DELETE FROM pod_verdicts WHERE run_id = %s", (run_id,))
            for batch in chunked(rows):
                cur.executemany(
                    f"INSERT INTO pod_samples {SAMPLE_COLUMNS} VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    batch,
                )
    except TigerError:
        raise
    except Exception as exc:
        raise TigerError(f"could not push run {run_id!r} ({type(exc).__name__})") from exc
    conn.commit()
    # Timescale forbids the manual rollup refresh inside a transaction block,
    # so it runs on its own after the inserts are committed.
    autocommit = getattr(conn, "autocommit", False)
    try:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute("CALL refresh_continuous_aggregate('pod_samples_1s', NULL, NULL)")
        finally:
            conn.autocommit = autocommit
    except Exception as exc:
        raise TigerError(f"run {run_id!r} is stored but the rollup refresh failed ({type(exc).__name__})") from exc
    return {"run_id": run_id, "samples": len(rows), "source": source, "provenance": provenance}


def insert_verdict(conn: Any, run_id: str, verdict: dict[str, Any], ts: datetime | None = None) -> dict[str, Any]:
    """Store one anomaly verdict for a run."""
    if not isinstance(verdict, dict):
        raise TigerError("verdict must be a JSON object")
    state = verdict.get("verdict")
    if state not in VERDICT_STATES:
        raise TigerError(f"verdict state must be one of {VERDICT_STATES}")
    score = verdict.get("max_score")
    if score is not None:
        try:
            score = float(score)
        except (TypeError, ValueError) as exc:
            raise TigerError("verdict max_score is not a number") from exc
    top = verdict.get("top_features") or []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pod_verdicts (run_id, ts, state, score, top_features)"
                " VALUES (%s, %s, %s, %s, %s::jsonb)",
                (run_id, ts or datetime.now(timezone.utc), state, score, json.dumps(top)),
            )
    except TigerError:
        raise
    except Exception as exc:
        raise TigerError(f"could not store the verdict for run {run_id!r} ({type(exc).__name__})") from exc
    return {"run_id": run_id, "state": state, "score": score}


def summary(conn: Any, run_id: str) -> dict[str, Any]:
    """Summarize a run from the per-second continuous aggregate."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT run_id, started_at, source, provenance, notes FROM pod_runs WHERE run_id = %s",
                (run_id,),
            )
            run = cur.fetchone()
            if run is None:
                raise TigerError(f"unknown run {run_id!r}")
            cur.execute(
                "SELECT ts, state, score, top_features FROM pod_verdicts"
                " WHERE run_id = %s ORDER BY ts DESC LIMIT 1",
                (run_id,),
            )
            vrow = cur.fetchone()
            cur.execute(
                "SELECT bucket, n_accel, accel_rms, sound_mean, sound_max FROM pod_samples_1s"
                " WHERE run_id = %s ORDER BY bucket",
                (run_id,),
            )
            brows = cur.fetchall()
    except TigerError:
        raise
    except Exception as exc:
        raise TigerError(f"could not summarize run {run_id!r} ({type(exc).__name__})") from exc
    verdict = (
        None
        if vrow is None
        else {"ts": vrow[0].isoformat() if hasattr(vrow[0], "isoformat") else str(vrow[0]),
              "state": vrow[1], "score": vrow[2], "top_features": vrow[3]}
    )
    buckets = [
        {"bucket": b[0].isoformat() if hasattr(b[0], "isoformat") else str(b[0]),
         "n_accel": b[1], "accel_rms": b[2], "sound_mean": b[3], "sound_max": b[4]}
        for b in brows
    ]
    started = run[1]
    return {
        "run": {"run_id": run[0],
                "started_at": started.isoformat() if hasattr(started, "isoformat") else str(started),
                "source": run[2], "provenance": run[3], "notes": run[4]},
        "verdict": verdict,
        "buckets": buckets,
        "n_buckets": len(buckets),
    }


# --- one-shot helpers used by the CLI ---------------------------------------


def init() -> int:
    conn = _connect()
    try:
        n = apply_schema(conn)
        conn.commit()
        return n
    finally:
        conn.close()


def push(
    recording: str | Path,
    verdict_path: str | Path | None,
    provenance: str,
    run_id: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    rec = read_recording(recording)
    rid = run_id or Path(recording).stem
    verdict = load_verdict(verdict_path) if verdict_path else None
    conn = _connect()
    try:
        result = push_recording(conn, rec, rid, provenance, notes)
        if verdict is not None:
            vres = insert_verdict(conn, rid, verdict, recording_start(rec))
            result["verdict_state"] = vres["state"]
        conn.commit()
        return result
    finally:
        conn.close()


def summarize(run_id: str) -> dict[str, Any]:
    conn = _connect()
    try:
        return summary(conn, run_id)
    finally:
        conn.close()
