"""Serialize private storage writes against purge without holding a DB transaction over I/O.

Writers hold a shared PostgreSQL session advisory lock and read the tombstone in
autocommit mode. Purge holds its exclusive counterpart after marking deleting.
It drains writes already in flight; any later writer observes the tombstone and
cannot recreate an orphan. Session rows must be retained after content deletion.
"""
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.models import Session
from .storage import DownloadedObject, MultipartObject, ObjectStorage, SignedPiece, StorageError, StoredPiece


def media_lock_id(session_id: UUID) -> int:
    return int.from_bytes(hashlib.sha256(b"upclose-media-v1:" + session_id.bytes).digest()[:8], "big", signed=True)


@contextmanager
def media_guard(factory: sessionmaker[DBSession], session_id: UUID, *, exclusive: bool = False) -> Iterator[None]:
    # This is a dedicated AUTOCOMMIT connection, not the claim/publication connection.
    lock = "pg_advisory_lock" if exclusive else "pg_advisory_lock_shared"
    unlock = "pg_advisory_unlock" if exclusive else "pg_advisory_unlock_shared"
    with factory() as db:
        conn = db.connection(execution_options={"isolation_level": "AUTOCOMMIT"})
        acquired = False
        try:
            conn.execute(text("SET lock_timeout = '5s'"))
            conn.execute(text(f"SELECT {lock}(:key)"), {"key": media_lock_id(session_id)})
            acquired = True
            if not exclusive:
                state = conn.execute(select(Session.state).where(Session.id == session_id)).scalar_one_or_none()
                if state not in ("capturing", "finalized"):
                    raise StorageError("SessionDeleted", definitive=True)
            yield
            if not exclusive:
                # The external request may outlive this database connection.
                # A postcheck must use this SAME connection: reconnecting would
                # conceal loss of its session advisory lock.
                state = conn.execute(select(Session.state).where(Session.id == session_id)).scalar_one_or_none()
                if state not in ("capturing", "finalized"):
                    raise StorageError("SessionDeleted", definitive=True)
        except SQLAlchemyError:
            raise StorageError("MediaGuardLost" if acquired else "MediaGuardBusy") from None
        finally:
            try:
                if acquired and conn.invalidated:
                    raise StorageError("MediaGuardLost")
                if acquired:
                    released = conn.execute(text(f"SELECT {unlock}(:key)"), {"key": media_lock_id(session_id)}).scalar_one()
                    if not released:
                        conn.invalidate()
                        raise StorageError("MediaGuardLost")
                conn.execute(text("RESET lock_timeout"))
            except SQLAlchemyError:
                # A pooled connection must never retain a session advisory lock.
                conn.invalidate()
                if acquired:
                    raise StorageError("MediaGuardLost") from None


def key_session(key: str) -> UUID:
    pieces = key.split("/")
    if len(pieces) < 3 or pieces[0] not in {"raw", "media", "demo"} or ".." in pieces:
        raise StorageError("InvalidSessionKey", definitive=True)
    try:
        return UUID(pieces[1])
    except ValueError:
        raise StorageError("InvalidSessionKey", definitive=True) from None


class FencedStorage:
    def __init__(self, factory: sessionmaker[DBSession], inner: ObjectStorage) -> None:
        self.factory, self.inner = factory, inner

    def create_multipart(self, key: str, content_type: str) -> str:
        started = False
        upload_id = None
        try:
            with media_guard(self.factory, key_session(key)):
                started = True
                upload_id = self.inner.create_multipart(key, content_type)
            return upload_id
        except StorageError as error:
            if started and error.code in ("SessionDeleted", "MediaGuardLost"):
                self._cleanup_deleted(key, upload_id, remove_object=False)
            raise

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        started = False
        try:
            with media_guard(self.factory, key_session(key)):
                started = True
                self.inner.put_file(key, path, content_type)
        except StorageError as error:
            if started and error.code in ("SessionDeleted", "MediaGuardLost"):
                self._cleanup_deleted(key, None, remove_object=True)
            raise

    def complete(self, key: str, provider_upload_id: str, pieces: tuple[StoredPiece, ...]) -> None:
        started = False
        try:
            with media_guard(self.factory, key_session(key)):
                started = True
                self.inner.complete(key, provider_upload_id, pieces)
        except StorageError as error:
            if started and error.code in ("SessionDeleted", "MediaGuardLost"):
                self._cleanup_deleted(key, provider_upload_id, remove_object=True)
            raise

    def _cleanup_deleted(self, key: str, upload_id: str | None, *, remove_object: bool) -> None:
        """Best effort only; tombstone audit recovers failed/unknown late effects.

        Do not delete previously accepted objects on a pre-I/O rejection or a
        transient error against a still-live session.
        """
        try:
            with self.factory() as db:
                state = db.scalar(select(Session.state).where(Session.id == key_session(key)))
            if state not in ("deleting", "deleted"):
                return
            if upload_id is not None:
                try:
                    self.inner.abort(key, upload_id)
                except StorageError:
                    pass
            if remove_object:
                self.inner.delete_object(key)
        except (DBAPIError, StorageError):
            pass

    def presign_piece(self, key: str, provider_upload_id: str, part_number: int,
                      content_length: int, expires_in: int) -> SignedPiece:
        return self.inner.presign_piece(key, provider_upload_id, part_number, content_length, expires_in)

    def list_pieces(self, key: str, provider_upload_id: str) -> tuple[StoredPiece, ...]:
        return self.inner.list_pieces(key, provider_upload_id)

    def abort(self, key: str, provider_upload_id: str) -> None:
        self.inner.abort(key, provider_upload_id)

    def head_size(self, key: str) -> int | None:
        return self.inner.head_size(key)

    def delete_object(self, key: str) -> None:
        self.inner.delete_object(key)

    def download_to(self, key: str, path: Path, max_bytes: int) -> DownloadedObject:
        return self.inner.download_to(key, path, max_bytes)

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        return self.inner.list_keys(prefix)

    def list_multipart(self, prefix: str) -> tuple[MultipartObject, ...]:
        return self.inner.list_multipart(prefix)

    def presign_read(self, key: str, expires_in: int = 60) -> str:
        return self.inner.presign_read(key, expires_in)


def fenced_storage(factory: sessionmaker[DBSession], storage: ObjectStorage) -> FencedStorage:
    return storage if isinstance(storage, FencedStorage) else FencedStorage(factory, storage)
