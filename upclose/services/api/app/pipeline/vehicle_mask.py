"""Group A: publish private, exact-frame masked inputs before any damage work."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.meta import (FrameRef, MaskRaster, MediaRef, ProviderFailure,
                               ProviderUsage, SegmentObservation, SegmentResult)
from app.adapters.session_storage import fenced_storage
from app.adapters.storage import ObjectStorage, StorageError
from app.contracts.enums import PartState, PrivacyState, SessionState
from app.contracts.evidence import BoundingBox
from app.jobs.queue import Failure, Lease
from app.jobs.worker import Publication, StageFailure
from app.models import Artifact, Observation, Part, PipelineRun, Session
from app.pipeline.ingest import PublicationRejected, scratch_root_for
from app.pipeline.media import FrameManifest, MediaError, load_media_config, scratch_dir, sha256_file
from app.pipeline.orchestration import (OrchestrationContext, fail_vehicle_mask, observations,
                                        release_masked_segments)
from app.pipeline.sam_dispatch import run_sam_request
from app.pipeline.segment import SegmenterFactory, SegmentHandler
from app.pipeline.vehicle_mask_contract import MaskedInput, VehicleMaskMarker, load_vehicle_mask_marker
from app.pipeline.vehicle_media import mask_vehicle_media
from app.services.budgets import BudgetService
from app.settings import Settings

MAX_MASK_ARCHIVE_BYTES = 24 * 1024 * 1024


class SyntheticVehicleSegmenter:
    """Central rectangular synthetic vehicle; never a perception result."""

    def segment(self, media_ref: MediaRef, prompt_id: str, request_id: UUID) -> SegmentResult:
        width, height = media_ref.display_size
        left, top = width // 10, height // 10
        right, bottom = width - left, height - top
        mask = MaskRaster(width=right-left, height=bottom-top, pixels=b"\x01" * ((right-left)*(bottom-top)))
        bbox = BoundingBox(x=left/width, y=top/height, w=(right-left)/width, h=(bottom-top)/height)
        return SegmentResult(request_id=request_id, prompt_id=prompt_id, provenance="synthetic",
            observations=tuple(SegmentObservation(track_id="1", frame_index=f.frame_index, frame_key=f.frame_key,
                timestamp_ms=f.timestamp_ms, frame_width=width, frame_height=height,
                bbox=bbox, mask=mask, confidence=None) for f in media_ref.frames), usage=ProviderUsage())


def synthetic_vehicle_segmenter_factory(part_no: int) -> SyntheticVehicleSegmenter:
    return SyntheticVehicleSegmenter()


@dataclass(frozen=True)
class MaskPlan:
    session_id: UUID
    part_id: UUID
    part_no: int
    run_id: UUID
    input_hash: str
    config_hash: str
    mode: str
    facts: dict


def original_media(plan: MaskPlan, manifest: FrameManifest) -> tuple[tuple[MediaRef, str, tuple[int, ...]], ...]:
    if plan.mode == "video":
        indices = tuple(range(manifest.frame_count))
        return ((MediaRef(media_key=plan.facts["proxy_key"], content_sha256=plan.facts["proxy_sha256"], kind="video", stored_width=manifest.width,
            stored_height=manifest.height, frames=tuple(FrameRef(frame_index=i,
                timestamp_ms=round(manifest.time_seconds(i)*1000), frame_key=f"pending/frame_{i}") for i in indices)),
            plan.facts["proxy_sha256"], indices),)
    return tuple((MediaRef(media_key=still["key"], content_sha256=still["sha256"], kind="image", stored_width=manifest.width,
        stored_height=manifest.height, frames=(FrameRef(frame_index=0, timestamp_ms=0, frame_key=still["key"]),)),
        still["sha256"], (still["frame_index"],)) for still in plan.facts["frames"])


def persist_mask_response(path: Path, result: SegmentResult) -> None:
    """Bounded compressed normalized response; no unredacted provider payload is saved."""
    records = []
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for index, item in enumerate(result.observations):
            name = f"mask-{index}.bin"
            archive.writestr(name, item.mask.pixels)
            records.append({**item.model_dump(mode="json", exclude={"mask"}),
                            "mask": {"width": item.mask.width, "height": item.mask.height, "file": name}})
            if path.stat().st_size > MAX_MASK_ARCHIVE_BYTES:
                raise ValueError("normalized mask archive exceeds bound")
        metadata = result.model_dump(mode="json", exclude={"observations"})
        archive.writestr("normalized.json", json.dumps({**metadata, "observations": records}, separators=(",", ":")))
    if path.stat().st_size > MAX_MASK_ARCHIVE_BYTES:
        raise ValueError("normalized mask archive exceeds bound")


class VehicleMaskHandler:
    def __init__(self, factory: sessionmaker[DBSession], storage: ObjectStorage, settings: Settings,
                 ctx: OrchestrationContext, *, segmenter_factory: SegmenterFactory = synthetic_vehicle_segmenter_factory,
                 budget_service: BudgetService | None = None) -> None:
        self.factory, self.storage = factory, fenced_storage(factory, storage)
        self.settings, self.ctx = settings, ctx
        self.segmenter_factory, self.budget = segmenter_factory, budget_service
        self.media_config = load_media_config()

    def _load(self, lease: Lease) -> MaskPlan | None:
        with self.factory() as db:
            try:
                run = db.get(PipelineRun, UUID(lease.subject_id))
            except ValueError:
                raise StageFailure(Failure("INVALID_INPUT", retryable=False)) from None
            owner = db.get(Session, lease.session_id)
            part = None if run is None else db.get(Part, run.part_id)
            if (run is None or owner is None or part is None or run.session_id != lease.session_id
                    or part.session_id != lease.session_id or run.subject_key != f"assess:{part.id}"
                    or owner.state not in (SessionState.CAPTURING, SessionState.FINALIZED)):
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            if part.state != PartState.PROCESSING:
                return None
            if observations(db, run.id, "vehicle_mask"):
                load_vehicle_mask_marker(db, run)
                return None
            markers = observations(db, run.id, "assess_input")
            if len(markers) != 1 or markers[0].session_id != run.session_id:
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            if any(run.config_hashes.get(k) != v for k, v in self.ctx.config_hashes.items()):
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            facts = markers[0].value
            refs = {facts["proxy_key"]: facts["proxy_sha256"], facts["manifest_key"]: facts["manifest_sha256"],
                    **{f["key"]: f["sha256"] for f in facts["frames"]}}
            rows = list(db.scalars(select(Artifact).where(Artifact.session_id == run.session_id, Artifact.key.in_(refs))))
            if (len(rows) != len(refs) or any(refs[row.key] != row.sha256 for row in rows)
                    or any(not key.startswith(f"media/{run.session_id}/{part.id}/") for key in refs)):
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            return MaskPlan(run.session_id, part.id, part.part_no, run.id, run.input_hash, run.config_hash,
                            self.ctx.config.prompts.part(part.part_no).media, dict(facts))

    def __call__(self, lease: Lease) -> Publication:
        plan = self._load(lease)
        if plan is None:
            return Publication()
        stored: list[str] = []
        try:
            with scratch_dir(scratch_root_for(self.settings)) as scratch:
                marker = self._prepare(lease, plan, scratch, stored)
        except Exception as error:
            for key in stored:
                try:
                    self.storage.delete_object(key)
                except StorageError:
                    pass
            terminal = isinstance(error, (ValueError, MediaError)) or (
                isinstance(error, ProviderFailure) and not error.retryable)
            if not terminal and lease.attempt < 3:
                raise StageFailure(Failure("TRANSIENT")) from None
            if isinstance(error, ProviderFailure):
                logging.warning("Vehicle mask failed: %s code=%s http_status=%s http_status_class=%s", type(error).__name__, error.code, error.http_status, error.http_status_class)
            else:
                logging.warning("Vehicle mask failed: %s", type(error).__name__)
            return self._failure(plan, lease.job_id)

        def publish(db: DBSession) -> None:
            run = db.get(PipelineRun, plan.run_id)
            part = db.get(Part, plan.part_id)
            if run is None or part is None or part.state != PartState.PROCESSING or observations(db, run.id, "vehicle_mask"):
                raise PublicationRejected("vehicle mask assessment changed")
            for item in marker.media:
                db.add(Artifact(session_id=plan.session_id, key=item.masked_key, sha256=item.masked_sha256,
                    kind="proxy" if item.kind == "video" else "frame", privacy_state=PrivacyState.PENDING))
                db.add(Artifact(session_id=plan.session_id, key=item.mask_key, sha256=item.mask_sha256,
                    kind="mask", privacy_state=PrivacyState.PENDING))
            db.add(Observation(session_id=plan.session_id, run_id=plan.run_id, kind="vehicle_mask",
                               value=marker.model_dump(mode="json")))
            db.flush()
            if not release_masked_segments(db, self.ctx, run, lease.job_id):
                raise PublicationRejected("vehicle mask binding invalid")
        return Publication(publish=publish)

    def _prepare(self, lease: Lease, plan: MaskPlan, scratch: Path, stored: list[str]) -> VehicleMaskMarker:
        manifest_path = scratch / "manifest.json"
        downloaded = self.storage.download_to(plan.facts["manifest_key"], manifest_path, 16*1024*1024)
        manifest = FrameManifest.from_json(manifest_path.read_text(encoding="utf-8"))
        if (downloaded.sha256 != plan.facts["manifest_sha256"] or manifest.content_hash != plan.facts["manifest_sha256"]
                or manifest.proxy_sha256 != plan.facts["proxy_sha256"]):
            raise ValueError("changed source manifest")
        entries = []
        for index, (media, digest, indices) in enumerate(original_media(plan, manifest)):
            SegmentHandler._check_deadline(lease)
            source = scratch / f"source-{index}"
            downloaded = self.storage.download_to(media.media_key, source, self.settings.pipeline_proxy_max_bytes)
            if downloaded.sha256 != digest:
                raise ValueError("changed source media")
            request_id = uuid5(NAMESPACE_URL, f"vehicle:{lease.job_id}:{lease.attempt}:{index}")
            result = run_sam_request(lease, self.segmenter_factory(plan.part_no), media, request_id, "vehicle", self.budget)
            if result.request_id != request_id or result.prompt_id != "vehicle" or result.limits_encountered:
                raise ValueError("vehicle response identity or completeness invalid")
            output = mask_vehicle_media(source, media, result.observations, scratch, self.media_config)
            archive = scratch / f"mask-{index}.zip"
            persist_mask_response(archive, result)
            prefix = f"media/{plan.session_id}/{plan.part_id}/{plan.run_id}/vehicle/a{lease.attempt}/"
            masked_key = prefix + f"{index}.{'mp4' if media.kind == 'video' else 'png'}"
            mask_key = prefix + f"{index}-masks.zip"
            self.storage.put_file(masked_key, output.path, "video/mp4" if media.kind == "video" else "image/png")
            stored.append(masked_key)
            self.storage.put_file(mask_key, archive, "application/zip")
            stored.append(mask_key)
            entries.append(MaskedInput(kind=media.kind, source_key=media.media_key, source_sha256=digest,
                masked_key=masked_key, masked_sha256=output.sha256, mask_key=mask_key, mask_sha256=sha256_file(archive),
                request_id=request_id, provenance=result.provenance, frame_indices=indices,
                timestamps_ms=tuple(round(manifest.time_seconds(i)*1000) for i in indices)))
            source.unlink(missing_ok=True)
            output.path.unlink(missing_ok=True)
            archive.unlink(missing_ok=True)
        return VehicleMaskMarker(input_hash=plan.input_hash, config_hash=plan.config_hash,
            source_manifest_sha256=manifest.content_hash, source_manifest=json.loads(manifest.to_canonical_json()), media=tuple(entries))

    def _failure(self, plan: MaskPlan, job_id: UUID) -> Publication:
        def publish(db: DBSession) -> None:
            run = db.get(PipelineRun, plan.run_id)
            if run is None:
                raise PublicationRejected("vehicle mask assessment missing")
            fail_vehicle_mask(db, self.ctx, run, "VEHICLE_MASK_UNAVAILABLE", job_id)
        return Publication(publish=publish)
