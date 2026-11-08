"""Server-side vibration chart: RMS line over time with anomalous-run bands. Pure function of its
inputs (no I/O, no clock): the same series always renders the same bytes. All text is escaped."""
import math
from xml.sax.saxutils import escape

from app.contracts.sources import SourceProvenance, VerdictRun
from app.domain.vibration_series import VibrationSeries

WIDTH, HEIGHT = 640, 360
MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 56, 12, 16, 44

SOURCE_LABELS = {"tiger": "Tiger Data", "computed": "computed from the frozen samples"}
PROVENANCE_LABELS = {"synthetic": "Simulated bench data", "cached": "Replayed recording", "live": "Live"}


def _nice_ceiling(value: float) -> float:
    if value <= 0:
        return 1.0
    magnitude = 10.0 ** math.floor(math.log10(value))
    for multiple in (1.0, 2.0, 2.5, 5.0, 10.0):
        if multiple * magnitude >= value:
            return multiple * magnitude
    return 10.0 * magnitude  # pragma: no cover - loop always returns


def _ticks(upper: float, count: int) -> list[float]:
    if upper <= 0:
        return [0.0]
    step = _nice_ceiling(upper / count)
    ticks = []
    value = 0.0
    while value <= upper + step / 2:
        ticks.append(round(value, 6))
        value += step
    return ticks


def _placeholder(title: str, desc: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="160" viewBox="0 0 {WIDTH} 160"'
        f' role="img"><title>{escape(title)}</title><desc>{escape(desc)}</desc>'
        f'<text x="{WIDTH // 2}" y="80" text-anchor="middle" font-family="sans-serif" font-size="14">'
        f"{escape(body)}</text></svg>"
    )


def render_vibration_svg(series: VibrationSeries,
                         anomalous_runs: tuple[VerdictRun, ...] = (),
                         provenance: SourceProvenance = "synthetic",
                         series_source: str = "computed") -> str:
    """RMS line (mg over seconds) with one shaded band per anomalous run, plus a provenance caption."""
    caption = f"{PROVENANCE_LABELS[provenance]} · {SOURCE_LABELS[series_source]}"
    if series.state == "not_assessed" or not series.points:
        reason = series.reason or "no vibration data"
        return _placeholder("Vibration chart",
                            f"Vibration chart unavailable: {reason}. {caption}.",
                            f"Vibration chart unavailable — {reason}")
    rms = [(p.t_s, p.accel_rms) for p in series.points if p.accel_rms is not None]
    if not rms:
        return _placeholder("Vibration chart", f"No vibration samples. {caption}.",
                            "Vibration chart unavailable — no vibration samples")
    max_t = max(t for t, _ in rms) or 1.0
    max_rms = max(v for _, v in rms)
    plot_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    plot_h = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM

    def x_of(t: float) -> float:
        return MARGIN_LEFT + min(max(t, 0.0), max_t) / max_t * plot_w

    def y_of(v: float) -> float:
        return MARGIN_TOP + plot_h - min(max(v, 0.0), max_rms or 1.0) / (max_rms or 1.0) * plot_h

    bands = []
    for run in anomalous_runs:
        start, end = max(run.t_start_s, 0.0), min(run.t_end_s, max_t)
        if end <= start:
            continue
        bands.append(
            f'<rect x="{x_of(start):.1f}" y="{MARGIN_TOP}" width="{x_of(end) - x_of(start):.1f}"'
            f' height="{plot_h}" fill="#f59e0b" fill-opacity="0.25">'
            f"<title>Anomalous {start:g}–{end:g} s</title></rect>"
        )
    line = " ".join(f"{x_of(t):.1f},{y_of(v):.1f}" for t, v in rms)
    x_ticks = "".join(
        f'<line x1="{x_of(t):.1f}" y1="{MARGIN_TOP}" x2="{x_of(t):.1f}" y2="{MARGIN_TOP + plot_h}"'
        f' stroke="#e5e7eb"/>'
        f'<text x="{x_of(t):.1f}" y="{MARGIN_TOP + plot_h + 16}" text-anchor="middle"'
        f' font-family="sans-serif" font-size="11" fill="#374151">{t:g}</text>'
        for t in _ticks(max_t, 6)
    )
    y_ticks = "".join(
        f'<line x1="{MARGIN_LEFT}" y1="{y_of(v):.1f}" x2="{MARGIN_LEFT + plot_w}" y2="{y_of(v):.1f}"'
        f' stroke="#e5e7eb"/>'
        f'<text x="{MARGIN_LEFT - 6}" y="{y_of(v) + 4:.1f}" text-anchor="end"'
        f' font-family="sans-serif" font-size="11" fill="#374151">{v:g}</text>'
        for v in _ticks(max_rms, 5)
    )
    peak = max_rms
    desc = (f"Vibration RMS over {max_t:g} seconds, {len(rms)} points, peak {peak:g} mg, "
            f"{len(bands)} anomalous band(s). {caption}.")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}"'
        f' viewBox="0 0 {WIDTH} {HEIGHT}" role="img">'
        f"<title>Vibration chart</title><desc>{escape(desc)}</desc>"
        f"{x_ticks}{y_ticks}{''.join(bands)}"
        f'<polyline points="{line}" fill="none" stroke="#1d4ed8" stroke-width="2"/>'
        f'<text x="{MARGIN_LEFT}" y="{HEIGHT - 26}" font-family="sans-serif" font-size="12"'
        f' fill="#374151">s</text>'
        f'<text x="14" y="{MARGIN_TOP + 10}" font-family="sans-serif" font-size="12"'
        f' fill="#374151">mg</text>'
        f'<text x="{WIDTH // 2}" y="{HEIGHT - 8}" text-anchor="middle" font-family="sans-serif"'
        f' font-size="12" fill="#6b7280">{escape(caption)}</text></svg>'
    )
