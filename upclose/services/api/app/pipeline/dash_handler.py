"""Lease-fenced dashboard reading; provider and storage work runs outside transactions."""
from __future__ import annotations

import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, NAMESPACE_URL, uuid5

from PIL import Image
from sqlalchemy import select

from app.adapters.dash_reader import build_reader
from app.adapters.meta import FrameRef, MediaRef, ProviderFailure
from app.adapters.session_storage import fenced_storage
from app.contracts.enums import PartState, SessionState
from app.contracts.mechanical import FrozenDash
from app.domain.dash_lights import load_dash_config
from app.jobs.queue import Failure
from app.jobs.worker import Publication, StageFailure
from app.models import Artifact, EvidenceSource, Job, Part, PipelineRun, Session
from app.pipeline.dash_reading import DashStill, persist_crop, run_dash_reading
from app.pipeline.dash_store import frozen_from_assessment, store_dash
from app.pipeline.orchestration import ASSESS_INPUT, advance_part, observations
from app.pipeline.sam_dispatch import run_sam_request
from app.services.budgets import (PRICE_VERSION, BudgetError, ReservationRequest, UsageUnits,
                                  documented_prices, price_usage)


class DashHandler:
    def __init__(self, factory, storage, settings, ctx, *, segmenter_factory=None, budget_service=None,
                 reader=None):
        self.factory, self.storage = factory, fenced_storage(factory, storage)
        self.settings, self.ctx, self.budget = settings, ctx, budget_service
        self.segmenter_factory, self.reader = segmenter_factory, reader

    def __call__(self, lease):
        try:
            run_id = UUID(lease.subject_id)
        except ValueError:
            raise StageFailure(Failure("INVALID_INPUT", retryable=False)) from None
        with self.factory() as db:
            run = db.get(PipelineRun, run_id)
            part = None if run is None else db.get(Part, run.part_id)
            owner = db.get(Session, lease.session_id)
            if (run is None or part is None or owner is None or part.part_no != 5
                    or run.session_id != lease.session_id or run.subject_key != f"assess:{part.id}"
                    or owner.state not in (SessionState.CAPTURING, SessionState.FINALIZED)):
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            if part.state != PartState.PROCESSING:
                return Publication()
            markers = observations(db, run.id, ASSESS_INPUT)
            if len(markers) != 1:
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            facts, session_id, part_id = dict(markers[0].value), run.session_id, part.id
            job = db.get(Job, lease.job_id)
            if (job is None or run.input_hash != job.input_hash or run.config_hash != job.config_hash
                    or any(run.config_hashes.get(k) != v for k, v in self.ctx.config_hashes.items())):
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            refs = {frame["key"]: frame["sha256"] for frame in facts["frames"]}
            artifacts = list(db.scalars(select(Artifact).where(Artifact.session_id == session_id,
                                                               Artifact.key.in_(refs))))
            if (len(artifacts) != len(refs) or any(refs[row.key] != row.sha256 for row in artifacts)
                    or any(not key.startswith(f"media/{session_id}/{part.id}/") for key in refs)):
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            config_hash = run.config_hashes.get("dash")
            source = db.scalar(select(EvidenceSource).where(EvidenceSource.session_id == session_id,
                                                             EvidenceSource.kind == "obd_snapshot"))
            obd_mil = None if source is None else source.summary.get("mil_on")
        config = load_dash_config()
        if config_hash != config.content_hash:
            return self._finish(lease, part_id, run_id, FrozenDash(state="not_readable",
                reason="dash_config_mismatch", config_hash=config_hash))
        try:
            result, crop_key = self._read(lease, facts, run_id, obd_mil, config)
        except ProviderFailure as error:
            if error.retryable and lease.attempt < 3:
                raise StageFailure(Failure("PROVIDER_TIMEOUT")) from None
            return self._finish(lease, part_id, run_id, FrozenDash(state="not_readable",
                reason=f"dash_{error.code}"[:120], config_hash=config.content_hash))
        crop = result.selection.crop
        frozen = frozen_from_assessment(result.assessment, None if crop is None else crop.sha256)
        frozen = frozen.model_copy(update={"provenance": (result.reader.provenance if result.reader else
            "live" if self.settings.model_mode == "live" else "synthetic")})
        return self._finish(lease, part_id, run_id, frozen, crop_key)

    def _read(self, lease, facts, run_id, obd_mil, config):
        from app.pipeline.vehicle_mask import SyntheticVehicleSegmenter
        # Synthetic fixture localization; it proves wiring, never perception quality.
        segmenter = (self.segmenter_factory(5) if self.segmenter_factory else
            SyntheticVehicleSegmenter())
        reader = self.reader or build_reader(os.environ if self.settings.model_mode == "live" else {})
        attempt_id = uuid5(NAMESPACE_URL, f"budget:{lease.job_id}:{lease.attempt}:dash-reader")
        dispatched = False

        def grant(bound):
            nonlocal dispatched
            if self.budget is None:
                return False
            self.budget.reserve(ReservationRequest(lease.session_id, lease.job_id, attempt_id, lease.token,
                lease.deadline, "muse-spark-1.3", PRICE_VERSION, UsageUnits(input_tokens=bound, output_tokens=8192)))
            dispatched = self.budget.mark_dispatched(attempt_id, lease.token)
            return dispatched

        try:
            with TemporaryDirectory(prefix="upclose-dash-") as scratch:
                stills = []
                for index, frame in enumerate(facts["frames"][:5]):
                    path = Path(scratch) / f"still-{index}"
                    downloaded = self.storage.download_to(frame["key"], path, 16 * 1024 * 1024)
                    if downloaded.sha256 != frame["sha256"]:
                        raise ProviderFailure("malformed_record")
                    data = path.read_bytes()
                    with Image.open(io.BytesIO(data)) as image:
                        width, height = image.size
                    media = MediaRef(media_key=frame["key"], content_sha256=frame["sha256"], kind="image",
                        stored_width=width, stored_height=height,
                        frames=(FrameRef(frame_index=0, timestamp_ms=0, frame_key=frame["key"]),))
                    stills.append(DashStill(media, data))
                result = run_dash_reading(stills, lambda media, prompt, request: run_sam_request(
                    lease, segmenter, media, request, prompt, self.budget), reader, config,
                    obd_mil_on=obd_mil, grant=grant)
        except BaseException:
            if dispatched:
                self.budget.mark_unknown(attempt_id)
            raise
        if dispatched:
            output = result.reader
            if output is None or output.input_tokens is None or output.output_tokens is None:
                self.budget.mark_unknown(attempt_id)
            else:
                units = UsageUnits(input_tokens=output.input_tokens, output_tokens=output.output_tokens)
                try:
                    self.budget.settle(attempt_id, actual_units=units,
                        actual_micro_usd=price_usage("muse-spark-1.3", units, documented_prices()),
                        outcome="succeeded", evidence_code="meta-usage-reported")
                except BudgetError:
                    pass  # unresolved billing remains reserved
        crop = result.selection.crop
        key = None
        if crop is not None:
            key = f"media/{lease.session_id}/dash/{run_id}/a{lease.attempt}/crop.png"
            persist_crop(self.storage, key, crop)
        return result, key

    def _finish(self, lease, part_id, run_id, dash, crop_key=None):
        def publish(db):
            run, part = db.get(PipelineRun, run_id), db.get(Part, part_id)
            from app.pipeline.dash_store import DASH_ASSESSMENT
            if (run is None or part is None or part.state != PartState.PROCESSING
                    or observations(db, run_id, DASH_ASSESSMENT)):
                return
            if crop_key is not None:
                db.add(Artifact(session_id=lease.session_id, kind="frame", key=crop_key,
                    sha256=dash.crop_sha256, privacy_state="pending", purge_state="retained"))
            db.flush()
            store_dash(db, lease.session_id, run_id, dash, crop_key)
            advance_part(db, self.ctx, part_id, lease.job_id)
        return Publication(publish=publish)
