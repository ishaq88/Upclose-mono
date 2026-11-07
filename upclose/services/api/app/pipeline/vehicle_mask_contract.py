"""Private, immutable binding between normalized originals and masked SAM inputs."""
from __future__ import annotations

import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.contracts.base import ContractModel, ObjectKey, Count
from app.models import Artifact, Observation, PipelineRun
from app.pipeline.media import FrameManifest

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MaskedInput(ContractModel):
    kind: Literal["image", "video"]
    source_key: ObjectKey
    source_sha256: Sha
    masked_key: ObjectKey
    masked_sha256: Sha
    mask_key: ObjectKey
    mask_sha256: Sha
    request_id: UUID
    provenance: Literal["synthetic", "cached", "live"]
    # Original proxy indices, including for still requests whose transport index is zero.
    frame_indices: Annotated[tuple[Count, ...], Field(min_length=1, max_length=2000)]
    timestamps_ms: Annotated[tuple[Count, ...], Field(min_length=1, max_length=2000)]


class VehicleMaskMarker(ContractModel):
    version: Literal["vehicle-mask-v1"] = "vehicle-mask-v1"
    state: Literal["ready"] = "ready"
    input_hash: Sha
    config_hash: Sha
    source_manifest_sha256: Sha
    source_manifest: dict[str, object]
    media: Annotated[tuple[MaskedInput, ...], Field(min_length=1, max_length=32)]


def load_vehicle_mask_marker(db: DBSession, run: PipelineRun) -> VehicleMaskMarker:
    """Check all durable bindings before releasing or consuming damage work.

    Object bytes are separately hash-checked immediately before provider dispatch;
    this function deliberately performs no external I/O in a DB transaction.
    """
    from app.pipeline.orchestration import observations

    markers = observations(db, run.id, "vehicle_mask")
    inputs = observations(db, run.id, "assess_input")
    if len(markers) != 1 or len(inputs) != 1 or any(
        row.session_id != run.session_id for row in (*markers, *inputs)
    ):
        raise ValueError("missing or foreign vehicle mask binding")
    marker = VehicleMaskMarker.model_validate(markers[0].value)
    facts = inputs[0].value
    try:
        manifest = FrameManifest.from_json(json.dumps(marker.source_manifest))
        integers = (manifest.width, manifest.height, manifest.time_base_num, manifest.time_base_den, *manifest.pts)
        if (any(type(value) is not int for value in integers) or min(manifest.width, manifest.height) <= 0
                or max(manifest.width, manifest.height) > 3840 or manifest.pts[0] != 0):
            raise ValueError("invalid vehicle source geometry")
        if (marker.input_hash != run.input_hash or marker.config_hash != run.config_hash
                or marker.source_manifest_sha256 != facts["manifest_sha256"]
                or manifest.content_hash != marker.source_manifest_sha256
                or manifest.proxy_sha256 != facts["proxy_sha256"]):
            raise ValueError("vehicle mask input identity changed")
        expected = ({facts["proxy_key"]: (facts["proxy_sha256"], tuple(range(manifest.frame_count)))}
                    if marker.media[0].kind == "video" else
                    {row["key"]: (row["sha256"], (row["frame_index"],)) for row in facts["frames"]})
        # The media mode is fixed by the part's configured capture plan, never by the marker.
        from app.pipeline.orchestration import load_pipeline_config, canonical_hash
        from app.models import Part
        part = db.get(Part, run.part_id)
        if part is None or part.session_id != run.session_id or run.input_hash != canonical_hash({
            "raw_sha256": part.raw_sha256, "proxy": facts["proxy_sha256"], "manifest": facts["manifest_sha256"]
        }):
            raise ValueError("vehicle mask assessment input changed")
        mode = load_pipeline_config().prompts.part(part.part_no).media if part else None
        if (mode not in ("video", "stills") or not expected
                or any(item.kind != ("video" if mode == "video" else "image") for item in marker.media)
                or len(marker.media) != len(expected)
                or {item.source_key for item in marker.media} != set(expected)):
            raise ValueError("vehicle mask media membership changed")
        refs = {facts["manifest_key"]: (facts["manifest_sha256"], "derivative"),
                facts["proxy_key"]: (facts["proxy_sha256"], "proxy")}
        prefix = f"media/{run.session_id}/{run.part_id}/{run.id}/vehicle/"
        used: set[str] = set()
        for item in marker.media:
            digest, indices = expected[item.source_key]
            times = tuple(round(manifest.time_seconds(i) * 1000) for i in indices)
            if (item.source_sha256 != digest or item.frame_indices != indices or item.timestamps_ms != times
                    or any(i < 0 or i >= manifest.frame_count for i in indices)):
                raise ValueError("vehicle mask frame binding changed")
            for key in (item.masked_key, item.mask_key):
                if not key.startswith(prefix) or key in used or ".." in key.split("/"):
                    raise ValueError("vehicle mask artifact ownership changed")
                used.add(key)
            refs[item.source_key] = (item.source_sha256, "proxy" if item.kind == "video" else "frame")
            refs[item.masked_key] = (item.masked_sha256, "proxy" if item.kind == "video" else "frame")
            refs[item.mask_key] = (item.mask_sha256, "mask")
        rows = list(db.scalars(select(Artifact).where(Artifact.session_id == run.session_id, Artifact.key.in_(refs))))
        if len(rows) != len(refs) or any(refs[row.key] != (row.sha256, row.kind) or row.purge_state != "retained" for row in rows):
            raise ValueError("vehicle mask artifact missing or changed")
        if any(not key.startswith(f"media/{run.session_id}/{run.part_id}/") for key in refs):
            raise ValueError("vehicle mask original ownership changed")
    except (KeyError, TypeError, IndexError, AttributeError) as error:
        raise ValueError("invalid vehicle mask binding") from error
    return marker
