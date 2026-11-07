"""Synthetic local media only; real ffmpeg checks mask isolation and alignment."""
import subprocess

from PIL import Image
import pytest

from app.adapters.meta.types import FrameRef, MaskRaster, MediaRef, SegmentObservation
from app.contracts import BoundingBox
from app.pipeline.media import MediaError, build_manifest, extract_frames, load_media_config
from app.pipeline.vehicle_media import BACKGROUND, mask_vehicle_media


def reference(kind="image", count=1):
    return MediaRef(media_key="source/test", kind=kind, stored_width=32, stored_height=24,
                    frames=tuple(FrameRef(frame_index=i, timestamp_ms=i * 200, frame_key=f"frames/{i}") for i in range(count)))


def observation(frame, **changes):
    values = dict(track_id="selected", frame_index=frame.frame_index, timestamp_ms=frame.timestamp_ms,
                  frame_key=frame.frame_key, frame_width=32, frame_height=24,
                  bbox=BoundingBox(x=.25, y=.25, w=.5, h=.5),
                  mask=MaskRaster(width=16, height=12, pixels=bytes([1] * 191 + [0])), confidence=None)
    values.update(changes)
    return SegmentObservation(**values)


def near(actual, expected, tolerance=20):
    """Masked video is 4:2:0 H.264 like the proxy, so colours are close, not bit-exact."""
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected))


def image_source(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (32, 24), (230, 12, 50)).save(source)
    return source


def test_image_exact_canvas_and_mask_hole(tmp_path):
    media = reference()
    result = mask_vehicle_media(image_source(tmp_path), media, [observation(media.frames[0])], tmp_path / "scratch", load_media_config())
    with Image.open(result.path) as image:
        assert image.size == (32, 24)
        assert image.getpixel((0, 0)) == BACKGROUND
        assert image.getpixel((8, 6)) == (230, 12, 50)
        assert image.getpixel((23, 17)) == BACKGROUND
    assert result.output_size_bytes == result.path.stat().st_size
    assert list((tmp_path / "scratch").iterdir()) == [result.path]


@pytest.mark.parametrize("mutation", ["missing", "empty", "dimensions", "time", "key", "extent", "too_few_frames"])
def test_invalid_masks_fail_closed(tmp_path, mutation):
    media = reference("video", 3) if mutation == "too_few_frames" else reference()
    observations = [observation(frame) for frame in media.frames]
    if mutation == "missing": observations = []
    elif mutation == "empty": observations[0] = observation(media.frames[0], mask=MaskRaster(width=16, height=12, pixels=bytes(192)))
    elif mutation == "dimensions": observations[0] = observation(media.frames[0], frame_width=30)
    elif mutation == "time": observations[0] = observation(media.frames[0], timestamp_ms=1)
    elif mutation == "key": observations[0] = observation(media.frames[0], frame_key="wrong/key")
    elif mutation == "extent": observations[0] = observation(media.frames[0], mask=MaskRaster(width=1, height=1, pixels=b"\x01"))
    else: observations = observations[:1]  # vehicle visible in 1 of 3 frames
    with pytest.raises(MediaError):
        mask_vehicle_media(image_source(tmp_path), media, observations, tmp_path / "scratch", load_media_config())
    assert not (tmp_path / "scratch").exists()


def make_video(tmp_path):
    for i in range(3):
        Image.new("RGB", (32, 24), (40 + i * 60, 10, 200)).save(tmp_path / f"input{i}.png")
    source = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-framerate", "5", "-i", str(tmp_path / "input%d.png"),
                    "-c:v", "libx264rgb", "-crf", "0", "-threads", "1", str(source)], check=True)
    return source


def test_video_preserves_frame_identity_and_rational_timing(tmp_path):
    config = load_media_config()
    source = make_video(tmp_path)
    media = reference("video", 3)
    scratch = tmp_path / "scratch"
    result = mask_vehicle_media(source, media, [observation(frame) for frame in media.frames], scratch, config)
    original = build_manifest(source, config, tmp_path)
    assert result.manifest is not None
    assert result.manifest.frame_count == 3
    assert [result.manifest.time_fraction(i) for i in range(3)] == [original.time_fraction(i) for i in range(3)]
    frames = extract_frames(result.path, result.manifest, range(3), tmp_path, config, image_format="png")
    for i, frame in enumerate(frames):
        with Image.open(frame.path) as image:
            assert image.size == (32, 24)
            assert near(image.getpixel((0, 0)), BACKGROUND)
            assert near(image.getpixel((10, 8)), (40 + i * 60, 10, 200))
            assert near(image.getpixel((28, 20)), BACKGROUND)
    assert list(scratch.iterdir()) == [result.path]


def test_real_sam_shapes_select_largest_vehicle_and_blank_missing_frames(tmp_path):
    # Real SAM output: a second parked car, a frame with no line, and a track id change.
    config = load_media_config()
    source = make_video(tmp_path)
    media = reference("video", 3)
    other_car = dict(track_id="9", bbox=BoundingBox(x=0, y=0, w=.25, h=.25),
                     mask=MaskRaster(width=8, height=6, pixels=bytes([1] * 48)))
    observations = [observation(media.frames[0], track_id="3"), observation(media.frames[0], **other_car),
                    observation(media.frames[2], track_id="12")]
    result = mask_vehicle_media(source, media, observations, tmp_path / "scratch", config)
    frames = extract_frames(result.path, result.manifest, range(3), tmp_path, config, image_format="png")
    pixels = []
    for frame in frames:
        with Image.open(frame.path) as image:
            pixels.append((image.getpixel((2, 2)), image.getpixel((12, 10))))
    expected = [(BACKGROUND, (40, 10, 200)),  # smaller car dropped, larger car kept
                (BACKGROUND, BACKGROUND),  # no vehicle line: nothing assessable
                (BACKGROUND, (160, 10, 200))]  # new track id still the vehicle
    assert all(near(a, b) for got, want in zip(pixels, expected) for a, b in zip(got, want))


@pytest.mark.parametrize("failure", ["timestamps", "frames", "geometry", "bytes", "scratch"])
def test_video_rejections_cleanup(tmp_path, failure):
    config = load_media_config()
    media = reference("video", 3)
    if failure == "timestamps": media = media.model_copy(update={"frames": tuple(frame.model_copy(update={"timestamp_ms": frame.timestamp_ms + 1}) for frame in media.frames)})
    if failure == "frames": media = media.model_copy(update={"frames": media.frames[:-1]})
    if failure == "geometry": media = media.model_copy(update={"stored_width": 30})
    if failure == "scratch": config = config.model_copy(update={"max_output_frames": 2})
    scratch = tmp_path / "scratch"
    with pytest.raises(MediaError):
        mask_vehicle_media(make_video(tmp_path), media, [observation(frame) for frame in media.frames], scratch, config,
                           max_output_bytes=100 if failure == "bytes" else 24 * 1024 * 1024)
    assert not scratch.exists() or not list(scratch.iterdir())


def test_image_decode_failure_cleans_output(tmp_path):
    source = tmp_path / "bad.png"
    source.write_bytes(b"invalid")
    media = reference()
    with pytest.raises(MediaError):
        mask_vehicle_media(source, media, [observation(media.frames[0])], tmp_path / "scratch", load_media_config())
    assert not list((tmp_path / "scratch").iterdir())


def test_image_output_limit_cleans_output(tmp_path):
    media = reference()
    with pytest.raises(MediaError):
        mask_vehicle_media(image_source(tmp_path), media, [observation(media.frames[0])], tmp_path / "scratch",
                           load_media_config(), max_output_bytes=1)
    assert not list((tmp_path / "scratch").iterdir())


def test_vfr_video_preserves_nonuniform_timestamps(tmp_path):
    source = make_video(tmp_path)
    vfr = tmp_path / "vfr.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-vf", "setpts=if(eq(N\\,2)\\,0.6/TB\\,PTS)",
                    "-fps_mode", "passthrough", "-c:v", "libx264rgb", "-crf", "0", "-threads", "1", str(vfr)], check=True)
    config = load_media_config().model_copy(update={"max_extract_frames": 2})
    original = build_manifest(vfr, config, tmp_path)
    media = reference("video", 3)
    media = media.model_copy(update={"frames": tuple(frame.model_copy(update={"timestamp_ms": round(original.time_seconds(i) * 1000)})
                                                    for i, frame in enumerate(media.frames))})
    result = mask_vehicle_media(vfr, media, [observation(frame) for frame in media.frames], tmp_path / "scratch", config)
    assert result.manifest is not None
    assert [result.manifest.time_fraction(i) for i in range(3)] == [original.time_fraction(i) for i in range(3)]


@pytest.mark.parametrize("seconds", [30, 60])
def test_normalized_long_video_uses_bounded_batches(tmp_path, monkeypatch, seconds):
    # These ordinary 1280x720/6fps proxies formerly exceeded the 200MiB
    # aggregate decoded-scratch check (roughly 950MiB and 1.9GiB respectively).
    import app.pipeline.vehicle_media as helper

    source = tmp_path / "long.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=1280x720:r=6",
        "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", str(source),
    ], check=True)
    config = load_media_config()
    original = build_manifest(source, config, tmp_path)
    media = MediaRef(media_key="source/long", kind="video", stored_width=1280, stored_height=720,
                    frames=tuple(FrameRef(frame_index=i, timestamp_ms=round(original.time_seconds(i) * 1000),
                                          frame_key=f"frames/{i}") for i in range(original.frame_count)))
    raster = MaskRaster(width=640, height=360, pixels=bytes([1]) * (640 * 360))
    observations = [SegmentObservation(
        track_id="selected", frame_index=f.frame_index, timestamp_ms=f.timestamp_ms, frame_key=f.frame_key,
        frame_width=1280, frame_height=720, bbox=BoundingBox(x=.25, y=.25, w=.5, h=.5), mask=raster, confidence=None,
    ) for f in media.frames]
    actual_extract = helper.extract_frames
    batch_sizes = []

    def bounded_extract(*args, **kwargs):
        batch_sizes.append(len(args[2]))
        assert len(args[2]) * (1280 * 720 * 12 + 4096) <= helper.MAX_BATCH_SCRATCH_BYTES
        # Earlier batches must have been removed before this batch starts.
        assert len(list(args[3].parent.glob("batch-*"))) == 1
        return actual_extract(*args, **kwargs)

    monkeypatch.setattr(helper, "extract_frames", bounded_extract)
    scratch = tmp_path / "scratch"
    result = mask_vehicle_media(source, media, observations, scratch, config)
    assert len(batch_sizes) > 1
    assert sum(batch_sizes) == seconds * 6
    assert result.manifest is not None
    assert result.manifest.pts == original.pts
    indices = [0, original.frame_count // 2, original.frame_count - 1]
    output_frames = actual_extract(result.path, result.manifest, indices, tmp_path, config, out_dir_name="masked", image_format="png")
    input_frames = actual_extract(source, original, indices, tmp_path, config, out_dir_name="original", image_format="png")
    for masked, unmasked in zip(output_frames, input_frames):
        with Image.open(masked.path) as image, Image.open(unmasked.path) as expected:
            assert image.size == expected.size == (1280, 720)
            assert near(image.getpixel((0, 0)), BACKGROUND)
            assert near(image.getpixel((320, 180)), expected.getpixel((320, 180)))
            assert near(image.getpixel((960, 540)), BACKGROUND)
    assert list(scratch.iterdir()) == [result.path]


def test_second_batch_failure_removes_prior_chunks(tmp_path, monkeypatch):
    import app.pipeline.vehicle_media as helper

    config = load_media_config().model_copy(update={"max_extract_frames": 2})
    media = reference("video", 3)
    actual_mask = helper._mask_image
    calls = 0

    def fail_third(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic decode failure")
        return actual_mask(*args)

    monkeypatch.setattr(helper, "_mask_image", fail_third)
    scratch = tmp_path / "scratch"
    with pytest.raises(MediaError):
        mask_vehicle_media(make_video(tmp_path), media, [observation(f) for f in media.frames], scratch, config)
    assert calls == 3
    assert not list(scratch.iterdir())


def test_masks_apply_to_exact_frame_across_batches(tmp_path):
    config = load_media_config().model_copy(update={"max_extract_frames": 1})
    media = reference("video", 3)
    observations = [observation(f) for f in media.frames]
    # A distinct 4x4 hole (chroma-block aligned) on each frame catches adjacent-mask reuse at batch edges.
    for i, frame in enumerate(media.frames):
        pixels = bytearray([1] * 192)
        for y in range(4):
            pixels[y * 16 + 4 * i:y * 16 + 4 * i + 4] = bytes(4)
        observations[i] = observation(frame, mask=MaskRaster(width=16, height=12, pixels=bytes(pixels)))
    result = mask_vehicle_media(make_video(tmp_path), media, observations, tmp_path / "scratch", config)
    assert result.manifest is not None
    # Extraction itself remains bounded to one frame per call.
    for i in range(3):
        frame = extract_frames(result.path, result.manifest, [i], tmp_path, config,
                               out_dir_name=f"check-{i}", image_format="png")[0]
        with Image.open(frame.path) as image:
            # Lossy 4:2:0 blurs tiny holes, so assert where the hole is, not bit-exact pixels.
            distance = [sum(abs(a - b) for a, b in zip(image.getpixel((9 + 4 * x, 7)), BACKGROUND)) for x in range(3)]
            assert distance.index(min(distance)) == i


def test_encoder_timeout_removes_partial_output(tmp_path, monkeypatch):
    import app.pipeline.vehicle_media as helper
    from app.pipeline.media import MediaReason, ToolRun

    source = make_video(tmp_path)
    media = reference("video", 3)

    def timeout(args, **kwargs):
        # Model a subprocess that leaves an incomplete output before timing out.
        from pathlib import Path
        Path(args[-1]).write_bytes(b"partial")
        return ToolRun(returncode=-9, stdout=b"", stderr="", timed_out=True)

    monkeypatch.setattr(helper, "run_tool", timeout)
    scratch = tmp_path / "scratch"
    with pytest.raises(MediaError) as exc:
        mask_vehicle_media(source, media, [observation(f) for f in media.frames], scratch, load_media_config())
    assert exc.value.reason == MediaReason.TIMEOUT
    assert not list(scratch.iterdir())
