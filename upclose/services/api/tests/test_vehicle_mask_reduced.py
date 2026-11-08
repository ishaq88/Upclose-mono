"""Reduced vehicle-mask input end to end (real PostgreSQL/Floci/ffmpeg, synthetic providers)."""
from __future__ import annotations

import shutil

import pytest

import app.pipeline.vehicle_mask as vehicle_mask
from app.contracts.enums import PartState
from app.models import Artifact, Observation
from tests.test_ingest import media_dir, scratch_root, stored  # noqa: F401
from tests.test_uploads import bucket, make, session_factory  # noqa: F401
from tests.test_orchestration import part, rows, setup, upload
from tests.test_vehicle_mask_pipeline import RecordingProvider, isolate, pipe_for  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def test_reduced_mask_input_is_sent_once_removed_and_expressed_on_every_frame(make, media_dir, scratch_root, monkeypatch):
    config = vehicle_mask.load_media_config().model_copy(
        update={"vehicle_mask_sample_fps": 2.0, "vehicle_mask_long_edge": 160})
    monkeypatch.setattr(vehicle_mask, "load_media_config", lambda *args, **kwargs: config)
    h = setup(make, scratch_root)
    provider = RecordingProvider(h)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, 1)
    assert pipe.step() and pipe.step()  # ingest, then the mask stage
    (prompt, sent), = provider.calls
    marker, = rows(h, Observation, Observation.kind == "vehicle_mask")
    item, = marker.value["media"]
    assert prompt == "vehicle"
    assert (sent.stored_width, sent.stored_height) == (160, 90)  # the 320x180 proxy, long edge 160
    assert item["mask_input_size"] == [160, 90]
    assert len(sent.frames) == len(item["mask_keyframes"]) < len(item["frame_indices"])
    assert item["mask_keyframes"][0] == 0
    assert not any("mask-input" in artifact.key for artifact in rows(h, Artifact))
    with pytest.raises(Exception):
        stored(h, sent.media_key)  # the derived input was removed after the request
    pipe.drain()
    assert part(h, 1).state == PartState.PROCESSED
    assert not any(scratch_root.rglob("*"))


def test_unset_mask_config_sends_the_full_proxy(make, media_dir, scratch_root, monkeypatch):
    config = vehicle_mask.load_media_config().model_copy(
        update={"vehicle_mask_sample_fps": None, "vehicle_mask_long_edge": None})
    monkeypatch.setattr(vehicle_mask, "load_media_config", lambda *args, **kwargs: config)
    h = setup(make, scratch_root)
    provider = RecordingProvider(h)
    pipe = pipe_for(h, provider)
    upload(h, media_dir, 1)
    assert pipe.step() and pipe.step()
    (_, sent), = provider.calls
    marker, = rows(h, Observation, Observation.kind == "vehicle_mask")
    item, = marker.value["media"]
    assert sent.media_key == item["source_key"] and len(sent.frames) == len(item["frame_indices"])
    assert "mask_keyframes" not in item or item["mask_keyframes"] is None
