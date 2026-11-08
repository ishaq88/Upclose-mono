"""Reduced vehicle-mask input: keyframes, nearest-keyframe mapping, mask upsampling, config bounds."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.adapters.meta.types import FrameRef, MaskRaster, MediaRef, SegmentObservation
from app.pipeline.media import DEFAULT_CONFIG_PATH, FrameManifest, MediaConfig, load_media_config
from app.pipeline.vehicle_keyframes import (derived_size, expand_to_proxy_frames, mask_keyframe_indices,
                                            nearest_keyframe_positions)


def manifest(frames: int = 72, fps: int = 6, width: int = 1280, height: int = 720) -> FrameManifest:
    return FrameManifest(proxy_sha256="0" * 64, width=width, height=height, time_base_num=1, time_base_den=fps,
                         pts=tuple(range(frames)))


def media(m: FrameManifest, *, key: str = "proxy") -> MediaRef:
    return MediaRef(media_key=key, kind="video", stored_width=m.width, stored_height=m.height,
                    frames=tuple(FrameRef(frame_index=i, timestamp_ms=round(m.time_seconds(i) * 1000),
                                          frame_key=f"f/{i}") for i in range(m.frame_count)))


def test_no_rate_keeps_every_frame_and_two_fps_takes_every_third_of_six():
    m = manifest()
    assert mask_keyframe_indices(m, None) == tuple(range(72))
    keys = mask_keyframe_indices(m, 2)
    assert keys == tuple(range(0, 72, 3)) and len(keys) == 24
    assert mask_keyframe_indices(m, 6) == tuple(range(72))
    assert mask_keyframe_indices(m, 60) == tuple(range(72))  # never more than the proxy has


def test_irregular_timestamps_pick_the_nearest_frame_per_tick():
    m = FrameManifest(proxy_sha256="0" * 64, width=4, height=4, time_base_num=1, time_base_den=100,
                      pts=(0, 40, 90, 130, 205, 260))
    # duration 2.6 s (span 2.6 + median delta 0.5? no: span + median delta), ticks every 0.5 s
    keys = mask_keyframe_indices(m, 2)
    assert keys[0] == 0 and list(keys) == sorted(set(keys))
    assert 2 in keys  # tick 1.0 s is nearest the 0.90 s frame


def test_every_frame_maps_to_its_nearest_keyframe_ties_earlier():
    m = manifest()
    keys = mask_keyframe_indices(m, 2)
    positions = nearest_keyframe_positions(m, keys)
    assert len(positions) == 72
    assert all(keys[positions[k]] == k for k in keys)  # a keyframe maps to itself
    for frame, position in enumerate(positions):
        nearest = min(range(len(keys)), key=lambda q: (abs(keys[q] - frame), keys[q]))
        assert position == nearest
    assert positions[:6] == (0, 0, 1, 1, 1, 2)  # frame 1 is nearer key 0; frame 2 is nearer key 3
    assert positions[-1] == len(keys) - 1  # the tail uses the last keyframe


def test_a_tie_goes_to_the_earlier_keyframe():
    m = manifest(5)
    assert nearest_keyframe_positions(m, (0, 4)) == (0, 0, 0, 1, 1)  # frame 2 is equidistant: earlier


def test_keyframes_must_be_ascending_unique_and_inside_the_manifest():
    m = manifest(12)
    for bad in ((), (3, 1), (1, 1), (-1, 2), (0, 12)):
        with pytest.raises(ValueError):
            nearest_keyframe_positions(m, bad)


def test_derived_size_is_proxy_sized_when_unset_even_and_never_upscaled():
    m = manifest()
    assert derived_size(m, None) == (1280, 720)
    assert derived_size(m, 640) == (640, 360)
    assert derived_size(m, 4000) == (1280, 720)
    assert derived_size(manifest(width=1000, height=562), 500) == (500, 280)


def _obs(position: int, derived: MediaRef, *, box=(2, 1, 4, 2), fill=1) -> SegmentObservation:
    width, height = derived.stored_width, derived.stored_height
    x, y, w, h = box
    frame = derived.frames[position]
    return SegmentObservation(track_id="1", frame_index=position, frame_key=frame.frame_key,
        timestamp_ms=frame.timestamp_ms, frame_width=width, frame_height=height,
        bbox={"x": x / width, "y": y / height, "w": w / width, "h": h / height},
        mask=MaskRaster(width=w, height=h, pixels=bytes([fill]) * (w * h)), confidence=None)


def test_expansion_gives_every_proxy_frame_its_nearest_keyframe_mask_with_its_own_identity():
    m = manifest(12)
    proxy = media(m)
    keys = mask_keyframe_indices(m, 3)  # 6 fps -> every second frame
    derived = media(manifest(len(keys), 3, 1280, 720), key="derived")
    obs = tuple(_obs(p, derived, box=(p + 1, 1, 4, 2)) for p in range(len(keys)))
    positions = nearest_keyframe_positions(m, keys)
    out = expand_to_proxy_frames(proxy, derived, keys, positions, obs)
    assert [o.frame_index for o in out] == list(range(12))
    assert [o.frame_key for o in out] == [f"f/{i}" for i in range(12)]
    assert [o.timestamp_ms for o in out] == [round(i / 6 * 1000) for i in range(12)]
    assert [round(o.bbox.x * 1280) for o in out] == [p + 1 for p in positions]


def test_a_keyframe_without_a_vehicle_leaves_its_frames_unmasked():
    m = manifest(6)
    proxy, keys = media(m), (0, 3)
    derived = media(manifest(2, 1), key="derived")
    out = expand_to_proxy_frames(proxy, derived, keys, nearest_keyframe_positions(m, keys), (_obs(1, derived),))
    assert [o.frame_index for o in out] == [2, 3, 4, 5]


def test_downscaled_masks_are_upsampled_onto_the_proxy_frame_and_stay_binary():
    m = manifest(2)
    proxy = media(m)
    small = MediaRef(media_key="derived", kind="video", stored_width=640, stored_height=360,
                     frames=(FrameRef(frame_index=0, timestamp_ms=0, frame_key="d/0"),))
    out = expand_to_proxy_frames(proxy, small, (0,), (0, 0), (_obs(0, small, box=(100, 50, 200, 100)),))
    assert len(out) == 2
    for o in out:
        assert (o.frame_width, o.frame_height) == (1280, 720)
        assert (round(o.bbox.x * 1280), round(o.bbox.y * 720), o.mask.width, o.mask.height) == (200, 100, 400, 200)
        assert set(o.mask.pixels) == {1}
    assert out[0].mask is out[1].mask  # upsampled once per keyframe


def test_upsampling_keeps_a_diagonal_between_zero_and_one():
    small = MediaRef(media_key="d", kind="video", stored_width=640, stored_height=360,
                     frames=(FrameRef(frame_index=0, timestamp_ms=0, frame_key="d/0"),))
    item = SegmentObservation(track_id="1", frame_index=0, frame_key="d/0", timestamp_ms=0, frame_width=640,
        frame_height=360, bbox={"x": 0.0, "y": 0.0, "w": 2 / 640, "h": 2 / 360},
        mask=MaskRaster(width=2, height=2, pixels=bytes([1, 0, 0, 1])), confidence=None)
    out, = expand_to_proxy_frames(media(manifest(1)), small, (0,), (0,), (item,))
    assert (out.mask.width, out.mask.height) == (4, 4)
    assert out.mask.pixels[0] == 1 and out.mask.pixels[3] == 0 and out.mask.pixels[-1] == 1
    assert set(out.mask.pixels) <= {0, 1}


def test_mismatched_mapping_or_foreign_observation_is_rejected():
    proxy, derived = media(manifest(4)), media(manifest(2, 3), key="d")
    with pytest.raises(ValueError):
        expand_to_proxy_frames(proxy, derived, (0, 2), (0, 0, 1), ())
    with pytest.raises(ValueError):
        expand_to_proxy_frames(proxy, derived, (0, 2), (0, 0, 1, 1),
                               (_obs(0, derived).model_copy(update={"frame_index": 5}),))


def _config(**extra) -> MediaConfig:
    data = json.loads(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    data.pop("vehicle_mask_sample_fps", None)
    data.pop("vehicle_mask_long_edge", None)
    return MediaConfig.model_validate({**data, **extra})


def test_mask_config_defaults_off_and_is_validated_against_the_plans():
    base = _config()
    assert base.vehicle_mask_sample_fps is None and base.vehicle_mask_long_edge is None
    assert _config(vehicle_mask_sample_fps=2, vehicle_mask_long_edge=640).vehicle_mask_sample_fps == 2
    for bad in ({"vehicle_mask_sample_fps": 0}, {"vehicle_mask_sample_fps": 5},  # above part 7's 4 fps plan
                {"vehicle_mask_long_edge": 8}, {"vehicle_mask_long_edge": 1281}, {"vehicle_mask_long_edge": 640.5}):
        with pytest.raises(ValidationError):
            _config(**bad)


def test_the_mask_settings_are_part_of_the_media_content_hash():
    assert _config().content_hash != _config(vehicle_mask_long_edge=640).content_hash
    assert load_media_config().content_hash  # the committed config loads


def test_marker_without_reduced_input_dumps_its_original_bytes():
    # Frozen report manifests embed this marker and hash its canonical JSON: a marker written before
    # the reduced-input fields existed must serialize exactly as it did, or every old report fails
    # its integrity check.
    from uuid import uuid4

    from app.pipeline.vehicle_mask_contract import MaskedInput

    base = {"kind": "video", "source_key": "media/s/p/r/a1/proxy.mp4", "source_sha256": "a" * 64,
            "masked_key": "media/s/p/r/vehicle/a1/0.mp4", "masked_sha256": "b" * 64,
            "mask_key": "media/s/p/r/vehicle/a1/0-masks.zip", "mask_sha256": "c" * 64,
            "request_id": str(uuid4()), "provenance": "live", "frame_indices": [0, 1], "timestamps_ms": [0, 167]}
    old = MaskedInput.model_validate(base)
    assert set(old.model_dump(mode="json")) == set(base)
    reduced = MaskedInput.model_validate({**base, "mask_keyframes": [0], "mask_input_size": [640, 360]})
    assert reduced.model_dump(mode="json")["mask_keyframes"] == [0]
    assert reduced.model_dump(mode="json")["mask_input_size"] == [640, 360]
