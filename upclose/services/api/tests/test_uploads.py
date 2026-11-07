"""Multipart upload routes against isolated PostgreSQL and the Floci emulator."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import os
import subprocess
import sys
import threading
import time
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.adapters.storage import S3Storage, StorageError, StoredPiece
from app.contracts.enums import PartState, SessionState, UploadState
from app.main import create_app
from app.models import Part, RequestKey, Session, Upload
from app.models.rate_limits import RateLimitBucket
from app.services.uploads import CHUNK_SIZE_BYTES
from tests.test_storage import ORIGIN, require_emulator, storage_settings

MIB = 1024 * 1024


@pytest.fixture(scope="module")
def session_factory():
    url = os.environ.get("SESSION_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("An isolated PostgreSQL MODEL_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing upload tests outside an explicitly named upclose_test_* PostgreSQL database")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env={**os.environ, "DATABASE_URL": url})
    engine = create_engine(url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(scope="module")
def bucket():
    name = f"upclose-test-{uuid4().hex[:12]}"
    storage = S3Storage(storage_settings(name))
    require_emulator(storage, storage_settings(name))
    storage.internal.put_bucket_cors(Bucket=name, CORSConfiguration={"CORSRules": [{
        "AllowedOrigins": [ORIGIN], "AllowedMethods": ["PUT", "GET", "HEAD"], "AllowedHeaders": ["*"],
        "ExposeHeaders": ["ETag"], "MaxAgeSeconds": 300}]})
    yield name
    for page in storage.internal.get_paginator("list_objects_v2").paginate(Bucket=name):
        for item in page.get("Contents", []):
            storage.internal.delete_object(Bucket=name, Key=item["Key"])
    for item in storage.internal.list_multipart_uploads(Bucket=name).get("Uploads", []):
        storage.abort(item["Key"], item["UploadId"])
    storage.internal.delete_bucket(Bucket=name)


class Harness:
    """One app + owner per test; a unique source address avoids the daily session limit."""

    def __init__(self, session_factory, bucket: str, storage_cls=S3Storage, **settings: object):
        self.db = session_factory
        self.settings = storage_settings(bucket, **settings)
        self.storage = storage_cls(self.settings)
        self.app = create_app(self.settings, database_probe=lambda _: True, session_factory=session_factory,
                              storage=self.storage)
        self.http = self.client()
        self.session_id, self.headers = self.new_owner(self.http)

    def client(self) -> TestClient:
        address = ipaddress.IPv6Address(int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int & ((1 << 64) - 1)))
        return TestClient(self.app, client=(str(address), 12345))

    def new_owner(self, http: TestClient) -> tuple[UUID, dict[str, str]]:
        created = http.post("/api/v1/sessions", json={"purpose": "buyer", "consent": True,
                                                       "consent_version": "upclose-prototype-v1"})
        assert created.status_code == 201
        body = created.json()
        return UUID(body["session_id"]), {"Authorization": f"Bearer {body['session_token']}"}

    def other_owner(self) -> dict[str, str]:
        return self.new_owner(self.client())[1]

    def start(self, size: int, part_no: int = 1, content_type: str = "video/mp4", headers=None, **extra: str):
        return self.http.post(f"/api/v1/sessions/{self.session_id}/parts", headers=headers or self.headers | extra,
                              json={"part_no": part_no, "content_type": content_type, "content_length": size})

    def begin(self, size: int, part_no: int = 1) -> UUID:
        response = self.start(size, part_no)
        assert response.status_code == 201, response.text
        return UUID(response.json()["upload_id"])

    def urls(self, upload_id: UUID, numbers: list[int], headers=None) -> httpx.Response:
        return self.http.post(f"/api/v1/uploads/{upload_id}/urls", headers=headers or self.headers,
                              json={"part_numbers": numbers})

    def send(self, upload_id: UUID, number: int, data: bytes, **headers: str) -> httpx.Response:
        item = self.urls(upload_id, [number]).json()["items"][0]
        return httpx.put(item["url"], content=data, headers=headers, timeout=60)

    def complete(self, upload_id: UUID, headers=None) -> httpx.Response:
        return self.http.post(f"/api/v1/uploads/{upload_id}/complete", headers=headers or self.headers, json={})

    def row(self, model, record_id: UUID):
        with self.db() as db:
            return db.get(model, record_id)

    def part(self, part_no: int = 1) -> Part:
        with self.db() as db:
            return db.scalar(select(Part).where(Part.session_id == self.session_id, Part.part_no == part_no))


@pytest.fixture
def make(session_factory, bucket):
    def build(storage_cls=S3Storage, **settings: object) -> Harness:
        return Harness(session_factory, bucket, storage_cls, **settings)
    return build


def problem(response: httpx.Response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == code
    return response.json()


def test_browser_origin_upload_completes_and_commits_state(make) -> None:
    h = make()
    payload = os.urandom(5000)
    created = h.start(len(payload))
    assert created.status_code == 201
    body = created.json()
    assert body["chunk_size_bytes"] == CHUNK_SIZE_BYTES and body["chunk_count"] == 1
    upload_id, part_id = UUID(body["upload_id"]), UUID(body["part_id"])
    upload, part = h.row(Upload, upload_id), h.row(Part, part_id)
    assert upload.state == UploadState.OPEN and part.state == PartState.UPLOADING
    assert part.active_upload_id == upload_id and part.raw_key is None
    assert upload.object_key == f"raw/{h.session_id}/{part_id}/{upload_id}"
    assert upload.expected_bytes == 5000 and upload.provider_upload_id not in upload.object_key

    signed = h.urls(upload_id, [1])
    assert signed.status_code == 200
    item = signed.json()["items"][0]
    assert item["url"].startswith(h.settings.s3_public_endpoint + "/") and item["part_number"] == 1
    assert "X-Amz-Signature" in item["url"] and item["headers"] == {}
    # Browser-style PUT: Origin header present, response must permit the origin and expose ETag.
    sent = httpx.put(item["url"], content=payload, headers={"Origin": ORIGIN}, timeout=30)
    assert sent.status_code == 200
    assert sent.headers["access-control-allow-origin"] == ORIGIN
    assert "etag" in sent.headers["access-control-expose-headers"].lower()

    listing = h.http.get(f"/api/v1/uploads/{upload_id}", headers=h.headers).json()
    assert listing["state"] == "open"
    assert listing["pieces"] == [{"part_number": 1, "etag": sent.headers["etag"], "size_bytes": 5000}]

    done = h.complete(upload_id)
    assert done.status_code == 202 and done.json() == {"part_id": str(part_id), "state": "uploaded"}
    upload, part = h.row(Upload, upload_id), h.row(Part, part_id)
    assert (upload.state, upload.accepted_at is not None) == (UploadState.ACCEPTED, True)
    assert part.state == PartState.UPLOADED and part.raw_key == upload.object_key and part.active_upload_id is None
    assert h.storage.head_size(upload.object_key) == 5000
    again = h.complete(upload_id)
    assert again.status_code == 202 and again.json() == done.json()
    final = h.http.get(f"/api/v1/uploads/{upload_id}", headers=h.headers).json()
    assert final == {"upload_id": str(upload_id), "state": "accepted", "pieces": []}


def test_interrupted_piece_resumes_without_reuploading_others(make) -> None:
    h = make()
    first, tail = os.urandom(CHUNK_SIZE_BYTES), os.urandom(1000)
    upload_id = h.begin(len(first) + len(tail))
    assert h.start(10).status_code == 409  # same part still has an open attempt
    assert h.send(upload_id, 1, first).status_code == 200
    before = h.http.get(f"/api/v1/uploads/{upload_id}", headers=h.headers).json()["pieces"]
    assert [(p["part_number"], p["size_bytes"]) for p in before] == [(1, CHUNK_SIZE_BYTES)]

    incomplete = problem(h.complete(upload_id), 409, "UPLOAD_INCOMPLETE")
    assert "etag" not in incomplete["detail"].lower()
    assert h.row(Upload, upload_id).state == UploadState.OPEN  # still resumable

    assert h.send(upload_id, 2, tail).status_code == 200  # only the missing piece is sent
    after = h.http.get(f"/api/v1/uploads/{upload_id}", headers=h.headers).json()["pieces"]
    assert [p["part_number"] for p in after] == [1, 2] and after[0] == before[0]  # piece 1 untouched
    assert h.complete(upload_id).json()["state"] == "uploaded"
    assert h.storage.head_size(h.row(Upload, upload_id).object_key) == len(first) + len(tail)


def test_wrong_piece_size_is_not_accepted_until_replaced(make) -> None:
    h = make()
    upload_id = h.begin(1000)
    sent = h.send(upload_id, 1, b"x" * 999)
    if sent.status_code == 200:  # AWS rejects the length mismatch at upload; Floci stores it
        problem(h.complete(upload_id), 409, "UPLOAD_INCOMPLETE")
        assert h.row(Upload, upload_id).state == UploadState.OPEN
    assert h.send(upload_id, 1, b"x" * 1000).status_code == 200
    assert h.complete(upload_id).status_code == 202


def test_authentication_and_ownership(make) -> None:
    h = make()
    upload_id = h.begin(100)
    other = h.other_owner()
    routes = [("post", f"/api/v1/uploads/{upload_id}/urls", {"part_numbers": [1]}),
              ("get", f"/api/v1/uploads/{upload_id}", None),
              ("post", f"/api/v1/uploads/{upload_id}/complete", {}),
              ("delete", f"/api/v1/uploads/{upload_id}", None),
              ("post", f"/api/v1/sessions/{h.session_id}/parts",
               {"part_no": 2, "content_type": "video/mp4", "content_length": 10})]
    for method, path, body in routes:
        kwargs = {"json": body} if body is not None else {}
        problem(getattr(h.http, method)(path, **kwargs), 401, "TOKEN_INVALID")
        problem(getattr(h.http, method)(path, headers={"Authorization": "Bearer " + "x" * 43}, **kwargs), 401, "TOKEN_INVALID")
        problem(getattr(h.http, method)(path, headers=other, **kwargs), 404, "NOT_FOUND")
    unknown = uuid4()
    problem(h.urls(unknown, [1]), 404, "NOT_FOUND")
    assert h.row(Upload, upload_id).state == UploadState.OPEN  # foreign delete did nothing
    assert h.part(2).state == PartState.PENDING


@pytest.mark.parametrize("body", [
    {"part_no": 1, "content_type": "video/mp4", "content_length": 0},
    {"part_no": 1, "content_type": "video/mp4", "content_length": -5},
    {"part_no": 1, "content_type": "video/mp4", "content_length": "10"},
    {"part_no": 1, "content_type": "video/mp4", "content_length": 10.5},
    {"part_no": 1, "content_type": "video/mp4", "content_length": True},
    {"part_no": 1, "content_type": "image/png", "content_length": 10},
    {"part_no": 0, "content_type": "video/mp4", "content_length": 10},
    {"part_no": 9, "content_type": "video/mp4", "content_length": 10},
    {"part_no": 1, "content_type": "video/mp4"},
])
def test_invalid_declarations_are_rejected_without_side_effects(make, body) -> None:
    h = make()
    problem(h.http.post(f"/api/v1/sessions/{h.session_id}/parts", headers=h.headers, json=body), 422, "VALIDATION")
    assert h.part(1).state == PartState.PENDING


def test_oversized_clip_and_aggregate_limits(make) -> None:
    h = make(session_upload_limit_bytes=3000)
    problem(h.start(200 * MIB + 1), 413, "UPLOAD_TOO_LARGE")
    first = h.begin(2000, part_no=1)
    problem(h.start(1001, part_no=2), 413, "UPLOAD_TOO_LARGE")  # accepted + open attempts count
    assert h.part(2).state == PartState.PENDING
    assert h.http.delete(f"/api/v1/uploads/{first}", headers=h.headers).status_code == 204
    assert h.start(1001, part_no=2).status_code == 201  # an aborted attempt no longer counts
    assert h.start(1999, part_no=3).status_code == 201
    problem(h.start(1, part_no=4), 413, "UPLOAD_TOO_LARGE")


def test_url_request_validation(make) -> None:
    h = make()
    upload_id = h.begin(CHUNK_SIZE_BYTES + 1)  # two pieces
    for numbers in ([], [0], [3], [1, 1], list(range(1, 12)), [-1], ["1"]):
        problem(h.urls(upload_id, numbers), 422, "VALIDATION")
    assert [i["part_number"] for i in h.urls(upload_id, [2, 1]).json()["items"]] == [2, 1]
    # Ten URLs per call is the ceiling; a 25-piece upload may ask for ten at once.
    big = make()
    big_id = big.begin(200 * MIB)
    assert len(big.urls(big_id, list(range(1, 11))).json()["items"]) == 10
    problem(big.urls(big_id, list(range(1, 12))), 422, "VALIDATION")
    assert big.urls(big_id, [25]).status_code == 200
    problem(big.urls(big_id, [26]), 422, "VALIDATION")


def test_expiry_default_is_fifteen_minutes_and_expired_url_can_be_refreshed(make) -> None:
    h = make()
    upload_id = h.begin(10)
    item = h.urls(upload_id, [1]).json()["items"][0]
    assert "X-Amz-Expires=900" in item["url"]
    short = make(upload_url_ttl_seconds=3)
    short_id = short.begin(10)
    expired = short.urls(short_id, [1]).json()["items"][0]["url"]
    time.sleep(5)
    assert httpx.put(expired, content=b"x" * 10, timeout=30).status_code == 403
    refreshed = short.urls(short_id, [1]).json()["items"][0]["url"]
    assert refreshed != expired
    assert httpx.put(refreshed, content=b"x" * 10, timeout=30).status_code == 200
    assert short.complete(short_id).status_code == 202


def test_url_refresh_is_rate_limited_per_session_in_postgres(make, session_factory) -> None:
    h = make(upload_url_refresh_limit_per_minute=2)
    upload_id = h.begin(10)
    for _ in range(2):
        assert h.urls(upload_id, [1]).status_code == 200
    limited = h.urls(upload_id, [1])
    problem(limited, 429, "RATE_LIMITED")
    assert 1 <= int(limited.headers["retry-after"]) <= 60
    with session_factory() as db:  # the limiter key is a hash, never the session id
        keys = db.scalars(select(RateLimitBucket.source_hash)).all()
        assert str(h.session_id) not in keys and all(len(k) == 64 for k in keys)
    other = make(upload_url_refresh_limit_per_minute=2)  # another session has its own budget
    assert other.urls(other.begin(10), [1]).status_code == 200


def test_abort_returns_part_to_pending_and_is_idempotent(make) -> None:
    h = make()
    upload_id = h.begin(CHUNK_SIZE_BYTES + 10)
    assert h.send(upload_id, 1, os.urandom(CHUNK_SIZE_BYTES)).status_code == 200
    old = h.row(Upload, upload_id)
    # A client that lost its local record can find the stranded attempt from session state.
    parts = h.http.get(f"/api/v1/sessions/{h.session_id}", headers=h.headers).json()["parts"]
    assert parts[0]["active_upload_id"] == str(upload_id) and parts[1]["active_upload_id"] is None
    assert h.http.delete(f"/api/v1/uploads/{upload_id}", headers=h.headers).status_code == 204
    assert h.http.delete(f"/api/v1/uploads/{upload_id}", headers=h.headers).status_code == 204
    part = h.part(1)
    assert (part.state, part.active_upload_id, part.raw_key) == (PartState.PENDING, None, None)
    assert h.row(Upload, upload_id).state == UploadState.ABORTED
    with pytest.raises(StorageError):
        h.storage.list_pieces(old.object_key, old.provider_upload_id)  # storage upload is gone
    problem(h.urls(upload_id, [1]), 409, "STATE_CONFLICT")
    problem(h.complete(upload_id), 409, "STATE_CONFLICT")
    # A retake gets a fresh attempt with a different key.
    retake = h.begin(10)
    assert h.row(Upload, retake).object_key != old.object_key


def test_accepted_upload_cannot_be_aborted_or_overwritten_with_an_old_piece_url(make) -> None:
    h = make()
    original = b"A" * 100
    upload_id = h.begin(100)
    old_url = h.urls(upload_id, [1]).json()["items"][0]["url"]
    assert httpx.put(old_url, content=original, timeout=30).status_code == 200
    assert h.complete(upload_id).status_code == 202
    key = h.row(Upload, upload_id).object_key
    replay = httpx.put(old_url, content=b"B" * 100, timeout=30)
    assert replay.status_code in (403, 404)  # Floci: 404 NoSuchUpload
    assert h.storage.internal.get_object(Bucket=h.settings.s3_bucket, Key=key)["Body"].read() == original
    problem(h.urls(upload_id, [1]), 409, "STATE_CONFLICT")
    problem(h.http.delete(f"/api/v1/uploads/{upload_id}", headers=h.headers), 409, "STATE_CONFLICT")
    assert h.row(Upload, upload_id).state == UploadState.ACCEPTED
    problem(h.start(10), 409, "STATE_CONFLICT")  # uploaded part cannot start a new attempt


def test_idempotent_create_replays_without_a_second_upload_or_stored_url(make, session_factory) -> None:
    h = make()
    first = h.start(500, **{"Idempotency-Key": "create-1"})
    again = h.start(500, **{"Idempotency-Key": "create-1"})
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Upload).where(Upload.session_id == h.session_id)) == 1
        stored = db.scalars(select(RequestKey).where(RequestKey.authority_id == h.session_id)).all()
        assert len(stored) == 1 and set(stored[0].response) == {"part_id", "upload_id", "chunk_size_bytes", "chunk_count"}
    problem(h.start(501, **{"Idempotency-Key": "create-1"}), 409, "STATE_CONFLICT")
    problem(h.start(500, **{"Idempotency-Key": ""}), 422, "VALIDATION")


def test_session_lifecycle_gates_uploads(make, session_factory) -> None:
    h = make()
    assert h.http.post(f"/api/v1/parts/{h.part(2).id}/skip", headers=h.headers, json={}).status_code == 200
    problem(h.start(10, part_no=2), 409, "STATE_CONFLICT")  # skipped part
    open_id = h.begin(10, part_no=1)
    problem(h.http.post(f"/api/v1/sessions/{h.session_id}/finalize", headers=h.headers, json={}), 409, "STATE_CONFLICT")
    assert h.http.delete(f"/api/v1/uploads/{open_id}", headers=h.headers).status_code == 204
    assert h.http.post(f"/api/v1/sessions/{h.session_id}/finalize", headers=h.headers, json={}).status_code == 202
    problem(h.start(10, part_no=1), 409, "STATE_CONFLICT")
    problem(h.urls(open_id, [1]), 409, "STATE_CONFLICT")
    assert h.http.delete(f"/api/v1/uploads/{open_id}", headers=h.headers).status_code == 204  # idempotent

    gone = make()
    live = gone.begin(10)
    with session_factory.begin() as db:
        db.execute(update(Session).where(Session.id == gone.session_id).values(state=SessionState.DELETING))
    problem(gone.start(10, part_no=2), 404, "NOT_FOUND")
    problem(gone.urls(live, [1]), 404, "NOT_FOUND")
    problem(gone.complete(live), 404, "NOT_FOUND")
    problem(gone.http.get(f"/api/v1/uploads/{live}", headers=gone.headers), 404, "NOT_FOUND")


class CountingStorage(S3Storage):
    """Test double over real storage that can inject faults after the real call."""
    complete_calls = 0
    head_override: int | None = None
    complete_error: StorageError | None = None
    delay = 0.0

    def complete(self, key, provider_upload_id, pieces) -> None:
        type(self).complete_calls += 1
        if self.complete_error:
            raise self.complete_error
        time.sleep(self.delay)
        super().complete(key, provider_upload_id, pieces)

    def head_size(self, key) -> int | None:
        return self.head_override if self.head_override is not None else super().head_size(key)


@pytest.fixture
def faulty(make):
    def build(**faults: object) -> Harness:
        class Faulty(CountingStorage):
            complete_calls = 0
        for name, value in faults.items():
            setattr(Faulty, name, value)
        return make(storage_cls=Faulty)
    return build


def test_size_mismatch_after_completion_is_not_accepted_and_repeat_does_not_complete_again(faulty) -> None:
    h = faulty(head_override=999)
    upload_id = h.begin(1000)
    assert h.send(upload_id, 1, b"x" * 1000).status_code == 200
    problem(h.complete(upload_id), 409, "UPLOAD_INCOMPLETE")
    upload, part = h.row(Upload, upload_id), h.part(1)
    # Clear hand-off state for T03c: object may exist, nothing accepted, no raw_key published.
    assert (upload.state, upload.accepted_at) == (UploadState.COMPLETING, None)
    assert (part.state, part.raw_key, part.active_upload_id) == (PartState.UPLOADING, None, upload_id)
    repeat = h.complete(upload_id)
    assert repeat.status_code == 202 and repeat.json()["state"] == "uploading"
    assert type(h.storage).complete_calls == 1
    problem(h.http.delete(f"/api/v1/uploads/{upload_id}", headers=h.headers), 409, "STATE_CONFLICT")


def test_definitive_storage_rejection_reopens_and_transient_failure_stays_completing(faulty) -> None:
    rejected = faulty(complete_error=StorageError("InvalidPart", definitive=True))
    upload_id = rejected.begin(100)
    assert rejected.send(upload_id, 1, b"x" * 100).status_code == 200
    problem(rejected.complete(upload_id), 409, "UPLOAD_INCOMPLETE")
    assert rejected.row(Upload, upload_id).state == UploadState.OPEN

    flaky = faulty(complete_error=StorageError("InternalError"))
    flaky_id = flaky.begin(100)
    assert flaky.send(flaky_id, 1, b"x" * 100).status_code == 200
    body = problem(flaky.complete(flaky_id), 503, "INTERNAL")
    assert "InternalError" not in str(body) and flaky.settings.s3_internal_endpoint not in str(body)
    assert flaky.row(Upload, flaky_id).state == UploadState.COMPLETING


def test_concurrent_completes_create_one_object_and_both_return_status(faulty) -> None:
    h = faulty(delay=0.5)
    upload_id = h.begin(100)
    assert h.send(upload_id, 1, b"x" * 100).status_code == 200
    barrier = threading.Barrier(2)

    def race() -> httpx.Response:
        barrier.wait()
        return h.complete(upload_id)
    with ThreadPoolExecutor(2) as pool:
        results = [f.result() for f in [pool.submit(race), pool.submit(race)]]
    assert [r.status_code for r in results] == [202, 202]
    assert type(h.storage).complete_calls == 1
    assert h.row(Upload, upload_id).state == UploadState.ACCEPTED


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:9", "invalid"])
def test_unreachable_or_misconfigured_storage_yields_503_and_leaves_no_attempt(session_factory, bucket, endpoint) -> None:
    settings = storage_settings(bucket, s3_internal_endpoint=endpoint)
    app = create_app(settings, database_probe=lambda _: True, session_factory=session_factory,
                     storage=S3Storage(settings))
    address = str(ipaddress.IPv6Address(int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int & ((1 << 64) - 1))))
    http = TestClient(app, client=(address, 1))
    created = http.post("/api/v1/sessions", json={"purpose": "buyer", "consent": True,
                                                  "consent_version": "upclose-prototype-v1"}).json()
    headers = {"Authorization": f"Bearer {created['session_token']}"}
    body = problem(http.post(f"/api/v1/sessions/{created['session_id']}/parts", headers=headers,
        json={"part_no": 1, "content_type": "video/mp4", "content_length": 10}), 503, "INTERNAL")
    assert endpoint not in str(body)
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Upload).where(Upload.session_id == UUID(created["session_id"]))) == 0
        assert db.scalar(select(Part.state).where(Part.session_id == UUID(created["session_id"]), Part.part_no == 1)) == PartState.PENDING
