"""Domain vibration-series tests: series math on synthetic rig_telemetry data."""

from app.contracts.sources import AccelData, RigTelemetryData, SoundData
from app.domain.vibration_series import MAX_POINTS, VibrationSeries, vibration_series


def make_data(seconds=2, accel_hz=100.0, vector=(0, 0, 1000), sound=None, sound_hz=10.0):
    n = int(seconds * accel_hz)
    data = {"rate_hz": accel_hz, "x_mg": tuple([vector[0]] * n), "y_mg": tuple([vector[1]] * n),
            "z_mg": tuple([vector[2]] * n)}
    kw = {}
    if sound is not None:
        kw["sound"] = SoundData(rate_hz=sound_hz, level=tuple(sound))
    return RigTelemetryData(accel=AccelData(**data), **kw)


def test_constant_vector_rms_and_sound_buckets():
    sound = [100] * 10 + [200] * 10
    series = vibration_series(make_data(sound=sound))
    assert isinstance(series, VibrationSeries)
    assert series.state == "assessed" and series.reason is None
    assert series.sample_count == 200 and series.duration_s == 2.0
    assert [(p.t_s, p.n_accel) for p in series.points] == [(0.0, 100), (1.0, 100)]
    assert [p.accel_rms for p in series.points] == [1000.0, 1000.0]
    assert [p.sound_mean for p in series.points] == [100.0, 200.0]
    assert [p.sound_max for p in series.points] == [100, 200]


def test_rms_matches_rollup_definition():
    # sqrt(mean(x^2 + y^2 + z^2)) over raw samples: (3, 4, 0) -> 5.0
    series = vibration_series(make_data(seconds=1, accel_hz=400.0, vector=(3, 4, 0)))
    assert series.state == "assessed"
    assert len(series.points) == 1 and series.points[0].accel_rms == 5.0


def test_short_data_is_not_assessed_not_clean():
    series = vibration_series(make_data(seconds=0.025, accel_hz=400.0))
    assert series.state == "not_assessed" and series.points == ()
    assert series.reason and "second" in series.reason


def test_missing_sound_stays_assessed_with_nulls():
    series = vibration_series(make_data())
    assert series.state == "assessed"
    assert all(p.sound_mean is None and p.sound_max is None for p in series.points)


def test_long_runs_bound_to_max_points_deterministically():
    series = vibration_series(make_data(seconds=700, accel_hz=50.0, vector=(0, 0, 500)))
    assert series.state == "assessed"
    assert 0 < len(series.points) <= MAX_POINTS
    assert series.points[0].t_s == 0.0
    again = vibration_series(make_data(seconds=700, accel_hz=50.0, vector=(0, 0, 500)))
    assert series == again
    assert [p.accel_rms for p in series.points] == [500.0] * len(series.points)
