"""Synthetic ffmpeg checks: explicit range bounds and exact original-frame identity."""
from pathlib import Path
import shutil

import pytest

from app.contracts.video import VideoRange
from app.pipeline.media import (FrameManifest, MediaError, MediaReason, build_manifest, extract_frames,
                                load_media_config, probe, sha256_file)
from app.pipeline.source_mapping import (MappingInput, SyntheticAreaMapper, UnavailableAreaMapper,
                                         load_source_config, source_media_config, validate_ranges)
from app.pipeline.source_media import normalize_source_range, select_range_frames
from tests import media_fixtures as fx

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")


def test_unavailable_mapping_does_not_default_to_driver_side():
    result = UnavailableAreaMapper().map(MappingInput("a" * 64, 45000))
    assert result.state == "unavailable" and result.ranges == ()
    assert result.reason == "VIDEO_MAPPING_UNAVAILABLE"


def test_synthetic_mapping_is_explicit_and_bounded():
    segment = VideoRange(part_no=2, start_ms=1000, end_ms=3000)
    result = SyntheticAreaMapper((segment,)).map(MappingInput("a" * 64, 45000))
    assert result.state == "synthetic" and result.ranges == (segment,)
    with pytest.raises(ValueError):
        SyntheticAreaMapper((segment,)).map(MappingInput("a" * 64, 2000))


@pytest.mark.parametrize("ranges", [
    [dict(part_no=1, start_ms=0, end_ms=2000), dict(part_no=2, start_ms=1000, end_ms=3000)],
    [dict(part_no=1, start_ms=0, end_ms=1000), dict(part_no=1, start_ms=1000, end_ms=2000)],
    [dict(part_no=1, start_ms=0, end_ms=4000)],
])
def test_invalid_range_mapping_is_rejected(ranges):
    with pytest.raises(ValueError):
        validate_ranges(tuple(VideoRange(**value) for value in ranges), 3000, load_source_config())


@pytest.mark.parametrize("container", ["mp4", "webm"])
def test_range_normalization_preserves_original_timestamp_and_color(tmp_path: Path, container: str):
    source = fx.make_color_steps(tmp_path / f"source.{container}", container=container).path
    media = load_media_config()
    config = source_media_config(load_source_config(), media)
    info = probe(source, config, tmp_path)
    digest = sha256_file(source)
    manifest = build_manifest(source, config, tmp_path, proxy_sha256=digest)
    work = tmp_path / "range"
    work.mkdir()
    result = normalize_source_range(source, work, media, info, manifest,
                                    VideoRange(part_no=2, start_ms=1150, end_ms=1900), digest)
    assert result.frame_timestamps_ms == (1200, 1400, 1600, 1700)
    assert result.normalized.source_sha256 == digest
    assert result.normalized.frame_count == len(result.frame_timestamps_ms)
    assert result.normalized.source.audio_stream_count == 0
    frames = extract_frames(result.normalized.path, result.normalized.manifest, [0], work, media)
    from PIL import Image
    with Image.open(frames[0].path) as image:
        red, green, blue = image.convert("RGB").getpixel((40, 40))
    assert green > red + 40 and green > blue + 40  # original 1..2s is green, not nearby red/blue


def test_variable_frame_rate_gap_is_preserved(tmp_path: Path):
    source = fx.make_gap_clip(tmp_path / "gap.mp4").path
    media = load_media_config()
    config = source_media_config(load_source_config(), media)
    info, digest = probe(source, config, tmp_path), sha256_file(source)
    manifest = build_manifest(source, config, tmp_path)
    work = tmp_path / "range"
    work.mkdir()
    result = normalize_source_range(source, work, media, info, manifest,
                                    VideoRange(part_no=4, start_ms=200, end_ms=1400), digest)
    assert result.frame_timestamps_ms == (200, 300, 400, 1000, 1100, 1200, 1300)
    assert result.normalized.manifest.frame_count == 7


def test_rotation_is_normalized_with_original_times(tmp_path: Path):
    source = fx.make_rotated_clip(tmp_path / "rotated.mp4", 90).path
    media = load_media_config()
    config = source_media_config(load_source_config(), media)
    info = probe(source, config, tmp_path)
    manifest = build_manifest(source, config, tmp_path)
    work = tmp_path / "range"
    work.mkdir()
    result = normalize_source_range(source, work, media, info, manifest,
                                    VideoRange(part_no=4, start_ms=200, end_ms=800), sha256_file(source))
    assert result.normalized.height > result.normalized.width
    assert result.frame_timestamps_ms == (200, 300, 400, 500, 600, 700)


@pytest.mark.parametrize("seconds", [30, 60, 180])
def test_source_limit_accepts_partial_and_full_walkaround(tmp_path: Path, seconds: int):
    source = fx.make_pattern_clip(tmp_path / "clip.mp4", size=(160, 90), fps=1, seconds=seconds).path
    config = source_media_config(load_source_config(), load_media_config())
    assert round(probe(source, config, tmp_path).duration_s) == seconds


def test_overlong_source_rejected_before_ranges(tmp_path: Path):
    source = fx.make_pattern_clip(tmp_path / "long.mp4", size=(160, 90), fps=1, seconds=181).path
    config = source_media_config(load_source_config(), load_media_config())
    with pytest.raises(MediaError) as error:
        probe(source, config, tmp_path)
    assert error.value.reason == MediaReason.DURATION_EXCEEDED


def test_oversize_source_is_rejected_before_decoding(tmp_path: Path):
    source = tmp_path / "oversize.mp4"
    with source.open("wb") as handle:
        handle.truncate(209715201)
    config = source_media_config(load_source_config(), load_media_config())
    with pytest.raises(MediaError) as error:
        probe(source, config, tmp_path)
    assert error.value.reason == MediaReason.RESOURCE_LIMIT


def test_original_pts_offset_is_not_silently_subtracted():
    manifest = FrameManifest("a" * 64, 160, 90, 1, 1000, (1000, 1100, 1200, 1300))
    assert select_range_frames(manifest, VideoRange(part_no=1, start_ms=1050, end_ms=1250), None) == (1, 2)
    with pytest.raises(MediaError):
        select_range_frames(manifest, VideoRange(part_no=1, start_ms=0, end_ms=500), None)
    negative = FrameManifest("a" * 64, 160, 90, 1, 1000, (-100, 0, 100))
    with pytest.raises(MediaError):
        select_range_frames(negative, VideoRange(part_no=1, start_ms=0, end_ms=500), None)


@pytest.mark.parametrize("container", ["mp4", "webm"])
def test_range_strips_audio_without_shifting_source_pts(tmp_path: Path, container: str):
    source = fx.make_pattern_clip(tmp_path / f"audio.{container}", container=container, with_audio=True).path
    media = load_media_config()
    config = source_media_config(load_source_config(), media)
    info = probe(source, config, tmp_path)
    manifest = build_manifest(source, config, tmp_path)
    work = tmp_path / "range"
    work.mkdir()
    result = normalize_source_range(source, work, media, info, manifest,
                                    VideoRange(part_no=4, start_ms=0, end_ms=1000), sha256_file(source))
    assert probe(result.normalized.path, media, work).audio_stream_count == 0
    assert result.frame_timestamps_ms[0] == int(manifest.time_fraction(0) * 1000)
