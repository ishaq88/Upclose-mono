"""Read-only Tiger Data (Timescale) adapter: per-second vibration series for one run.

Reads the `pod_samples_1s` continuous aggregate only; it never writes. Any failure returns
a result with `series=None` and a stable reason code. The connection string never appears
in results, exceptions, or logs.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.adapters.tiger.config import CONNECT_TIMEOUT_S, MAX_POINTS, STATEMENT_TIMEOUT_MS, database_url
from app.domain.vibration_series import SeriesPoint, VibrationSeries

RunQuery = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class TigerSeriesResult:
    series: VibrationSeries | None
    reason: str | None  # None on success, else one of the codes below


REASONS = ("no_url", "bad_run_id", "timeout", "connection_failed", "query_failed",
           "unknown_run", "no_buckets")
SOURCE: Literal["tiger"] = "tiger"


def _points(rows: list[tuple[Any, ...]]) -> tuple[SeriesPoint, ...]:
    first = rows[0][0]
    points = []
    for bucket, n_accel, accel_rms, sound_mean, sound_max in rows:
        delta = bucket - first
        seconds = delta.total_seconds() if hasattr(delta, "total_seconds") else float(delta)
        points.append(SeriesPoint(
            t_s=round(float(seconds), 3), n_accel=int(n_accel or 0),
            accel_rms=None if accel_rms is None else round(float(accel_rms), 3),
            sound_mean=None if sound_mean is None else round(float(sound_mean), 3),
            sound_max=None if sound_max is None else int(sound_max),
        ))
    return tuple(points)


def vibration_series(run_id: str, environ: Mapping[str, str] | None = None,
                     connect: Callable[..., Any] | None = None) -> TigerSeriesResult:
    """Fetch the frozen per-second rollup for `run_id` (`<session id>` on the rig side)."""
    if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
        return TigerSeriesResult(None, "bad_run_id")
    url = database_url(environ)
    if not url:
        return TigerSeriesResult(None, "no_url")
    if connect is None:
        import psycopg

        connect = psycopg.connect
    try:
        conn = connect(url, connect_timeout=CONNECT_TIMEOUT_S,
                       options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    except Exception:
        return TigerSeriesResult(None, "connection_failed")
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT 1 FROM pod_runs WHERE run_id = %s", (run_id,))
                if cur.fetchone() is None:
                    return TigerSeriesResult(None, "unknown_run")
                cur.execute(
                    "SELECT bucket, n_accel, accel_rms, sound_mean, sound_max"
                    " FROM pod_samples_1s WHERE run_id = %s ORDER BY bucket LIMIT %s",
                    (run_id, MAX_POINTS + 1),
                )
                rows = cur.fetchall()
            except Exception as exc:
                if type(exc).__name__ in ("QueryCanceled",) or "timeout" in type(exc).__name__.lower():
                    return TigerSeriesResult(None, "timeout")
                return TigerSeriesResult(None, "query_failed")
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not rows:
        return TigerSeriesResult(None, "no_buckets")
    points = _points(rows)
    return TigerSeriesResult(
        VibrationSeries(state="assessed", reason=None, sample_count=sum(p.n_accel for p in points),
                        duration_s=round(points[-1].t_s - points[0].t_s, 3) if len(points) > 1 else 0.0,
                        points=points),
        None,
    )
