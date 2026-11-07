"""Private object storage behind a typed interface; only this module imports boto3.

Two clients on purpose: the internal endpoint serves every API call, while the public
endpoint is used only to presign UploadPart URLs. A presigned hostname is part of its
signature, so nothing is ever rewritten after signing.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.settings import Settings

# Bounded explicitly instead of inheriting botocore's default retry policy.
_CLIENT_CONFIG = dict(signature_version="s3v4", s3={"addressing_style": "path"},
                      retries={"total_max_attempts": 2, "mode": "standard"},
                      connect_timeout=3, read_timeout=15)
_DEFINITIVE_REJECTIONS = {"InvalidPart", "InvalidPartOrder", "EntityTooSmall", "EntityTooLarge",
                          "MalformedXML", "InvalidRequest"}
_LIST_PAGE_SIZE = 1000
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class StorageError(Exception):
    """Never carries provider payloads; `code` is the provider error code when known."""

    def __init__(self, code: str, *, definitive: bool = False):
        super().__init__(code)
        self.code = code
        self.definitive = definitive


class NoSuchUpload(StorageError):
    def __init__(self) -> None:
        super().__init__("NoSuchUpload", definitive=True)


@dataclass(frozen=True)
class StoredPiece:
    """ETag is the provider's opaque piece identity, not a file SHA-256."""
    part_number: int
    etag: str
    size_bytes: int


@dataclass(frozen=True)
class SignedPiece:
    url: str
    headers: dict[str, str]


@dataclass(frozen=True)
class DownloadedObject:
    """What was actually written to disk: authoritative size and SHA-256 of the bytes."""
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class MultipartObject:
    key: str
    provider_upload_id: str


class ObjectStorage(Protocol):
    def create_multipart(self, key: str, content_type: str) -> str: ...
    def presign_piece(self, key: str, provider_upload_id: str, part_number: int,
                      content_length: int, expires_in: int) -> SignedPiece: ...
    def list_pieces(self, key: str, provider_upload_id: str) -> tuple[StoredPiece, ...]: ...
    def complete(self, key: str, provider_upload_id: str, pieces: tuple[StoredPiece, ...]) -> None: ...
    def abort(self, key: str, provider_upload_id: str) -> None: ...
    def head_size(self, key: str) -> int | None: ...
    def delete_object(self, key: str) -> None: ...
    def download_to(self, key: str, path: Path, max_bytes: int) -> DownloadedObject: ...
    def put_file(self, key: str, path: Path, content_type: str) -> None: ...
    def list_keys(self, prefix: str) -> tuple[str, ...]: ...
    def list_multipart(self, prefix: str) -> tuple[MultipartObject, ...]: ...
    def presign_read(self, key: str, expires_in: int = 60) -> str: ...


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", "Unknown"))


class S3Storage:
    """Clients are built on first use so an invalid endpoint cannot stop the app from booting;
    /ready reports configuration problems and storage routes fail closed with StorageError."""

    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self._settings = settings
        self._clients: dict[str, object] = {}

    def _client(self, name: str, endpoint: str):  # type: ignore[no-untyped-def]
        if name not in self._clients:
            settings = self._settings
            try:
                self._clients[name] = boto3.client("s3", endpoint_url=endpoint,
                    region_name=settings.aws_default_region,
                    aws_access_key_id=settings.aws_access_key_id.get_secret_value(),
                    aws_secret_access_key=settings.aws_secret_access_key.get_secret_value(),
                    config=Config(**_CLIENT_CONFIG))
            except (ValueError, BotoCoreError):
                raise StorageError("InvalidConfiguration") from None  # endpoint text stays out of errors
        return self._clients[name]

    @property
    def internal(self):  # type: ignore[no-untyped-def]
        return self._client("internal", self._settings.s3_internal_endpoint)

    @property
    def public(self):  # type: ignore[no-untyped-def]
        return self._client("public", self._settings.s3_public_endpoint)

    def create_multipart(self, key: str, content_type: str) -> str:
        response = self._call(lambda: self.internal.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type))
        upload_id = response.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise StorageError("MissingUploadId")
        return upload_id

    def presign_piece(self, key: str, provider_upload_id: str, part_number: int,
                      content_length: int, expires_in: int) -> SignedPiece:
        # Signing Content-Length bounds what a holder of the URL can store for this piece.
        try:
            url = self.public.generate_presigned_url("upload_part", ExpiresIn=expires_in,
                Params={"Bucket": self.bucket, "Key": key, "UploadId": provider_upload_id,
                        "PartNumber": part_number, "ContentLength": content_length})
        except (BotoCoreError, ClientError) as error:
            raise StorageError(type(error).__name__) from None
        return SignedPiece(url=url, headers={})

    def list_pieces(self, key: str, provider_upload_id: str) -> tuple[StoredPiece, ...]:
        pieces: list[StoredPiece] = []
        marker = 0
        while True:
            page = self._call(lambda: self.internal.list_parts(Bucket=self.bucket, Key=key,
                UploadId=provider_upload_id, MaxParts=_LIST_PAGE_SIZE, PartNumberMarker=marker))
            pieces.extend(StoredPiece(int(item["PartNumber"]), str(item["ETag"]), int(item["Size"]))
                          for item in page.get("Parts", []))
            if not page.get("IsTruncated"):
                return tuple(sorted(pieces, key=lambda piece: piece.part_number))
            next_marker = int(page.get("NextPartNumberMarker", 0))
            if next_marker <= marker:
                raise StorageError("PaginationStalled")
            marker = next_marker

    def complete(self, key: str, provider_upload_id: str, pieces: tuple[StoredPiece, ...]) -> None:
        listing = [{"PartNumber": piece.part_number, "ETag": piece.etag} for piece in pieces]
        self._call(lambda: self.internal.complete_multipart_upload(Bucket=self.bucket, Key=key,
            UploadId=provider_upload_id, MultipartUpload={"Parts": listing}))

    def abort(self, key: str, provider_upload_id: str) -> None:
        try:
            self._call(lambda: self.internal.abort_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=provider_upload_id))
        except NoSuchUpload:
            return  # Already aborted or completed: abort is idempotent.

    def head_size(self, key: str) -> int | None:
        try:
            response = self._call(lambda: self.internal.head_object(Bucket=self.bucket, Key=key))
        except StorageError as error:
            if error.code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return int(response["ContentLength"])

    def delete_object(self, key: str) -> None:
        """Private cleanup/purge operation; deleting a missing key succeeds."""
        self._call(lambda: self.internal.delete_object(Bucket=self.bucket, Key=key))

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        keys: set[str] = set()
        marker: str | None = None
        while True:
            params = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": _LIST_PAGE_SIZE}
            if marker is not None:
                params["ContinuationToken"] = marker
            page = self._call(lambda: self.internal.list_objects_v2(**params))
            keys.update(str(item["Key"]) for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                return tuple(sorted(keys))
            next_marker = page.get("NextContinuationToken")
            if not isinstance(next_marker, str) or not next_marker or next_marker == marker:
                raise StorageError("PaginationStalled")
            marker = next_marker

    def list_multipart(self, prefix: str) -> tuple[MultipartObject, ...]:
        uploads: dict[tuple[str, str], MultipartObject] = {}
        marker: tuple[str, str] | None = None
        while True:
            params = {"Bucket": self.bucket, "Prefix": prefix, "MaxUploads": _LIST_PAGE_SIZE}
            if marker is not None:
                params.update(KeyMarker=marker[0], UploadIdMarker=marker[1])
            page = self._call(lambda: self.internal.list_multipart_uploads(**params))
            for item in page.get("Uploads", []):
                upload = MultipartObject(str(item["Key"]), str(item["UploadId"]))
                uploads[(upload.key, upload.provider_upload_id)] = upload
            if not page.get("IsTruncated"):
                return tuple(uploads[key] for key in sorted(uploads))
            next_marker = (str(page.get("NextKeyMarker", "")), str(page.get("NextUploadIdMarker", "")))
            if not all(next_marker) or next_marker == marker:
                raise StorageError("PaginationStalled")
            marker = next_marker

    def presign_read(self, key: str, expires_in: int = 60) -> str:
        if not 1 <= expires_in <= 60:
            raise StorageError("InvalidReadLifetime", definitive=True)
        try:
            return self.public.generate_presigned_url("get_object", ExpiresIn=expires_in,
                Params={"Bucket": self.bucket, "Key": key, "ResponseCacheControl": "private, no-store"})
        except (BotoCoreError, ClientError) as error:
            raise StorageError(type(error).__name__) from None

    def download_to(self, key: str, path: Path, max_bytes: int) -> DownloadedObject:
        """Stream an object into `path`, hashing the bytes written; never stores more than `max_bytes`.

        Server-side only. An object larger than the bound fails definitively (`ObjectTooLarge`)
        and the partial file is removed. A missing key surfaces as `NoSuchKey`.
        """
        response = self._call(lambda: self.internal.get_object(Bucket=self.bucket, Key=key))
        body = response["Body"]
        declared = response.get("ContentLength")
        digest, size = hashlib.sha256(), 0
        try:
            if isinstance(declared, int) and declared > max_bytes:
                raise StorageError("ObjectTooLarge", definitive=True)
            with path.open("wb") as handle:
                for chunk in iter(lambda: body.read(_DOWNLOAD_CHUNK_BYTES), b""):
                    size += len(chunk)
                    if size > max_bytes:
                        raise StorageError("ObjectTooLarge", definitive=True)
                    digest.update(chunk)
                    handle.write(chunk)
        except BotoCoreError as error:
            path.unlink(missing_ok=True)
            raise StorageError(type(error).__name__) from None
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            body.close()
        return DownloadedObject(size_bytes=size, sha256=digest.hexdigest())

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        """Write a private object from a local file. Callers use per-attempt unique keys."""
        size = path.stat().st_size
        with path.open("rb") as handle:
            self._call(lambda: self.internal.put_object(Bucket=self.bucket, Key=key, Body=handle,
                ContentLength=size, ContentType=content_type))

    @staticmethod
    def _call(operation):  # type: ignore[no-untyped-def]
        try:
            return operation()
        except ClientError as error:
            code = _error_code(error)
            if code == "NoSuchUpload":
                raise NoSuchUpload() from None
            raise StorageError(code, definitive=code in _DEFINITIVE_REJECTIONS) from None
        except BotoCoreError as error:
            raise StorageError(type(error).__name__) from None
