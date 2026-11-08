"""Vibration chart tests: determinism, bands, labels, escaping."""

from app.contracts.sources import VerdictRun
from app.domain.vibration_series import VibrationSeries, vibration_series
from app.report.vibration_chart import render_vibration_svg
from tests.test_vibration_series import make_data


def assessed():
    return vibration_series(make_data(sound=[100] * 10 + [300] * 10))


def test_render_is_deterministic():
    series = assessed()
    run = VerdictRun(t_start_s=0.5, t_end_s=1.5, peak_score=9.0, top_features=())
    first = render_vibration_svg(series, (run,), "synthetic", "tiger")
    assert first == render_vibration_svg(assessed(), (run,), "synthetic", "tiger")
    assert "<title>Vibration chart</title>" in first and "<desc>" in first
    assert "<script" not in first and "javascript" not in first


def test_bands_clipped_to_series_range():
    series = assessed()  # 2 seconds
    svg = render_vibration_svg(series, (VerdictRun(t_start_s=0.5, t_end_s=1.5, peak_score=1.0,
                                                  top_features=()),), "live", "computed")
    assert svg.count("<rect") == 1
    svg = render_vibration_svg(series, (VerdictRun(t_start_s=9.0, t_end_s=10.0, peak_score=1.0,
                                                  top_features=()),), "live", "computed")
    assert "<rect" not in svg


def test_caption_labels_and_axes():
    svg = render_vibration_svg(assessed(), (), "synthetic", "tiger")
    assert "Simulated bench data · Tiger Data" in svg
    svg = render_vibration_svg(assessed(), (), "cached", "computed")
    assert "Replayed recording · computed from the frozen samples" in svg
    assert ">s</text>" in svg and ">mg</text>" in svg
    assert "<polyline" in svg


def test_not_assessed_renders_reason_escaped_not_a_line():
    series = VibrationSeries(state="not_assessed", reason='too short <img src="x"> & "odd"',
                             sample_count=3, duration_s=0.1, points=())
    svg = render_vibration_svg(series, (), "synthetic", "computed")
    assert "<polyline" not in svg
    assert "<img" not in svg and "&lt;img" in svg and "&amp;" in svg
    assert "<title>Vibration chart</title>" in svg
