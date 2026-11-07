import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.adapters.meta import (
    FixtureResponse, FixtureSegmenter, LiveConfiguration, LiveSegmenter, MediaRef,
    PRICE_VERSION, ProviderFailure, ProviderUsage, normalize_output, sam_request_micro_usd,
)

REQUEST = UUID("00000000-0000-4000-8000-000000000001")
RESPONSES = {key: FixtureResponse.model_validate(value) for key, value in json.loads(
    (Path(__file__).parent / "fixtures/meta/responses.json").read_text(encoding="utf-8")
).items()}


def media(*, video: bool = False, rotation: int = 0) -> MediaRef:
    frames = [{"frame_index": 0, "timestamp_ms": 0, "frame_key": "synthetic/frame-0.png"}]
    if video:
        frames.extend([
            {"frame_index": 1, "timestamp_ms": 33, "frame_key": "synthetic/frame-1.png"},
            {"frame_index": 2, "timestamp_ms": 67, "frame_key": "synthetic/frame-2.png"},
        ])
    return MediaRef(media_key="synthetic/input.mp4", kind="video" if video else "image",
                    stored_width=4, stored_height=3, rotation_degrees=rotation, frames=frames)


def test_image_real_official_parser_and_binary_mask() -> None:
    result = FixtureSegmenter(RESPONSES).segment(media(), "image", REQUEST)
    assert result.provenance == "synthetic"
    assert result.parser_version == "meta-sam-parser==0.1.0"
    observation, = result.observations
    assert observation.track_id == "7"
    assert observation.bbox.model_dump() == {"x": 0.25, "y": 0.0, "w": 0.5, "h": 2 / 3}
    assert observation.mask.pixels == bytes([1, 0, 0, 1])
    assert observation.frame_key == "synthetic/frame-0.png"
    assert observation.confidence is None


def test_sparse_out_of_order_frames_preserve_explicit_identity_and_time() -> None:
    result = FixtureSegmenter(RESPONSES).segment(media(video=True), "video", REQUEST)
    assert [item.frame_index for item in result.observations] == [0, 2]
    assert [item.timestamp_ms for item in result.observations] == [0, 67]
    assert [item.frame_key for item in result.observations] == ["synthetic/frame-0.png", "synthetic/frame-2.png"]
    assert {item.track_id for item in result.observations} == {"7"}


def test_rotated_dimensions_no_double_rotation() -> None:
    result = FixtureSegmenter(RESPONSES).segment(media(rotation=90), "rotated", REQUEST)
    item, = result.observations
    assert (item.frame_width, item.frame_height) == (3, 4)
    assert item.bbox.model_dump() == {"x": 1 / 3, "y": 0.5, "w": 2 / 3, "h": 0.5}
    with pytest.raises(ProviderFailure, match="source_dimensions_mismatch"):
        FixtureSegmenter(RESPONSES).segment(media(), "rotated", REQUEST)


def test_empty_completed_lane_is_zero_candidates() -> None:
    result = FixtureSegmenter(RESPONSES).segment(media(), "empty", REQUEST)
    assert result.observations == ()
    assert result.usage.images_processed == 1


@pytest.mark.parametrize("name,code", [
    ("malformed_mask", "invalid_segmentation"), ("malformed_geometry", "invalid_segmentation"),
    ("timeout", "provider_timeout"), ("failed", "provider_failed"),
    ("incomplete", "incomplete_response"), ("missing", "fixture_missing"),
])
def test_failure_cannot_become_clean_result(name: str, code: str) -> None:
    with pytest.raises(ProviderFailure) as caught:
        FixtureSegmenter(RESPONSES).segment(media(), name, REQUEST)
    assert str(caught.value) == code


@pytest.mark.parametrize("replacement,code", [
    (("<0f>", "<9f>"), "unknown_source_frame"),
    (("data=2,2,", "data=99999,99999,"), "mask_limit"),
    (("w=4;h=3", "w=3;h=4"), "source_dimensions_mismatch"),
    (("x=0;y=0", "x=1;y=0"), "invalid_segmentation"),
])
def test_invalid_provider_payload(replacement: tuple[str, str], code: str) -> None:
    output = RESPONSES["image"].output_text.replace(*replacement)
    with pytest.raises(ProviderFailure, match=code):
        normalize_output(output, media(video=True), "test", REQUEST, usage=ProviderUsage())


def test_unstructured_and_json_text_not_treated_as_geometry() -> None:
    for output in ["[]", '{"objects":[]}', "No damage found"]:
        with pytest.raises(ProviderFailure):
            normalize_output(output, media(), "test", REQUEST, usage=ProviderUsage())


def test_diagnostics_reject_partial_success() -> None:
    output = RESPONSES["image"].output_text + "\n<0f>bad record"
    with pytest.raises(ProviderFailure):
        normalize_output(output, media(), "test", REQUEST, usage=ProviderUsage())


def test_repeated_frame_track_keeps_latest_revision() -> None:
    # The official parser numbers a re-emitted (frame, object) as the next revision.
    first = RESPONSES["image"].output_text
    later = first.replace("x1=1;y1=0;x2=2;y2=1", "x1=0;y1=0;x2=1;y2=1")
    result = normalize_output(first + "\n" + later, media(), "test", REQUEST, usage=ProviderUsage())
    observation, = result.observations
    assert observation.bbox.x == 0


@pytest.mark.parametrize("code,retryable", [
    ("provider_timeout", True), ("provider_failed", True), ("incomplete_response", True),
    ("mask_alignment_mismatch", False), ("invalid_segmentation", False), ("mask_limit", False),
    ("malformed_record", False), ("unknown_source_frame", False),
])
def test_only_transport_failures_are_retried_because_responses_are_billed(code: str, retryable: bool) -> None:
    assert ProviderFailure(code).retryable is retryable


def test_parser_warnings_do_not_reject_kept_records() -> None:
    output = RESPONSES["image"].output_text.replace("w=4;h=3", "w=4;h=3;future=1")
    observation, = normalize_output(output, media(), "test", REQUEST, usage=ProviderUsage()).observations
    assert observation.mask.pixels == bytes([1, 0, 0, 1])


def test_confidence_optional_and_uncalibrated() -> None:
    output = RESPONSES["image"].output_text.replace("w=4;h=3", "w=4;h=3;c=0.8").replace("x=0;y=0;data", "x=0;y=0;c=0.8;data")
    result = normalize_output(output, media(), "test", REQUEST, usage=ProviderUsage())
    assert result.observations[0].confidence == 0.8
    with pytest.raises(ProviderFailure):
        normalize_output(output.replace("c=0.8;data", "c=0.7;data"), media(), "test", REQUEST, usage=ProviderUsage())


@pytest.mark.parametrize("configuration,code", [
    ({"model": "sam-3-1"}, "model_unverified"),
    ({"model": "sam-3.1"}, "price_unverified"),
    ({"model": "sam-3.1", "price_version": PRICE_VERSION}, "standard_access_unverified"),
    ({"model": "sam-3.1", "price_version": PRICE_VERSION, "standard_access_receipt": "synthetic-not-access", "capability_receipt": "synthetic-not-capability"}, "budget_not_reserved"),
    ({"model": "sam-3.1", "price_version": PRICE_VERSION, "standard_access_receipt": "synthetic-not-access", "capability_receipt": "synthetic-not-capability", "budget_reservation_id": REQUEST}, "live_transport_disabled"),
])
def test_live_boundary_always_closed(configuration: dict, code: str) -> None:
    with pytest.raises(ProviderFailure, match=code):
        LiveSegmenter(LiveConfiguration(**configuration)).segment(media(), "test", REQUEST)


def test_documented_request_prices_with_fifty_frame_minimum() -> None:
    assert sam_request_micro_usd(images=1) == 2500
    assert sam_request_micro_usd(frames=1) == 10000
    assert sam_request_micro_usd(frames=49) == 10000
    assert sam_request_micro_usd(frames=50) == 10000
    assert sam_request_micro_usd(frames=80) == 16000
    for units in [{}, {"frames": -1}, {"images": 1, "frames": 1}, {"frames": True}]:
        with pytest.raises(ValueError):
            sam_request_micro_usd(**units)


def test_no_keys_or_network_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def forbid(*args: object, **kwargs: object) -> None:
        raise AssertionError("network forbidden")

    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr(socket, "socket", forbid)
    FixtureSegmenter(RESPONSES).segment(media(), "image", REQUEST)


def test_manifest_requires_unique_frames_and_correct_image_index() -> None:
    valid = media().model_dump()
    with pytest.raises(ValidationError):
        MediaRef.model_validate({**valid, "frames": valid["frames"] * 2})


def test_quota_preflight_happens_before_official_mask_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.adapters.meta import parser as module

    def forbid() -> None:
        raise AssertionError("official decoder must not receive over-quota payload")

    monkeypatch.setattr(module, "image_segmentation_format", forbid)
    output = RESPONSES["image"].output_text.replace("data=2,2,", "data=1280,1280,")
    with pytest.raises(ProviderFailure, match="mask_limit"):
        normalize_output("\n".join([output] * 11), media(), "test", REQUEST, usage=ProviderUsage())
    monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 10)
    with pytest.raises(ProviderFailure, match="output_limit"):
        normalize_output(RESPONSES["image"].output_text, media(), "test", REQUEST, usage=ProviderUsage())


def test_box_local_mask_at_other_resolution_is_resampled_onto_box() -> None:
    # 2x2 diagonal raster on a 4x2 box: nearest-neighbour keeps it binary and box-local.
    output = RESPONSES["image"].output_text.replace("x1=1;y1=0;x2=2;y2=1", "x1=0;y1=0;x2=3;y2=1")
    observation, = normalize_output(output, media(), "test", REQUEST, usage=ProviderUsage()).observations
    assert (observation.mask.width, observation.mask.height) == (4, 2)
    assert observation.mask.pixels == bytes([1, 1, 0, 0, 0, 0, 1, 1])
    assert observation.bbox.model_dump() == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 2 / 3}


def test_downsampled_mask_on_odd_box_extent_is_resampled() -> None:
    output = RESPONSES["image"].output_text.replace("x2=2", "x2=3")
    observation, = normalize_output(output, media(), "test", REQUEST, usage=ProviderUsage()).observations
    assert (observation.mask.width, observation.mask.height) == (3, 2)
    assert observation.mask.pixels == bytes([1, 1, 0, 0, 0, 1])


def test_video_documented_track_cap_is_explicit() -> None:
    record = RESPONSES["image"].output_text.removeprefix("<0f>7")
    output = "<0f>" + ",".join(str(index) + record for index in range(16))
    result = normalize_output(output, media(video=True), "test", REQUEST, usage=ProviderUsage())
    assert len(result.observations) == 16
    assert result.limits_encountered == ("documented_video_track_cap_reached",)


# Synthetic all-ones 4x3 mask payload from the pinned parser's test-only encoder.
_FULL_4X3_PAYLOAD = "!!!!!'y7K#I-="


def _full_frame_line(frame_index: int, track: str = "1", *, width: int = 4, height: int = 3,
                     payload: str = _FULL_4X3_PAYLOAD) -> str:
    return (f"<{frame_index}f>{track}<|box;x1=0;y1=0;x2={width - 1};y2={height - 1};"
            f"w={width};h={height}|><|mask;x=0;y=0;data={height},{width},{payload}|>")


def _video_media(frame_count: int, *, width: int = 4, height: int = 3) -> MediaRef:
    return MediaRef(media_key="synthetic/input.mp4", kind="video", stored_width=width,
                    stored_height=height,
                    frames=[{"frame_index": index, "timestamp_ms": index * 33,
                             "frame_key": f"synthetic/frame-{index}.png"}
                            for index in range(frame_count)])


def test_multiframe_near_full_frame_masks_parse_all_frames() -> None:
    result = normalize_output("\n".join(_full_frame_line(index) for index in range(46)),
                              _video_media(46), "vehicle", REQUEST, usage=ProviderUsage())
    assert [item.frame_index for item in result.observations] == list(range(46))
    assert all(item.mask.width == 4 and item.mask.height == 3 for item in result.observations)
    assert all(item.mask.pixels == bytes([1] * 12) for item in result.observations)
    assert result.limits_encountered == ()


def test_incident_scale_vehicle_masks_pass_preflight() -> None:
    from app.adapters.meta import parser as module

    incident = _video_media(46, width=1280, height=720)
    # Declared dims only: preflight runs before any mask decode.
    output = "\n".join(
        f"<{index}f>1<|box;x1=0;y1=0;x2=1279;y2=719;w=1280;h=720|>"
        "<|mask;x=0;y=0;data=720,1280,!synthetic|>" for index in range(46))
    assert module._per_mask_pixel_limit(incident) == 1280 * 720
    assert module._total_mask_pixel_limit(incident) == 46 * 1280 * 1280
    module._preflight(output, incident)


def test_single_mask_larger_than_frame_raises_mask_limit() -> None:
    output = ("<0f>1<|box;x1=0;y1=0;x2=3;y2=2;w=4;h=3|>"
              "<|mask;x=0;y=0;data=4,4,!synthetic|>")
    with pytest.raises(ProviderFailure, match="mask_limit"):
        normalize_output(output, media(), "vehicle", REQUEST, usage=ProviderUsage())


def test_total_mask_pixels_over_frame_aware_limit_raises_mask_limit() -> None:
    # Each mask is exactly at the per-mask cap, so only the frame-aware
    # total (3 frames x per-mask cap) can reject the fourth mask.
    record = ("<|box;x1=0;y1=0;x2=1279;y2=1279;w=1280;h=1280|>"
              "<|mask;x=0;y=0;data=1280,1280,!synthetic|>")
    output = "<0f>" + ",".join(f"{track}{record}" for track in range(1, 5))
    with pytest.raises(ProviderFailure, match="mask_limit"):
        normalize_output(output, _video_media(3, width=1280, height=1280),
                         "vehicle", REQUEST, usage=ProviderUsage())


def test_track_ids_cannot_escape_safe_sort_conversion() -> None:
    output = RESPONSES["image"].output_text.replace("<0f>7", "<0f>" + "9" * 129)
    with pytest.raises(ProviderFailure, match="invalid_track_id"):
        normalize_output(output, media(), "test", REQUEST, usage=ProviderUsage())
