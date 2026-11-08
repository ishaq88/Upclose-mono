"""Lease-fenced engine-audio ingest: duration, loudness envelope and a spectrogram PNG, all in private storage.

A clip that cannot be decoded, is too long, or is silent publishes `not_assessed` with a reason, never a clean
result. Storage trouble retries; after the last attempt it also publishes `not_assessed`. A superseded or deleted
clip publishes nothing. The spectrogram key lives under the session purge prefix (`media/<session>/`).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.session_storage import fenced_storage
from app.adapters.storage import ObjectStorage, StorageError
from app.contracts.audio import MAX_AUDIO_SECONDS
from app.contracts.enums import SessionState, UploadState
from app.contracts.sources import EngineAudioSummary
from app.domain.audio import loudness
from app.jobs.queue import Failure, Lease
from app.jobs.worker import Publication, StageFailure, StageRegistry
from app.models import AudioUpload, EvidenceSource, Job, Session
from app.pipeline.ingest import scratch_root_for
from app.pipeline.media import MediaError, MediaReason, ProcessLimits, _file_url, _tool, run_tool, scratch_dir
from app.services.audio_uploads import audio_input_hash
from app.services.sources import load_sources
from app.settings import Settings

_WHITELIST = "wav,mp3,mov"  # mov is the ffmpeg demuxer for mp4/m4a
_LIMITS = ProcessLimits(wall_time_s=45, cpu_seconds=45, address_space_bytes=2 * 1024 ** 3,
                        max_file_bytes=32 * 1024 * 1024, threads=2)


@dataclass(frozen=True, slots=True)
class AudioPlan:
    upload_id: UUID
    session_id: UUID
    object_key: str
    expected_bytes: int
    expected_sha256: str
    content_type: str
    input_hash: str
    config_matches: bool


@dataclass(frozen=True, slots=True)
class AudioResult:
    state: str
    reason: str | None = None
    duration_s: float | None = None
    rms_db: float | None = None
    peak_db: float | None = None
    envelope_db: tuple[float, ...] = ()
    spectrogram_key: str | None = None
    spectrogram_sha256: str | None = None


class AudioIngestHandler:
    def __init__(self, factory: sessionmaker[DBSession], storage: ObjectStorage, settings: Settings) -> None:
        self.factory = factory
        self.storage = fenced_storage(factory, storage)
        self.settings = settings
        self.config = load_sources()

    def __call__(self, lease: Lease) -> Publication:
        plan = self._load(lease)
        if isinstance(plan, Publication):
            return plan
        if not plan.config_matches:
            return self._publish(plan, AudioResult("not_assessed", "Audio settings changed while the clip was queued."))
        written: list[str] = []
        try:
            with scratch_dir(scratch_root_for(self.settings)) as scratch:
                result = self._process(lease, plan, scratch, written)
        except StorageError:
            self._discard(written)
            return self._retry_or_fail(lease, plan, "The clip could not be read from storage.")
        except MediaError as error:
            self._discard(written)
            if error.reason.name in ("TIMEOUT", "RESOURCE_LIMIT"):
                return self._retry_or_fail(lease, plan, "The clip took too long to analyse.")
            return self._publish(plan, AudioResult("not_assessed", "The clip could not be decoded as audio."))
        except Exception:
            self._discard(written)
            return self._retry_or_fail(lease, plan, "The clip could not be analysed.")
        return self._publish(plan, result)

    def _load(self, lease: Lease) -> AudioPlan | Publication:
        invalid = StageFailure(Failure("INVALID_INPUT", retryable=False))
        try:
            upload_id = UUID(lease.subject_id)
        except ValueError:
            raise invalid from None
        if lease.stage != "audio_ingest" or lease.subject_kind != "session" or lease.session_id is None:
            raise invalid
        with self.factory() as db:
            owner = db.get(Session, lease.session_id)
            upload = db.get(AudioUpload, upload_id)
            job = db.get(Job, lease.job_id)
            if (owner is None or upload is None or upload.session_id != owner.id or job is None
                    or job.session_id != owner.id or job.subject_id != str(upload.id) or job.stage != "audio_ingest"):
                raise invalid
            if owner.state != SessionState.CAPTURING or upload.state != UploadState.ACCEPTED \
                    or upload.processing_state != "queued" or job.input_hash != audio_input_hash(upload):
                return Publication()  # superseded, finalized or already settled: nothing to do
            return AudioPlan(upload.id, owner.id, upload.object_key, upload.expected_bytes, upload.raw_sha256 or "",
                             upload.content_type, job.input_hash, job.config_hash == self.config.content_hash)

    def _process(self, lease: Lease, plan: AudioPlan, scratch: Path, written: list[str]) -> AudioResult:
        cfg = self.config.audio
        self._deadline(lease)
        clip = scratch / "clip.bin"
        downloaded = self.storage.download_to(plan.object_key, clip, plan.expected_bytes)
        if downloaded.size_bytes != plan.expected_bytes or downloaded.sha256 != plan.expected_sha256:
            return AudioResult("not_assessed", "The stored clip does not match what was uploaded.")
        probe = run_tool([*_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          "-protocol_whitelist", "file", "-format_whitelist", _WHITELIST, "-i", _file_url(clip)],
                         limits=_LIMITS, cwd=scratch)
        if probe.timed_out:
            raise MediaError(MediaReason.TIMEOUT, "probe timed out")
        try:
            info = json.loads(probe.stdout or b"{}")
            streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
            duration = float(info["format"]["duration"])
        except (ValueError, KeyError, TypeError):
            return AudioResult("not_assessed", "The clip could not be decoded as audio.")
        if probe.returncode != 0 or not streams or duration <= 0:
            return AudioResult("not_assessed", "The clip could not be decoded as audio.")
        if duration > MAX_AUDIO_SECONDS + 0.5:
            return AudioResult("not_assessed", f"The clip is longer than {MAX_AUDIO_SECONDS} seconds.", round(duration, 2))
        if duration < cfg.min_duration_s:
            return AudioResult("not_assessed", "The clip is too short to analyse.", round(duration, 2))
        self._deadline(lease)
        decoded = run_tool([*_tool("ffmpeg"), "-v", "error", "-nostdin", "-protocol_whitelist", "file",
                            "-format_whitelist", _WHITELIST, "-i", _file_url(clip), "-vn", "-map", "0:a:0",
                            "-ac", "1", "-ar", str(cfg.decode_rate_hz), "-t", str(MAX_AUDIO_SECONDS + 1),
                            "-f", "s16le", "-"], limits=_LIMITS, cwd=scratch, stdout_cap_bytes=8 * 1024 * 1024)
        if decoded.timed_out:
            raise MediaError(MediaReason.TIMEOUT, "decode timed out")
        if decoded.returncode != 0 or len(decoded.stdout) < 2 * cfg.decode_rate_hz * cfg.min_duration_s:
            return AudioResult("not_assessed", "The clip could not be decoded as audio.", round(duration, 2))
        summary = loudness(decoded.stdout, cfg.decode_rate_hz, cfg.window_ms, cfg.max_points)
        measured = round(summary.sample_count / cfg.decode_rate_hz, 2)
        if summary.rms_db < cfg.silent_rms_db:
            return AudioResult("not_assessed", "The clip is silent or nearly silent.", measured, summary.rms_db,
                               summary.peak_db, summary.envelope_db)
        self._deadline(lease)
        png = scratch / "spectrogram.png"
        shot = run_tool([*_tool("ffmpeg"), "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file",
                         "-format_whitelist", _WHITELIST, "-i", _file_url(clip), "-vn",
                         "-t", str(MAX_AUDIO_SECONDS + 1), "-filter_complex",
                         f"[0:a:0]showspectrumpic=s={cfg.spectrogram_width}x{cfg.spectrogram_height}:legend=0:scale=log[v]",
                         "-map", "[v]", "-frames:v", "1", str(png)], limits=_LIMITS, cwd=scratch)
        if shot.timed_out:
            raise MediaError(MediaReason.TIMEOUT, "spectrogram timed out")
        if shot.returncode != 0 or not png.is_file() or png.stat().st_size == 0:
            return AudioResult("not_assessed", "A spectrogram could not be drawn for this clip.", measured,
                               summary.rms_db, summary.peak_db, summary.envelope_db)
        digest = hashlib.sha256(png.read_bytes()).hexdigest()
        key = f"media/{plan.session_id}/audio/{plan.upload_id}/spectrogram.png"
        written.append(key)
        self._deadline(lease)
        self.storage.put_file(key, png, "image/png")
        return AudioResult("assessed", None, measured, summary.rms_db, summary.peak_db, summary.envelope_db, key, digest)

    def _publish(self, plan: AudioPlan, result: AudioResult) -> Publication:
        def publish(db: DBSession) -> None:
            owner = db.scalar(select(Session).where(Session.id == plan.session_id).with_for_update())
            upload = db.scalar(select(AudioUpload).where(AudioUpload.id == plan.upload_id,
                                                         AudioUpload.session_id == plan.session_id).with_for_update())
            source = db.scalar(select(EvidenceSource).where(EvidenceSource.session_id == plan.session_id,
                                                            EvidenceSource.kind == "engine_audio").with_for_update())
            if (owner is None or owner.state != SessionState.CAPTURING or upload is None or source is None
                    or upload.state != UploadState.ACCEPTED or upload.processing_state != "queued"
                    or audio_input_hash(upload) != plan.input_hash or source.data.get("upload_id") != str(upload.id)):
                return  # replaced, finalized or deleted while processing: this attempt changes nothing
            summary = EngineAudioSummary(
                config_hash=self.config.content_hash, provenance=source.provenance, state=result.state,
                reason=result.reason, size_bytes=plan.expected_bytes, content_type=plan.content_type,
                duration_s=result.duration_s, rms_db=result.rms_db, peak_db=result.peak_db,
                envelope_db=result.envelope_db, envelope_step_ms=self.config.audio.window_ms,
                spectrogram_sha256=result.spectrogram_sha256)
            source.summary = summary.model_dump(mode="json")
            upload.processing_state = "ready" if result.state == "assessed" else "failed"
            upload.reason = result.reason
            upload.spectrogram_key, upload.spectrogram_sha256 = result.spectrogram_key, result.spectrogram_sha256
            db.flush()
        return Publication(publish)

    def _retry_or_fail(self, lease: Lease, plan: AudioPlan, reason: str) -> Publication:
        if lease.attempt < 3:
            raise StageFailure(Failure("TRANSIENT"))
        return self._publish(plan, AudioResult("not_assessed", reason))

    def _discard(self, keys: list[str]) -> None:
        for key in keys:
            try:
                self.storage.delete_object(key)
            except StorageError:
                pass

    @staticmethod
    def _deadline(lease: Lease) -> None:
        if datetime.now(timezone.utc) >= lease.deadline - timedelta(seconds=5):
            raise StageFailure(Failure("TRANSIENT"))


def register_audio_handlers(registry: StageRegistry, factory: sessionmaker[DBSession], storage: ObjectStorage,
                            settings: Settings) -> AudioIngestHandler:
    handler = AudioIngestHandler(factory, storage, settings)
    registry.register("audio_ingest", handler)
    return handler
