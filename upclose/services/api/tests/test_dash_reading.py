"""Frame selection and the dash run on synthetic arrays. No network."""
import io
from decimal import Decimal

import pytest
from PIL import Image, ImageDraw

from app.adapters.dash_reader import FixtureDashReader
from app.adapters.meta import MediaRef, ProviderFailure, SegmentResult
from app.adapters.meta.types import MaskRaster, ProviderUsage, SegmentObservation
from app.domain.dash_lights import load_dash_config
from app.pipeline.dash_reading import (Candidate, DashStill, choose, crop_box, run_dash_reading,
                                       score_candidate, select_cluster)
from app.contracts.evidence import BoundingBox

CONFIG = load_dash_config()
W, H = 200, 100
GOOD_JSON = ('{"cluster_readable":true,"engine_running_evidence":"tach_above_zero",'
             '"lit_indicators":[{"indicator":"check_engine","confidence":4}],"notes":""}')


def png(sharp: bool) -> bytes:
    image = Image.new("RGB", (W, H), (20, 20, 20))
    if sharp:
        draw = ImageDraw.Draw(image)
        for x in range(20, 180, 8):
            draw.rectangle((x, 20, x + 3, 80), fill=(240, 240, 240))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def still(key: str, sharp=True) -> DashStill:
    media = MediaRef(media_key=key, kind="image", stored_width=W, stored_height=H,
                     frames=[{"frame_index": 0, "timestamp_ms": 0, "frame_key": key}])
    return DashStill(media, png(sharp))


def observation(key: str, box=(20, 10, 180, 90), fill=1.0) -> SegmentObservation:
    left, top, right, bottom = box
    bw, bh = right - left, bottom - top
    count = int(bw * bh * fill)
    pixels = bytes([1] * count + [0] * (bw * bh - count))
    return SegmentObservation(track_id="1", frame_index=0, frame_key=key, timestamp_ms=0, frame_width=W,
                              frame_height=H, bbox={"x": left / W, "y": top / H, "w": bw / W, "h": bh / H},
                              mask=MaskRaster(width=bw, height=bh, pixels=pixels), confidence=0.9)


def result(*obs) -> SegmentResult:
    return SegmentResult(request_id="00000000-0000-4000-8000-000000000000", prompt_id="dash_cluster",
                         provenance="synthetic", observations=tuple(obs), usage=ProviderUsage())


def sam_for(table):
    def call(media, prompt_id, request_id):
        assert prompt_id == CONFIG.sam_prompt.prompt_id
        value = table[media.media_key]
        if isinstance(value, Exception):
            raise value
        return value
    return call


def test_too_few_frames():
    selection = select_cluster([still("a")], sam_for({}), CONFIG)
    assert selection.crop is None and selection.reason == "too_few_frames"


def test_no_cluster_found():
    stills = [still("a"), still("b")]
    selection = select_cluster(stills, sam_for({"a": result(), "b": result()}), CONFIG)
    assert selection.crop is None and selection.reason == "cluster_not_found"


def test_tiny_cluster_ignored():
    tiny = observation("a", box=(0, 0, 10, 10))
    selection = select_cluster([still("a"), still("b")],
                               sam_for({"a": result(tiny), "b": result(observation("b", box=(0, 0, 10, 10)))}), CONFIG)
    assert selection.reason == "cluster_not_found"


def test_prefers_larger_sharper_frame_and_crops_with_margin():
    stills = [still("a", sharp=False), still("b", sharp=True), still("c", sharp=True)]
    table = {"a": result(observation("a")), "b": result(observation("b")),
             "c": result(observation("c", box=(60, 30, 120, 70)))}
    selection = select_cluster(stills, sam_for(table), CONFIG)
    assert selection.reason is None and selection.crop.source_frame_key == "b"
    cluster, region = selection.crop.cluster_bbox, selection.crop.source_bbox
    assert region.w > cluster.w and region.x < cluster.x
    assert Image.open(io.BytesIO(selection.crop.png)).size == (selection.crop.width, selection.crop.height)


def test_all_blurry_is_not_readable():
    stills = [still("a", sharp=False), still("b", sharp=False)]
    table = {"a": result(observation("a")), "b": result(observation("b"))}
    selection = select_cluster(stills, sam_for(table), CONFIG)
    assert selection.crop is None and selection.reason == "cluster_blurry"


def test_retryable_sam_failure_propagates_but_other_frame_can_win():
    stills = [still("a"), still("b")]
    table = {"a": ProviderFailure("provider_timeout"), "b": result(observation("b"))}
    assert select_cluster(stills, sam_for(table), CONFIG).crop.source_frame_key == "b"
    table = {"a": ProviderFailure("provider_timeout"), "b": ProviderFailure("provider_timeout")}
    with pytest.raises(ProviderFailure):
        select_cluster(stills, sam_for(table), CONFIG)


def test_nonretryable_sam_failure_is_not_readable():
    table = {"a": ProviderFailure("invalid_segmentation"), "b": ProviderFailure("invalid_segmentation")}
    assert select_cluster([still("a"), still("b")], sam_for(table), CONFIG).reason == "cluster_not_found"


def test_choose_and_score_pure():
    low = Candidate(0, "a", BoundingBox(x=0, y=0, w=1, h=1), Decimal("0.01"), Decimal(10), Decimal("0.1"))
    high = Candidate(1, "b", BoundingBox(x=0, y=0, w=1, h=1), Decimal("0.3"), Decimal(500), Decimal("1"))
    assert choose([low, high]) is high and choose([]) is None
    assert score_candidate(Decimal("0.5"), Decimal(1000), CONFIG) == Decimal(1)


def test_crop_box_clamped():
    box = crop_box(BoundingBox(x=0.0, y=0.9, w=0.5, h=0.1), Decimal("0.5"))
    assert box.x == 0 and box.y + box.h <= 1.0 + 1e-9


def test_full_run_with_fixture_reader_and_obd_flag():
    stills = [still("a"), still("b")]
    table = {"a": result(observation("a")), "b": result(observation("b"))}
    run = run_dash_reading(stills, sam_for(table), FixtureDashReader(GOOD_JSON), CONFIG, obd_mil_on=False)
    assert run.assessment.state == "assessed" and run.assessment.flags == ("mil_mismatch",)
    assert run.reader.provenance == "synthetic"


def test_full_run_malformed_reader_output_is_not_readable():
    stills = [still("a"), still("b")]
    table = {"a": result(observation("a")), "b": result(observation("b"))}
    run = run_dash_reading(stills, sam_for(table), FixtureDashReader("{not json"), CONFIG)
    assert run.assessment.state == "not_readable" and not run.assessment.is_clean
    assert run.assessment.reason.startswith("invalid_response")


def test_full_run_no_cluster_never_calls_reader():
    class Boom:
        requires_budget = False
        def read(self, image, *, grant=None):
            raise AssertionError("reader must not run")
    run = run_dash_reading([still("a"), still("b")], sam_for({"a": result(), "b": result()}), Boom(), CONFIG)
    assert run.assessment.state == "not_readable" and run.reader is None
