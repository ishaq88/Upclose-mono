"""Storage purge outside publication transactions, then fenced content scrubbing.

No model-provider upload copies currently exist. A future provider upload adapter
must add its cleanup here before enabling those uploads.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.session_storage import media_guard
from app.adapters.storage import ObjectStorage, StorageError, NoSuchUpload
from app.contracts.enums import SessionState
from app.jobs.queue import Failure, Lease
from app.jobs.worker import Publication, StageFailure, StageRegistry
from app.models import Artifact, PrivacyDerivative, Session, Upload
from app.services.deletion import scrub_content
from app.settings import Settings


class PurgeHandler:
    def __init__(self, factory: sessionmaker[DBSession], storage: ObjectStorage) -> None:
        self.factory, self.storage = factory, storage

    def __call__(self, lease: Lease) -> Publication:
        if lease.session_id is None or lease.subject_id != str(lease.session_id):
            raise StageFailure(Failure("INVALID_INPUT", retryable=False))
        session_id = lease.session_id
        # Read-only preparation closes before waiting for the exclusive I/O guard.
        with self.factory() as db:
            owner = db.get(Session, session_id)
            if owner is None or owner.state != SessionState.DELETING:
                raise StageFailure(Failure("INVALID_INPUT", retryable=False))
            keys = set(db.scalars(select(Artifact.key).where(Artifact.session_id == session_id)))
            uploads = tuple(db.execute(select(Upload.object_key, Upload.provider_upload_id)
                .where(Upload.session_id == session_id)).all())
            keys.update(key for key, _ in uploads)
            keys.update(db.scalars(select(PrivacyDerivative.object_key).where(PrivacyDerivative.session_id == session_id)))
        prefixes = tuple(f"{kind}/{session_id}/" for kind in ("raw", "media", "demo"))
        try:
            with media_guard(self.factory, session_id, exclusive=True):
                multiparts = set(uploads)
                for prefix in prefixes:
                    self._deadline(lease)
                    keys.update(self.storage.list_keys(prefix))
                    multiparts.update((item.key, item.provider_upload_id) for item in self.storage.list_multipart(prefix))
                for key, upload_id in multiparts:
                    self._deadline(lease)
                    try:
                        self.storage.abort(key, upload_id)
                    except NoSuchUpload:
                        pass
                for key in keys:
                    self._deadline(lease)
                    self.storage.delete_object(key)
                for prefix in prefixes:
                    self._deadline(lease)
                    if self.storage.list_keys(prefix) or self.storage.list_multipart(prefix):
                        raise StorageError("PurgeIncomplete")
                for key in keys:
                    self._deadline(lease)
                    if self.storage.head_size(key) is not None:
                        raise StorageError("PurgeIncomplete")
                for key, upload_id in multiparts:
                    self._deadline(lease)
                    if any(item.key == key and item.provider_upload_id == upload_id
                           for item in self.storage.list_multipart(key)):
                        raise StorageError("PurgeIncomplete")
        except StorageError:
            raise StageFailure(Failure("TRANSIENT")) from None
        return Publication(publish=lambda db: scrub_content(db, session_id))

    @staticmethod
    def _deadline(lease: Lease) -> None:
        if datetime.now(timezone.utc) >= lease.deadline:
            raise StageFailure(Failure("TRANSIENT"))


def register_purge_handlers(registry: StageRegistry, factory: sessionmaker[DBSession],
                            storage: ObjectStorage, settings: Settings) -> PurgeHandler:
    handler = PurgeHandler(factory, storage)
    registry.register("purge", handler)
    return handler


