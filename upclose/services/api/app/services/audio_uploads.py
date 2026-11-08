"""Owner engine-audio upload lifecycle (private multipart upload, <= 10 MiB) and the evidence-source attach.

One open attempt per session. The clip is verified (size at completion, sha256 when it is attached as an
`engine_audio` source) before any derived value exists. Derivations run in the worker (`audio_ingest`).
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.session_storage import fenced_storage
from app.adapters.storage import NoSuchUpload, ObjectStorage, StorageError
from app.contracts.api import UploadUrl, UploadUrlsResponse
from app.contracts.audio import AudioAcceptedResponse, AudioUploadResponse, CreateAudioUploadRequest, MAX_AUDIO_BYTES
from app.contracts.enums import SessionState, UploadState
from app.jobs.queue import JobSpec, enqueue
from app.models import AudioUpload, Session
from app.settings import Settings
from .auth import owner_session
from .problems import conflict, not_found, validation
from .uploads import (canonical_hash, chunk_count, consume_url_refresh, incomplete, piece_size,
                      storage_unavailable, verify_pieces)


def audio_object_key(session_id: UUID, upload_id: UUID) -> str:
    return f"raw/{session_id}/audio/{upload_id}"


def audio_input_hash(upload: AudioUpload) -> str:
    return canonical_hash({"upload_id": str(upload.id), "object_key": upload.object_key,
                           "bytes": upload.expected_bytes, "sha256": upload.raw_sha256,
                           "content_type": upload.content_type})


def audio_config_hash() -> str:
    from .sources import load_sources
    return load_sources().content_hash


def queue_audio_processing(db: DBSession, upload: AudioUpload) -> None:
    upload.processing_state = "queued"
    db.flush()
    enqueue(db, JobSpec(upload.session_id, "session", str(upload.id), "audio_ingest",
                        audio_input_hash(upload), audio_config_hash()))


class AudioUploadService:
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings, storage: ObjectStorage):
        self.sessions, self.settings = sessions, settings
        self.storage = fenced_storage(sessions, storage)

    def create(self, token: str, session_id: UUID, request: CreateAudioUploadRequest) -> AudioUploadResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            self._capturing(owner)
            if db.scalar(select(AudioUpload.id).where(AudioUpload.session_id == owner.id,
                                                      AudioUpload.state == UploadState.OPEN).limit(1)):
                raise conflict()
            upload_id = uuid4()
            key = audio_object_key(owner.id, upload_id)
            try:
                provider_id = self.storage.create_multipart(key, request.content_type)
            except StorageError:
                raise storage_unavailable() from None
            try:
                db.add(AudioUpload(id=upload_id, session_id=owner.id, object_key=key, provider_upload_id=provider_id,
                                   expected_bytes=request.content_length, content_type=request.content_type))
                db.flush()
            except Exception:
                try:
                    self.storage.abort(key, provider_id)
                except StorageError:
                    pass
                raise
            return AudioUploadResponse(upload_id=upload_id, chunk_count=chunk_count(request.content_length))

    def urls(self, token: str, upload_id: UUID, part_numbers: tuple[int, ...]) -> UploadUrlsResponse:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state != UploadState.OPEN:
                raise conflict()
            if any(number < 1 or number > chunk_count(upload.expected_bytes) for number in part_numbers):
                raise validation()
            consume_url_refresh(db, owner.id, self.settings.upload_url_refresh_limit_per_minute)
            ttl = self.settings.upload_url_ttl_seconds
            expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
            try:
                signed = [(number, self.storage.presign_piece(upload.object_key, upload.provider_upload_id, number,
                                                              piece_size(upload.expected_bytes, number), ttl))
                          for number in part_numbers]
            except StorageError:
                raise storage_unavailable() from None
            return UploadUrlsResponse(items=tuple(UploadUrl(part_number=number, url=piece.url, expires_at=expires,
                                                            headers=piece.headers) for number, piece in signed))

    def complete(self, token: str, upload_id: UUID) -> AudioAcceptedResponse:
        with self.sessions() as db:
            owner = owner_session(db, token)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state == UploadState.ACCEPTED:
                return AudioAcceptedResponse(upload_id=upload.id)
            if upload.state != UploadState.OPEN:
                raise conflict()
            key, provider_id, expected = upload.object_key, upload.provider_upload_id, upload.expected_bytes
        try:
            try:
                pieces = self.storage.list_pieces(key, provider_id)
            except NoSuchUpload:
                pieces = None  # completed earlier (lost response): fall through to the size check
            if pieces is not None:
                verify_pieces(expected, pieces)
                try:
                    self.storage.complete(key, provider_id, pieces)
                except StorageError as error:
                    if error.definitive and not isinstance(error, NoSuchUpload):
                        raise incomplete("Object storage rejected the uploaded pieces.") from None
                    if not isinstance(error, NoSuchUpload):
                        raise storage_unavailable() from None
            actual = self.storage.head_size(key)
        except StorageError:
            raise storage_unavailable() from None
        if actual != expected:
            raise incomplete("The completed object does not match the declared size.")
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state == UploadState.OPEN:
                upload.accepted_at = db.scalar(select(func.clock_timestamp()))
                upload.state = UploadState.ACCEPTED
            elif upload.state != UploadState.ACCEPTED:
                raise conflict()
            return AudioAcceptedResponse(upload_id=upload.id)

    def abort(self, token: str, upload_id: UUID) -> None:
        with self.sessions.begin() as db:
            owner = owner_session(db, token, lock=True)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state == UploadState.ABORTED:
                return
            if upload.state != UploadState.OPEN:
                raise conflict()
            try:
                self.storage.abort(upload.object_key, upload.provider_upload_id)
            except StorageError:
                raise storage_unavailable() from None
            upload.state = UploadState.ABORTED

    def verify_clip(self, token: str, session_id: UUID, upload_id: UUID, declared_sha256: str) -> None:
        """Size and sha256 of the stored object against the upload and the client's claim; never trusts the client.

        Runs outside any lock. A mismatch retires the upload (its object is deleted) so a corrected clip can be sent."""
        with self.sessions() as db:
            owner = owner_session(db, token, session_id)
            self._capturing(owner)
            upload = self._upload(db, owner, upload_id)
            if upload.state != UploadState.ACCEPTED:
                raise conflict()
            key, expected = upload.object_key, upload.expected_bytes
        with tempfile.TemporaryDirectory(prefix="upclose-audio-") as scratch:
            try:
                downloaded = self.storage.download_to(key, Path(scratch) / "clip.bin", MAX_AUDIO_BYTES)
            except StorageError:
                raise storage_unavailable() from None
        if downloaded.size_bytes == expected and downloaded.sha256 == declared_sha256:
            return
        with self.sessions.begin() as db:
            owner = owner_session(db, token, session_id, lock=True)
            upload = self._upload(db, owner, upload_id)
            if upload.state == UploadState.ACCEPTED:
                upload.state, upload.reason = UploadState.ABORTED, "AUDIO_HASH_MISMATCH"
        try:
            self.storage.delete_object(key)
        except StorageError:
            pass  # purge removes anything left under the session prefix
        raise validation()

    @staticmethod
    def _upload(db: DBSession, owner: Session, upload_id: UUID) -> AudioUpload:
        upload = db.scalar(select(AudioUpload).where(AudioUpload.id == upload_id, AudioUpload.session_id == owner.id))
        if upload is None:
            raise not_found()
        return upload

    @staticmethod
    def _capturing(owner: Session) -> None:
        if owner.state != SessionState.CAPTURING:
            raise conflict()
