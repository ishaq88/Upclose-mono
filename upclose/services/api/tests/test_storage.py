"""Storage adapter behavior. Emulator tests skip cleanly when Floci is unreachable.

Observed Floci 1.6.0 behavior is recorded where it differs from AWS S3 (see the xfail test).
"""
import os
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import HttpUrl

from app.adapters.storage import NoSuchUpload, S3Storage, StorageError
from app.settings import Settings

ORIGIN = "http://localhost:5173"


def storage_settings(bucket: str, **overrides: object) -> Settings:
    values: dict[str, object] = {"s3_bucket": bucket,
        "s3_internal_endpoint": os.environ.get("S3_INTERNAL_ENDPOINT", "http://localhost:4566"),
        "s3_public_endpoint": os.environ.get("S3_PUBLIC_ENDPOINT", "http://localhost:4566")}
    return Settings(**(values | overrides))


def require_emulator(storage: S3Storage, settings: Settings) -> None:
    """Skip unless the internal API and the browser-facing (public) endpoint both answer."""
    try:
        storage.internal.create_bucket(Bucket=settings.s3_bucket)
        httpx.get(settings.s3_public_endpoint, timeout=3)
    except Exception:
        pytest.skip("Floci storage emulator is not reachable on its internal and public endpoints")


@pytest.fixture(scope="module")
def emulator():
    """A unique throwaway bucket with the same CORS rule storage-init applies."""
    settings = storage_settings(f"upclose-test-{uuid4().hex[:12]}")
    storage = S3Storage(settings)
    require_emulator(storage, settings)
    storage.internal.put_bucket_cors(Bucket=settings.s3_bucket, CORSConfiguration={"CORSRules": [{
        "AllowedOrigins": [ORIGIN], "AllowedMethods": ["PUT", "GET", "HEAD"], "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 300}]})
    yield storage
    for page in storage.internal.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
        for item in page.get("Contents", []):
            storage.internal.delete_object(Bucket=settings.s3_bucket, Key=item["Key"])
    for item in storage.internal.list_multipart_uploads(Bucket=settings.s3_bucket).get("Uploads", []):
        storage.abort(item["Key"], item["UploadId"])
    storage.internal.delete_bucket(Bucket=settings.s3_bucket)


def new_key() -> str:
    return f"raw/test/{uuid4().hex}"


def put(url: str, data: bytes, **headers: str) -> httpx.Response:
    return httpx.put(url, content=data, headers=headers, timeout=30)


def test_public_endpoint_signs_the_browser_host_and_internal_endpoint_is_separate() -> None:
    settings = Settings(s3_internal_endpoint="http://floci:4566", s3_public_endpoint="http://localhost:4566")
    storage = S3Storage(settings)
    signed = storage.presign_piece("raw/a/b/c", "upload-1", 2, 1000, 900)
    parts = urlsplit(signed.url)
    query = parse_qs(parts.query)
    assert (parts.scheme, parts.netloc) == ("http", "localhost:4566")
    assert parts.path == f"/{settings.s3_bucket}/raw/a/b/c"  # path-style addressing
    assert query["partNumber"] == ["2"] and query["uploadId"] == ["upload-1"]
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"] and query["X-Amz-Expires"] == ["900"]
    assert "content-length" in query["X-Amz-SignedHeaders"][0]
    assert storage.internal.meta.endpoint_url == "http://floci:4566"
    # The contract DTO serializes the URL; normalization must not alter a signed URL.
    assert str(HttpUrl(signed.url)) == signed.url
    assert "X-Amz-Signature" in query


def test_approved_read_signing_has_short_lifetime_and_no_store() -> None:
    storage = S3Storage(Settings(s3_public_endpoint="https://media.example.test"))
    url = urlsplit(storage.presign_read("media/synthetic/derivatives/reviewed"))
    query = parse_qs(url.query)
    assert url.netloc == "media.example.test" and url.scheme == "https"
    assert query["X-Amz-Expires"] == ["60"]
    assert query["response-cache-control"] == ["private, no-store"]
    for lifetime in (0, 61):
        with pytest.raises(StorageError) as error:
            storage.presign_read("media/synthetic/derivatives/reviewed", lifetime)
        assert error.value.code == "InvalidReadLifetime"


def test_clients_do_not_retry_hidden_and_errors_carry_no_provider_text() -> None:
    storage = S3Storage(Settings(s3_internal_endpoint="http://127.0.0.1:9", s3_public_endpoint="http://127.0.0.1:9"))
    assert storage.internal.meta.config.retries["total_max_attempts"] == 2
    with pytest.raises(StorageError) as caught:
        storage.create_multipart(new_key(), "video/mp4")
    assert "127.0.0.1" not in str(caught.value) and not caught.value.definitive


def test_invalid_endpoint_does_not_fail_construction_but_operations_fail_closed() -> None:
    storage = S3Storage(Settings(s3_internal_endpoint="invalid", s3_public_endpoint="invalid"))  # must not raise
    for call in (lambda: storage.create_multipart("k", "video/mp4"),
                 lambda: storage.presign_piece("k", "u", 1, 10, 900)):
        with pytest.raises(StorageError) as caught:
            call()
        assert caught.value.code == "InvalidConfiguration" and "invalid" not in str(caught.value).replace("Invalid", "")


def test_browser_origin_piece_upload_exposes_etag_and_completes(emulator: S3Storage) -> None:
    key, payload = new_key(), os.urandom(4096)
    upload_id = emulator.create_multipart(key, "video/mp4")
    signed = emulator.presign_piece(key, upload_id, 1, len(payload), 900)
    preflight = httpx.options(signed.url, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "PUT",
                                                    "Access-Control-Request-Headers": "content-type"}, timeout=30)
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == ORIGIN
    assert "PUT" in preflight.headers["access-control-allow-methods"]
    response = put(signed.url, payload, Origin=ORIGIN)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "etag" in response.headers["access-control-expose-headers"].lower()
    pieces = emulator.list_pieces(key, upload_id)
    assert [(p.part_number, p.size_bytes) for p in pieces] == [(1, 4096)]
    assert pieces[0].etag == response.headers["etag"]
    emulator.complete(key, upload_id, pieces)
    assert emulator.head_size(key) == 4096
    assert emulator.head_size(key + "-missing") is None


def test_listing_paginates_and_sorts_pieces(emulator: S3Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    key = new_key()
    upload_id = emulator.create_multipart(key, "video/mp4")
    for number in (3, 1, 2):
        assert put(emulator.presign_piece(key, upload_id, number, 10, 900).url, bytes([number]) * 10).status_code == 200
    monkeypatch.setattr("app.adapters.storage._LIST_PAGE_SIZE", 2)  # force two pages
    assert [p.part_number for p in emulator.list_pieces(key, upload_id)] == [1, 2, 3]
    emulator.abort(key, upload_id)


def test_abort_is_idempotent_and_list_after_abort_reports_no_such_upload(emulator: S3Storage) -> None:
    key = new_key()
    upload_id = emulator.create_multipart(key, "video/mp4")
    emulator.abort(key, upload_id)
    emulator.abort(key, upload_id)
    with pytest.raises(NoSuchUpload):
        emulator.list_pieces(key, upload_id)


def test_expired_url_is_rejected_and_a_fresh_one_for_the_same_upload_works(emulator: S3Storage) -> None:
    key = new_key()
    upload_id = emulator.create_multipart(key, "video/mp4")
    short = emulator.presign_piece(key, upload_id, 1, 10, 1)
    time.sleep(3)
    assert put(short.url, b"x" * 10).status_code == 403  # Floci enforces expiry
    assert put(emulator.presign_piece(key, upload_id, 1, 10, 900).url, b"x" * 10).status_code == 200
    emulator.abort(key, upload_id)


def test_completed_upload_rejects_an_old_piece_url_and_keeps_its_bytes(emulator: S3Storage) -> None:
    key, original = new_key(), b"A" * 64
    upload_id = emulator.create_multipart(key, "video/mp4")
    old = emulator.presign_piece(key, upload_id, 1, 64, 900)
    assert put(old.url, original).status_code == 200
    emulator.complete(key, upload_id, emulator.list_pieces(key, upload_id))
    # Observed on Floci: 404 NoSuchUpload, like S3 once the multipart upload is consumed.
    replay = put(old.url, b"B" * 64)
    assert replay.status_code in (403, 404) and "NoSuchUpload" in replay.text
    stored = emulator.internal.get_object(Bucket=emulator.bucket, Key=key)["Body"].read()
    assert stored == original


@pytest.mark.xfail(strict=True, reason="Floci 1.6.0 does not verify presigned signatures: an unsigned PUT to the "
                   "key succeeds. AWS S3 returns 403. The API never exposes PutObject authority, but this "
                   "emulator cannot prove that boundary; production storage must be verified separately.")
def test_unsigned_put_cannot_overwrite_an_accepted_key(emulator: S3Storage) -> None:
    key = new_key()
    upload_id = emulator.create_multipart(key, "video/mp4")
    signed = emulator.presign_piece(key, upload_id, 1, 10, 900)
    assert put(signed.url, b"A" * 10).status_code == 200
    emulator.complete(key, upload_id, emulator.list_pieces(key, upload_id))
    bare = put(signed.url.split("?")[0], b"B" * 50)
    assert bare.status_code in (401, 403)
    assert emulator.head_size(key) == 10


class PrefixIgnoringClient:
    """Fake boto client that, like some S3-compatible stores, ignores Prefix on both listings."""

    def __init__(self, keys: list[str], uploads: list[tuple[str, str]]) -> None:
        self.keys, self.uploads = sorted(keys), sorted(uploads)

    def list_objects_v2(self, **params: object) -> dict[str, object]:
        start = int(str(params.get("ContinuationToken", "0")))
        size = int(str(params["MaxKeys"]))
        page = self.keys[start:start + size]
        more = start + size < len(self.keys)
        return {"Contents": [{"Key": key} for key in page], "IsTruncated": more,
                **({"NextContinuationToken": str(start + size)} if more else {})}

    def list_multipart_uploads(self, **params: object) -> dict[str, object]:
        rows = self.uploads
        if "KeyMarker" in params:
            marker = (str(params["KeyMarker"]), str(params["UploadIdMarker"]))
            rows = [row for row in rows if row > marker]
        size = int(str(params["MaxUploads"]))
        page, more = rows[:size], len(rows) > size
        out: dict[str, object] = {"Uploads": [{"Key": key, "UploadId": uid} for key, uid in page],
                                  "IsTruncated": more}
        if more:
            out |= {"NextKeyMarker": page[-1][0], "NextUploadIdMarker": page[-1][1]}
        return out


def fake_storage(client: PrefixIgnoringClient) -> S3Storage:
    class FakeBackedStorage(S3Storage):
        internal = client  # type: ignore[assignment]

    return FakeBackedStorage(Settings())


def test_list_multipart_keeps_only_matching_keys_when_the_backend_ignores_prefix(
        monkeypatch: pytest.MonkeyPatch) -> None:
    uploads = [("raw/a/one", "u1"), ("raw/b/two", "u2"), ("raw/a/three", "u3"), ("media/a/x", "u4"),
               ("raw/a/one", "u5"), ("raw/ab/four", "u6"), ("raw/a/five", "u7")]
    storage = fake_storage(PrefixIgnoringClient([], uploads))
    expected = (("raw/a/five", "u7"), ("raw/a/one", "u1"), ("raw/a/one", "u5"), ("raw/a/three", "u3"))
    for page_size in (100, 2, 1):  # foreign rows straddle page boundaries when paging
        monkeypatch.setattr("app.adapters.storage._LIST_PAGE_SIZE", page_size)
        found = storage.list_multipart("raw/a/")
        assert tuple((item.key, item.provider_upload_id) for item in found) == expected
    assert storage.list_multipart("no-such-prefix/") == ()


def test_list_keys_keeps_only_matching_keys_when_the_backend_ignores_prefix(
        monkeypatch: pytest.MonkeyPatch) -> None:
    storage = fake_storage(PrefixIgnoringClient(["raw/a/1", "raw/b/2", "raw/a/3", "media/a/4"], []))
    monkeypatch.setattr("app.adapters.storage._LIST_PAGE_SIZE", 2)
    assert storage.list_keys("raw/a/") == ("raw/a/1", "raw/a/3")
    assert storage.list_keys("none/") == ()
