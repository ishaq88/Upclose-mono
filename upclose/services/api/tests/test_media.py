"""T05a-media acceptance: bounded probe/normalize/manifest/extraction and quality measurement.

All media is synthetic and generated at test time with ffmpeg (see media_fixtures.py). These
tests need the ffmpeg/ffprobe binaries from the API image.
"""

import hashlib
import shutil
import json
import os
import socket
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from app.pipeline import media
from app.pipeline.media import (
    FrameManifest,
    MediaConfig,
    MediaError,
    MediaReason,
    ProcessLimits,
    ProbeResult,
    Rotation,
    build_manifest,
    extract_frames,
    ensure_plain_file,
    load_media_config,
    normalize,
    parse_rotation,
    plan_frame_indices,
    probe,
    run_tool,
    scratch_dir,
    sweep_stale_scratch,
    target_size,
)
from app.pipeline.quality import (
    ClipAnalysis,
    FrameQuality,
    evaluate_quality,
    laplacian_variance,
    luma_metrics,
    measure_quality,
    normalize_and_assess,
    working_size,
)
from tests import media_fixtures as fx

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


# --------------------------------------------------------------------------- helpers


@pytest.fixture(scope="session")
def config() -> MediaConfig:
    return load_media_config()


@pytest.fixture()
def scratch(tmp_path: Path):
    with scratch_dir(tmp_path / "scratch-root") as directory:
        yield directory


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("synthetic-media")


@pytest.fixture(scope="module")
def steps_mp4(media_dir: Path) -> Path:
    return fx.make_color_steps(media_dir / "steps.mp4").path


@pytest.fixture(scope="module")
def steps_webm(media_dir: Path) -> Path:
    return fx.make_color_steps(media_dir / "steps.webm", container="webm").path


def limited(config: MediaConfig, tool: str, **updates: object) -> MediaConfig:
    return config.model_copy(update={"limits": config.limits.model_copy(update={tool: getattr(config.limits, tool).model_copy(update=updates)})})


def rgb_at(path: Path, x: int, y: int) -> tuple[int, int, int]:
    with Image.open(path) as image:
        return image.convert("RGB").getpixel((x, y))  # type: ignore[return-value]


def center_color_name(path: Path) -> str:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        pixel = rgb.getpixel((rgb.width // 2, rgb.height // 2))
    name, distance = min(
        ((label, sum((a - b) ** 2 for a, b in zip(pixel, value)) ** 0.5) for label, value in fx.COLOR_STEPS),
        key=lambda item: item[1],
    )
    assert distance < 120, f"pixel {pixel} is not close to any palette color"
    return name


def is_red(pixel: tuple[int, int, int]) -> bool:
    return pixel[0] > 180 and pixel[1] < 90 and pixel[2] < 90


def is_blue(pixel: tuple[int, int, int]) -> bool:
    return pixel[2] > 180 and pixel[0] < 90 and pixel[1] < 90


# --------------------------------------------------------------------------- config


def test_config_loads_with_stable_content_hash(config: MediaConfig) -> None:
    assert config.version == "media-v1-provisional"
    assert load_media_config().content_hash == config.content_hash
    assert len(config.content_hash) == 64
    assert config.model_copy(update={"max_duration_s": 59}).content_hash != config.content_hash
    assert config.max_duration_s == 60 and config.max_source_long_edge == 3840 and config.proxy_long_edge == 1280
    assert config.quality.provisional is True


def test_frame_plan_covers_each_part_and_tires_use_eight_stills(config: MediaConfig) -> None:
    assert [plan.part_number for plan in config.frame_plans] == list(range(1, 9))
    tires = config.plan_for(4)
    assert (tires.mode, tires.count, tires.sample_fps) == ("stills", 8, None)
    assert config.plan_for(5).mode == "stills" and config.plan_for(8).mode == "stills"
    assert config.plan_for(1).sample_fps == 6 and config.plan_for(7).sample_fps == 4
    assert config.plan_for(1).proxy_fps(30.0) == 6 and config.plan_for(1).proxy_fps(4.0) == 4
    assert tires.proxy_fps(30.0) is None


def test_config_rejects_incomplete_frame_plans(config: MediaConfig) -> None:
    payload = json.loads(config.model_dump_json())
    payload["frame_plans"] = payload["frame_plans"][:-1]
    with pytest.raises(ValueError, match="frame_plans"):
        MediaConfig.model_validate(payload)


# --------------------------------------------------------------------------- normalize


def test_mp4_normalizes_to_canonical_proxy(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_pattern_clip(media_dir / "audio.mp4", with_audio=True)
    result = normalize(clip.path, scratch, config)
    assert clip.provenance == "synthetic"
    assert (result.width, result.height) == (320, 180) and result.frame_count == 20
    produced = probe(result.path, config, scratch)
    assert (produced.container, produced.codec, produced.pix_fmt) == ("mov", "h264", "yuv420p"), "mov,mp4 demuxer family"
    assert produced.audio_stream_count == 0 and result.source.audio_stream_count == 1
    assert produced.rotation == Rotation(0, "none")
    assert result.source_sha256 == hashlib.sha256(clip.path.read_bytes()).hexdigest()
    assert result.output_sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
    data = result.path.read_bytes()
    assert 0 <= data.find(b"moov") < data.find(b"mdat"), "+faststart expected: moov before mdat"
    assert result.manifest.proxy_sha256 == result.output_sha256


def test_webm_normalizes(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_pattern_clip(media_dir / "pattern.webm", container="webm")
    result = normalize(clip.path, scratch, config)
    assert result.source.container == "matroska" and result.source.codec == "vp8"
    assert result.frame_count == 20 and result.path.suffix == ".mp4"
    assert probe(result.path, config, scratch).codec == "h264"


def test_webm_without_duration_header_uses_packet_scan(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_headerless_webm(media_dir / "nodur.webm", seconds=2.0)
    info = probe(clip.path, config, scratch)
    assert info.duration_s == pytest.approx(2.0, abs=0.15)
    assert normalize(clip.path, scratch, config, probe_result=info).frame_count == 20


def test_normalize_is_reproducible(config: MediaConfig, tmp_path: Path, media_dir: Path) -> None:
    clip = fx.make_pattern_clip(media_dir / "repro.mp4", seconds=1.0)
    hashes = []
    for name in ("a", "b"):
        with scratch_dir(tmp_path / name) as directory:
            hashes.append(normalize(clip.path, directory, config).output_sha256)
    assert hashes[0] == hashes[1]


@pytest.mark.parametrize(
    ("source", "expected"),
    [((1920, 1080), (1280, 720)), ((1080, 1920), (720, 1280)), ((1922, 1082), (1280, 720)), ((320, 180), (320, 180))],
)
def test_proxy_size_even_bounded_and_never_upscaled(
    config: MediaConfig, scratch: Path, media_dir: Path, source: tuple[int, int], expected: tuple[int, int]
) -> None:
    clip = fx.make_pattern_clip(media_dir / f"size-{source[0]}x{source[1]}.mp4", size=source, fps=2, seconds=1.0)
    result = normalize(clip.path, scratch, config)
    assert (result.width, result.height) == expected
    assert result.width % 2 == 0 and result.height % 2 == 0 and max(result.width, result.height) <= 1280


def test_target_size_rules() -> None:
    assert target_size(3840, 2160, 1280) == (1280, 720)
    assert target_size(2160, 3840, 1280) == (720, 1280)
    assert target_size(100, 50, 1280) == (100, 50)
    assert target_size(1281, 721, 1280) == (1280, 720)


@pytest.mark.parametrize(("clockwise", "red_edge"), [(0, "left"), (90, "top"), (180, "right"), (270, "bottom")])
def test_rotation_is_applied_and_leaves_no_metadata(
    config: MediaConfig, scratch: Path, media_dir: Path, clockwise: int, red_edge: str
) -> None:
    clip = fx.make_rotated_clip(media_dir / f"rot{clockwise}.mp4", clockwise) if clockwise else fx.make_band_clip(media_dir / "rot0.mp4")
    info = probe(clip.path, config, scratch)
    assert info.rotation.clockwise_degrees == clockwise
    portrait = clockwise in (90, 270)
    assert (info.display_width, info.display_height) == ((90, 160) if portrait else (160, 90))
    result = normalize(clip.path, scratch, config, probe_result=info)
    assert (result.width, result.height) == ((90, 160) if portrait else (160, 90)), "portrait stays portrait"
    assert probe(result.path, config, scratch).rotation == Rotation(0, "none")
    frame = extract_frames(result.path, result.manifest, [0], scratch, config, image_format="png")[0].path
    w, h = result.width, result.height
    edges = {"left": (4, h // 2), "right": (w - 5, h // 2), "top": (w // 2, 4), "bottom": (w // 2, h - 5)}
    centre = (w // 2, h // 2)
    for name, point in edges.items():
        assert is_red(rgb_at(frame, *point)) == (name == red_edge), f"{name} edge for {clockwise} degrees"
    assert is_blue(rgb_at(frame, *centre))


def test_legacy_rotate_tag_is_applied_by_explicit_filter(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_band_clip(media_dir / "legacy.mp4")
    info = probe(clip.path, config, scratch)
    legacy = ProbeResult(**{**_asdict(info), "rotation": Rotation(90, "legacy_tag"), "display_width": 90, "display_height": 160})
    result = normalize(clip.path, scratch, config, probe_result=legacy)
    assert (result.width, result.height) == (90, 160)
    frame = extract_frames(result.path, result.manifest, [0], scratch, config, image_format="png")[0].path
    assert is_red(rgb_at(frame, 45, 4)), "left band rotated 90 clockwise lands on top"


def _asdict(info: ProbeResult) -> dict[str, object]:
    return {name: getattr(info, name) for name in info.__slots__}


def test_parse_rotation_sign_conventions_and_precedence() -> None:
    assert parse_rotation({}) == Rotation(0, "none")
    assert parse_rotation({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}) == Rotation(90, "display_matrix")
    assert parse_rotation({"side_data_list": [{"rotation": 90.0}]}) == Rotation(270, "display_matrix")
    assert parse_rotation({"side_data_list": [{"rotation": 180}]}).clockwise_degrees == 180
    assert parse_rotation({"tags": {"rotate": "90"}}) == Rotation(90, "legacy_tag")
    assert parse_rotation({"tags": {"rotate": "270"}}).clockwise_degrees == 270
    both = {"side_data_list": [{"rotation": -90}], "tags": {"rotate": "180"}}
    assert parse_rotation(both) == Rotation(90, "display_matrix"), "display matrix wins over the legacy tag"
    for bad in ({"tags": {"rotate": "45"}}, {"tags": {"rotate": "x"}}, {"side_data_list": [{"rotation": 33.3}]}):
        with pytest.raises(MediaError) as caught:
            parse_rotation(bad)
        assert caught.value.reason is MediaReason.UNSUPPORTED_ROTATION


# --------------------------------------------------------------------------- manifest and extraction


@pytest.mark.parametrize("source", ["steps_mp4", "steps_webm"])
def test_extracted_frames_match_manifest_timestamps(
    request: pytest.FixtureRequest, config: MediaConfig, scratch: Path, source: str
) -> None:
    clip: Path = request.getfixturevalue(source)
    result = normalize(clip, scratch, config)
    manifest = result.manifest
    assert manifest.frame_count == 40
    assert [manifest.time_seconds(i) for i in (0, 9, 10, 39)] == pytest.approx([0.0, 0.9, 1.0, 3.9], abs=1e-3)
    wanted = [0, 9, 10, 19, 20, 29, 30, 39]
    frames = extract_frames(result.path, manifest, wanted, scratch, config, image_format="png")
    assert [frame.frame_index for frame in frames] == wanted
    for frame in frames:
        expected = fx.COLOR_STEPS[int(round(frame.time_s, 3))][0]
        assert center_color_name(frame.path) == expected, f"frame {frame.frame_index} at {frame.time_s}s"
        assert frame.pts == manifest.pts[frame.frame_index]
        assert frame.sha256 == hashlib.sha256(frame.path.read_bytes()).hexdigest()


def test_manifest_follows_real_timestamps_not_constant_fps(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_gap_clip(media_dir / "gap.mp4")
    result = normalize(clip.path, scratch, config)
    manifest = result.manifest
    times = [manifest.time_seconds(i) for i in range(manifest.frame_count)]
    assert manifest.frame_count == 10
    assert times[4] == pytest.approx(0.4, abs=1e-3) and times[5] == pytest.approx(1.0, abs=1e-3)
    deltas = [round(b - a, 3) for a, b in zip(times, times[1:])]
    assert len(set(deltas)) > 1, "variable frame rate must not be flattened"
    before, after = extract_frames(result.path, manifest, [4, 5], scratch, config, image_format="png")
    assert is_red(rgb_at(before.path, 80, 45)) and is_blue(rgb_at(after.path, 80, 45))
    assert after.time_s - before.time_s == pytest.approx(0.6, abs=1e-3)


def test_manifest_serialization_is_deterministic_and_validated() -> None:
    manifest = FrameManifest("ab" * 32, 160, 90, 1, 1000, (0, 100, 250))
    again = FrameManifest.from_json(manifest.to_canonical_json())
    assert again == manifest and again.content_hash == manifest.content_hash
    assert manifest.to_canonical_json() == json.dumps(json.loads(manifest.to_canonical_json()), sort_keys=True, separators=(",", ":"))
    assert FrameManifest("ab" * 32, 160, 90, 1, 1000, (0, 100, 251)).content_hash != manifest.content_hash
    assert FrameManifest("cd" * 32, 160, 90, 1, 1000, (0, 100, 250)).content_hash != manifest.content_hash
    assert manifest.time_fraction(2).numerator == 1 and manifest.time_seconds(2) == 0.25
    for pts in ((), (0, 0, 1), (5, 3)):
        with pytest.raises(ValueError):
            FrameManifest("ab" * 32, 1, 1, 1, 1000, pts)
    with pytest.raises(ValueError):
        FrameManifest.from_json(manifest.to_canonical_json().replace(media.MANIFEST_VERSION, "other"))


def test_manifest_hash_is_reproducible_across_rebuilds(config: MediaConfig, scratch: Path, steps_mp4: Path) -> None:
    result = normalize(steps_mp4, scratch, config)
    rebuilt = build_manifest(result.path, config, scratch)
    assert rebuilt.content_hash == result.manifest.content_hash


def test_extract_jpeg_scaling_and_input_validation(config: MediaConfig, scratch: Path, steps_mp4: Path) -> None:
    result = normalize(steps_mp4, scratch, config)
    frames = extract_frames(result.path, result.manifest, [30, 5, 5], scratch, config, image_format="jpeg", scale_to=(80, 44))
    assert [frame.frame_index for frame in frames] == [5, 30], "sorted and de-duplicated"
    assert all(frame.path.suffix == ".jpg" and (frame.width, frame.height) == (80, 44) for frame in frames)
    assert center_color_name(frames[0].path) == "red" and center_color_name(frames[1].path) == "yellow"
    for bad in ([], [-1], [40]):
        with pytest.raises(ValueError):
            extract_frames(result.path, result.manifest, bad, scratch, config)
    small = config.model_copy(update={"max_extract_frames": 2})
    with pytest.raises(MediaError) as caught:
        extract_frames(result.path, result.manifest, [0, 1, 2], scratch, small)
    assert caught.value.reason is MediaReason.RESOURCE_LIMIT


def test_plan_frame_indices(config: MediaConfig, scratch: Path, steps_mp4: Path) -> None:
    manifest = normalize(steps_mp4, scratch, config).manifest  # 40 frames over 4 s at 10 fps
    tires = plan_frame_indices(manifest, config.plan_for(4))
    assert len(tires) == 8 and list(tires) == sorted(set(tires))
    assert tires == (2, 7, 12, 17, 22, 27, 32, 37)
    video = plan_frame_indices(manifest, config.plan_for(7))  # 4 fps over 4 s
    assert len(video) == 16 and video[0] == 0
    short = FrameManifest("ab" * 32, 16, 16, 1, 10, (0, 1, 2))
    assert plan_frame_indices(short, config.plan_for(4)) == (0, 1, 2), "fewer frames than requested yields fewer stills"


def test_video_part_proxy_is_resampled_to_plan_fps(config: MediaConfig, scratch: Path, steps_mp4: Path) -> None:
    result = normalize(steps_mp4, scratch, config, target_fps=config.plan_for(1).proxy_fps(10.0))
    assert result.frame_count == 24 and result.manifest.time_seconds(6) == pytest.approx(1.0, abs=1e-3)


# --------------------------------------------------------------------------- probe rejections


def reason_of(callable_, *args, **kwargs) -> MediaReason:
    with pytest.raises(MediaError) as caught:
        callable_(*args, **kwargs)
    assert "/tmp" not in caught.value.detail and "pytest-of" not in caught.value.detail, "detail must not leak paths"
    return caught.value.reason


def test_corrupt_inputs_fail_with_reason(config: MediaConfig, scratch: Path, media_dir: Path, steps_mp4: Path) -> None:
    garbage = fx.make_garbage(media_dir / "garbage.mp4").path
    truncated = fx.make_truncated(steps_mp4, media_dir / "truncated.mp4").path
    empty = media_dir / "empty.mp4"
    empty.write_bytes(b"")
    for path in (garbage, truncated, empty):
        assert reason_of(probe, path, config, scratch) is MediaReason.CORRUPT, path.name
        assert reason_of(normalize, path, scratch, config) is MediaReason.CORRUPT
    assert not (scratch / "proxy.mp4").exists()


def test_structural_rejections(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    assert reason_of(probe, fx.make_audio_only(media_dir / "audio-only.mp4").path, config, scratch) is MediaReason.NO_VIDEO
    assert reason_of(probe, fx.make_two_video_streams(media_dir / "two.mp4").path, config, scratch) is MediaReason.MULTIPLE_VIDEO_STREAMS
    assert reason_of(probe, fx.make_codec_clip(media_dir / "mpeg4.mp4").path, config, scratch) is MediaReason.UNSUPPORTED_CODEC
    assert reason_of(probe, fx.make_mpegts(media_dir / "clip.ts").path, config, scratch) is MediaReason.UNSUPPORTED_CONTAINER


def test_over_duration_fails_before_decode(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_long_tiny_clip(media_dir / "long.mp4", seconds=62)
    assert reason_of(probe, clip.path, config, scratch) is MediaReason.DURATION_EXCEEDED
    assert reason_of(normalize, clip.path, scratch, config) is MediaReason.DURATION_EXCEEDED
    assert not (scratch / "proxy.mp4").exists(), "no transcode was attempted"
    ok = fx.make_pattern_clip(media_dir / "two-sec.mp4", seconds=2.0)
    assert reason_of(probe, ok.path, config.model_copy(update={"max_duration_s": 1.0}), scratch) is MediaReason.DURATION_EXCEEDED


def test_lying_duration_is_caught_after_transcode(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_pattern_clip(media_dir / "liar.mp4", seconds=3.0)
    info = probe(clip.path, config, scratch)
    understated = ProbeResult(**{**_asdict(info), "duration_s": 0.5})
    tight = config.model_copy(update={"max_duration_s": 1.0, "duration_tolerance_s": 0.0})
    assert reason_of(normalize, clip.path, scratch, tight, probe_result=understated) is MediaReason.DURATION_EXCEEDED
    assert not (scratch / "proxy.mp4").exists(), "over-long output is discarded"


def test_over_dimension_fails_with_reason(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    wide = fx.make_solid(media_dir / "wide.mp4", "gray", size=(4000, 300), seconds=0.4)
    tall = fx.make_solid(media_dir / "tall.mp4", "gray", size=(300, 4000), seconds=0.4)
    assert reason_of(probe, wide.path, config, scratch) is MediaReason.DIMENSIONS_EXCEEDED
    assert reason_of(probe, tall.path, config, scratch) is MediaReason.DIMENSIONS_EXCEEDED
    assert reason_of(normalize, wide.path, scratch, config) is MediaReason.DIMENSIONS_EXCEEDED
    clip = fx.make_pattern_clip(media_dir / "small-limit.mp4", size=(320, 180), fps=2, seconds=1.0)
    tight = config.model_copy(update={"max_source_short_edge": 100})
    assert reason_of(probe, clip.path, tight, scratch) is MediaReason.DIMENSIONS_EXCEEDED


def test_oversized_source_file_rejected(config: MediaConfig, scratch: Path, steps_mp4: Path) -> None:
    assert reason_of(probe, steps_mp4, config.model_copy(update={"max_source_bytes": 100}), scratch) is MediaReason.RESOURCE_LIMIT


# --------------------------------------------------------------------------- process limits


PYTHON = [sys.executable, "-I", "-c"]


def limits(**updates: object) -> ProcessLimits:
    base = {"wall_time_s": 10.0, "cpu_seconds": 10, "address_space_bytes": 4 * 1024**3, "max_file_bytes": 1024**2, "threads": 1}
    return ProcessLimits(**{**base, **updates})


def test_run_tool_enforces_wall_time(scratch: Path) -> None:
    started = time.monotonic()
    run = run_tool([*PYTHON, "import time; time.sleep(30)"], limits=limits(wall_time_s=0.5), cwd=scratch)
    assert run.timed_out and time.monotonic() - started < 25
    assert media.classify_failure(run, MediaReason.CORRUPT).reason is MediaReason.TIMEOUT


def test_run_tool_enforces_cpu_limit(scratch: Path) -> None:
    run = run_tool([*PYTHON, "while True: pass"], limits=limits(cpu_seconds=1, wall_time_s=120.0), cwd=scratch)
    assert not run.timed_out and run.returncode < 0
    assert media.classify_failure(run, MediaReason.CORRUPT).reason is MediaReason.RESOURCE_LIMIT


def test_run_tool_enforces_file_size_and_address_space(scratch: Path) -> None:
    target = scratch / "big.bin"
    run = run_tool([*PYTHON, f"open({str(target)!r}, 'wb').write(b'x' * 5_000_000)"], limits=limits(max_file_bytes=100_000), cwd=scratch)
    assert run.returncode != 0 and target.stat().st_size <= 100_000
    assert media.classify_failure(run, MediaReason.CORRUPT).reason is MediaReason.RESOURCE_LIMIT
    run = run_tool([*PYTHON, "bytearray(512 * 1024 * 1024)"], limits=limits(address_space_bytes=256 * 1024**2), cwd=scratch)
    assert run.returncode != 0
    assert media.classify_failure(run, MediaReason.CORRUPT).reason is MediaReason.RESOURCE_LIMIT


def test_run_tool_uses_scratch_cwd_and_minimal_env(scratch: Path) -> None:
    code = "import os, json; print(json.dumps({'cwd': os.getcwd(), 'env': sorted(os.environ)}))"
    run = run_tool([*PYTHON, code], limits=limits(), cwd=scratch)
    data = json.loads(run.stdout)
    assert Path(data["cwd"]) == scratch
    assert {"PATH", "HOME", "TMPDIR", "LANG"} >= set(data["env"]) - {"LC_CTYPE"}
    assert "AWS_SECRET_ACCESS_KEY" not in data["env"]


def test_run_tool_rejects_malformed_args(scratch: Path) -> None:
    for bad in ([], ["echo", "a\x00b"]):
        with pytest.raises(ValueError):
            run_tool(bad, limits=limits(), cwd=scratch)


def heavy_clip(media_dir: Path) -> Path:
    path = media_dir / "heavy-4k.mp4"
    if not path.exists():
        fx.make_pattern_clip(path, size=(3840, 2160), fps=10, seconds=5.0)
    return path


def test_excessive_cpu_fails_with_resource_reason(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = heavy_clip(media_dir)
    assert probe(clip, config, scratch).display_width == 3840
    assert reason_of(normalize, clip, scratch, limited(config, "transcode", cpu_seconds=1)) is MediaReason.RESOURCE_LIMIT
    assert not (scratch / "proxy.mp4").exists()


def test_excessive_memory_fails_with_resource_reason(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = heavy_clip(media_dir)
    assert reason_of(normalize, clip, scratch, limited(config, "transcode", address_space_bytes=200 * 1024**2)) is MediaReason.RESOURCE_LIMIT


def test_excessive_output_size_and_wall_time_fail(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_pattern_clip(media_dir / "limits.mp4", size=(640, 360), fps=10, seconds=2.0)
    assert reason_of(normalize, clip.path, scratch, limited(config, "transcode", max_file_bytes=2000)) is MediaReason.RESOURCE_LIMIT
    heavy = heavy_clip(media_dir)
    assert reason_of(normalize, heavy, scratch, limited(config, "transcode", wall_time_s=0.01)) is MediaReason.TIMEOUT
    assert not (scratch / "proxy.mp4").exists()


# --------------------------------------------------------------------------- no network / local files only


@pytest.fixture()
def no_subprocess(monkeypatch: pytest.MonkeyPatch):
    def refuse(*args: object, **kwargs: object):
        raise AssertionError("a subprocess was started for an input that must be rejected first")

    monkeypatch.setattr(media.subprocess, "Popen", refuse)


@pytest.mark.parametrize(
    "bad",
    [
        "http://127.0.0.1:9/video.mp4",
        "https://example.invalid/video.mp4",
        "rtmp://example.invalid/live",
        "file:///etc/hostname",
        "concat:a.mp4|b.mp4",
        "lavfi:testsrc",
        "",
        "-i",
        "x\x00y",
    ],
)
def test_non_file_input_is_rejected_without_starting_a_process(
    config: MediaConfig, scratch: Path, no_subprocess: None, bad: str
) -> None:
    assert reason_of(probe, bad, config, scratch) is MediaReason.INVALID_INPUT
    assert reason_of(normalize, bad, scratch, config) is MediaReason.INVALID_INPUT


def test_directory_symlink_fifo_and_missing_are_rejected(config: MediaConfig, scratch: Path, steps_mp4: Path, no_subprocess: None) -> None:
    link = scratch / "link.mp4"
    link.symlink_to(steps_mp4)
    fifo = scratch / "fifo.mp4"
    os.mkfifo(fifo)
    for bad in (scratch, link, fifo, scratch / "missing.mp4", 42):
        assert reason_of(probe, bad, config, scratch) is MediaReason.INVALID_INPUT, bad
    assert ensure_plain_file(steps_mp4) == steps_mp4


def test_playlists_referencing_urls_never_open_a_connection(config: MediaConfig, scratch: Path) -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    server.settimeout(0.5)
    url = f"http://127.0.0.1:{server.getsockname()[1]}/segment.mp4"
    playlists = {
        "concat.txt": f"ffconcat version 1.0\nfile '{url}'\n",
        "stream.m3u8": f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{url}\n#EXT-X-ENDLIST\n",
        "stream.mp4": f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n{url}\n#EXT-X-ENDLIST\n",
    }
    try:
        for name, body in playlists.items():
            path = scratch / name
            path.write_text(body)
            reason = reason_of(probe, path, config, scratch)
            assert reason in {MediaReason.UNSUPPORTED_CONTAINER, MediaReason.CORRUPT}, (name, reason)
            assert reason_of(normalize, path, scratch, config) in {MediaReason.UNSUPPORTED_CONTAINER, MediaReason.CORRUPT}
        with pytest.raises(socket.timeout):
            server.accept()
    finally:
        server.close()


# --------------------------------------------------------------------------- scratch lifecycle


def test_scratch_removed_on_success_and_failure(tmp_path: Path, config: MediaConfig, steps_mp4: Path, media_dir: Path) -> None:
    root = tmp_path / "root"
    with scratch_dir(root) as directory:
        result = normalize(steps_mp4, directory, config)
        assert result.path.exists()
    assert list(root.iterdir()) == []
    garbage = fx.make_garbage(media_dir / "scratch-garbage.mp4").path
    with pytest.raises(MediaError):
        with scratch_dir(root) as directory:
            (directory / "leftover.tmp").write_bytes(b"x")
            normalize(garbage, directory, config)
    assert list(root.iterdir()) == []


def test_scratch_removed_after_resource_failure_and_unexpected_error(tmp_path: Path, config: MediaConfig, media_dir: Path) -> None:
    root = tmp_path / "root"
    clip = fx.make_pattern_clip(media_dir / "fail-scratch.mp4", size=(640, 360), seconds=2.0)
    with pytest.raises(MediaError):
        with scratch_dir(root) as directory:
            normalize(clip.path, directory, limited(config, "transcode", max_file_bytes=2000))
    with pytest.raises(RuntimeError):
        with scratch_dir(root):
            raise RuntimeError("handler bug")
    assert list(root.iterdir()) == []


def test_sweep_stale_scratch(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with scratch_dir(root) as live:
        stale = root / f"{media.SCRATCH_PREFIX}deadbeef"
        stale.mkdir()
        (stale / media.SCRATCH_MARKER).touch()
        (stale / "proxy.mp4").write_bytes(b"x")
        unmarked = root / f"{media.SCRATCH_PREFIX}nomarker"
        unmarked.mkdir()
        other = root / "someone-elses-dir"
        other.mkdir()
        (other / media.SCRATCH_MARKER).touch()
        target = tmp_path / "outside"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        (root / f"{media.SCRATCH_PREFIX}link").symlink_to(target, target_is_directory=True)
        now = time.time() + 7200
        assert sweep_stale_scratch(root, timedelta(hours=1), now=time.time()) == ()
        removed = sweep_stale_scratch(root, timedelta(hours=1), now=now)
        assert stale in removed and live in removed, "everything marked and older than the cutoff is removed"
    assert not stale.exists() and unmarked.exists() and other.exists() and (target / "keep.txt").exists()
    assert sweep_stale_scratch(tmp_path / "absent", timedelta(hours=1)) == ()


# --------------------------------------------------------------------------- quality


def test_sharp_clip_is_usable_and_blurry_clip_is_warned(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    sharp = fx.make_pattern_clip(media_dir / "sharp.mp4", size=(640, 360), seconds=2.0)
    blurry = fx.make_pattern_clip(media_dir / "blurry.mp4", size=(640, 360), seconds=2.0, video_filter="boxblur=luma_radius=18:luma_power=3")
    sharp_result = normalize(sharp.path, scratch, config, output_name="sharp-proxy.mp4")
    blur_result = normalize(blurry.path, scratch, config, output_name="blur-proxy.mp4")
    good = measure_quality(sharp_result.path, sharp_result.manifest, scratch, config)
    bad = measure_quality(blur_result.path, blur_result.manifest, scratch, config)
    assert good.usable_for_scoring is True
    assert not any(w.code.startswith("blur") for w in good.warnings)
    assert good.median_blur_variance is not None and good.median_blur_variance >= config.quality.blur_warn_below
    assert bad.usable_for_scoring is False, "blurry footage must never be a clean pass"
    codes = {w.code for w in bad.warnings}
    assert codes & {"blur_low", "blur_strong"} and any(w.blocks_scoring for w in bad.warnings)
    assert bad.median_blur_variance is not None and bad.median_blur_variance < config.quality.blur_unusable_below
    assert bad.thresholds_provisional is True and bad.config_hash == config.content_hash
    assert len(bad.frames) == config.quality.sample_count
    assert json.dumps(bad.to_json_dict())
    assert (bad.working_width, bad.working_height) == (640, 360)


def test_dark_clip_is_not_usable_and_short_clip_only_warns(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    dark = fx.make_solid(media_dir / "dark.mp4", "black")
    result = normalize(dark.path, scratch, config)
    report = measure_quality(result.path, result.manifest, scratch, config, source_display_size=(320, 180))
    codes = {w.code for w in report.warnings}
    assert {"exposure_dark", "resolution_low", "duration_short"} <= codes
    assert report.usable_for_scoring is False
    short_only = evaluate_quality(
        (FrameQuality(0, 0.0, 500.0, 120.0, 0.0, 0.0),), 3.0, (1920, 1080), config.quality
    )
    assert [w.code for w in short_only] == ["duration_short"] and not short_only[0].blocks_scoring


def test_measurement_failure_is_reported_not_raised(config: MediaConfig, scratch: Path) -> None:
    manifest = FrameManifest("ab" * 32, 320, 180, 1, 10, tuple(range(10)))
    report = measure_quality(scratch / "missing.mp4", manifest, scratch, config)
    assert report.usable_for_scoring is False
    assert [w.code for w in report.warnings] == ["measurement_failed"]


def test_threshold_bands(config: MediaConfig) -> None:
    quality = config.quality

    def verdict(blur: float, luma: float = 120.0, bright: float = 0.0, dark: float = 0.0, duration: float = 20.0):
        frames = tuple(FrameQuality(i, float(i), blur, luma, bright, dark) for i in range(5))
        return evaluate_quality(frames, duration, (1920, 1080), quality)

    assert verdict(100.0) == () and verdict(250.0) == ()
    mild = verdict(70.0)
    # Below the PRD blur pass gate (100) the zone is not scored: "failed blur" is annotated in coverage.
    assert [(w.code, w.severity, w.blocks_scoring) for w in mild] == [("blur_low", "warn", True)]
    strong = verdict(39.9)
    assert [(w.code, w.severity, w.blocks_scoring) for w in strong] == [("blur_strong", "strong", True)]
    assert [w.code for w in verdict(200.0, luma=39.0)] == ["exposure_dark"]
    assert [w.code for w in verdict(200.0, luma=216.0)] == ["exposure_bright"]
    assert verdict(200.0, luma=10.0)[0].blocks_scoring and verdict(200.0, luma=250.0)[0].blocks_scoring
    assert [w.code for w in verdict(200.0, bright=5.0)] == ["clipping_bright"]
    assert [w.code for w in verdict(200.0, dark=10.0)] == ["clipping_dark"]
    assert evaluate_quality((), 20.0, None, quality)[0].code == "measurement_failed"


def test_portrait_720p_source_is_not_flagged_low_resolution(config: MediaConfig) -> None:
    frame = (FrameQuality(0, 0.0, 200.0, 120.0, 0.0, 0.0),)
    assert evaluate_quality(frame, 20.0, (720, 1280), config.quality) == ()
    assert [w.code for w in evaluate_quality(frame, 20.0, (540, 960), config.quality)] == ["resolution_low"]


def test_working_size_is_720p_box_by_orientation(config: MediaConfig) -> None:
    quality = config.quality
    assert working_size(1280, 720, quality) == (1280, 720)
    assert working_size(720, 1280, quality) == (720, 1280)
    assert working_size(1280, 960, quality) == (960, 720)
    assert working_size(320, 180, quality) == (320, 180)


def test_laplacian_variance_matches_reference_and_ranks_blur() -> None:
    import random

    rng = random.Random(3)
    width, height = 12, 9
    values = [rng.randrange(256) for _ in range(width * height)]
    image = Image.new("L", (width, height))
    image.putdata(values)
    grid = [values[row * width : (row + 1) * width] for row in range(height)]
    laplacian = [
        grid[y - 1][x] + grid[y + 1][x] + grid[y][x - 1] + grid[y][x + 1] - 4 * grid[y][x]
        for y in range(1, height - 1)
        for x in range(1, width - 1)
    ]
    assert laplacian_variance(image) == pytest.approx(statistics.pvariance(laplacian), rel=1e-3)
    assert laplacian_variance(Image.new("L", (32, 32), 90)) == 0.0
    assert laplacian_variance(Image.new("L", (2, 2))) == 0.0
    mean, bright, dark = luma_metrics(Image.new("L", (4, 4), 255), 250, 10)
    assert (mean, bright, dark) == (255.0, 100.0, 0.0)
    assert luma_metrics(Image.new("L", (4, 4), 0), 250, 10) == (0.0, 0.0, 100.0)


# --------------------------------------------------------------------------- end to end


def test_normalize_and_assess_for_a_still_part(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_pattern_clip(media_dir / "tires.mp4", size=(640, 360), fps=10, seconds=3.0)
    analysis = normalize_and_assess(clip.path, scratch, config, part_number=4)
    assert isinstance(analysis, ClipAnalysis) and analysis.plan.count == 8
    assert len(analysis.planned_frame_indices) == 8
    assert analysis.normalized.frame_count == 30, "stills parts keep source timing; stills are cut afterwards"
    frames = extract_frames(analysis.normalized.path, analysis.normalized.manifest, analysis.planned_frame_indices, scratch, config)
    assert len(frames) == 8 and [f.frame_index for f in frames] == sorted(analysis.planned_frame_indices)
    assert analysis.quality.usable_for_scoring is True


def test_normalize_and_assess_resamples_video_parts(config: MediaConfig, scratch: Path, media_dir: Path) -> None:
    clip = fx.make_pattern_clip(media_dir / "side.mp4", size=(640, 360), fps=10, seconds=3.0)
    analysis = normalize_and_assess(clip.path, scratch, config, part_number=7)  # plan: 4 fps
    assert analysis.normalized.frame_count == 12
    assert analysis.planned_frame_indices == tuple(range(12))
    with pytest.raises(MediaError):
        normalize_and_assess(fx.make_garbage(media_dir / "bad.mp4").path, scratch, config, part_number=1)
