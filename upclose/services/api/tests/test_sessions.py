"""Owner lifecycle acceptance against isolated PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.models import Part, Report, ReportRevision, RequestKey, Session, Upload
from app.models.rate_limits import RateLimitBucket
from app.services.auth import token_hash
from app.services.idempotency import execute_idempotent
from app.services.problems import ProblemError
from app.settings import Settings


@pytest.fixture(scope="module")
def session_factory():
    url = os.environ.get("SESSION_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("An isolated PostgreSQL SESSION_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing owner tests outside an explicitly named upclose_test_* PostgreSQL database")
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def app(session_factory):
    return create_app(Settings(), database_probe=lambda _: True, session_factory=session_factory)


def client(app, ip: str | None = None):
    # Unique source bucket by default; explicit values exercise shared-IP limits.
    address = ip or f"2001:db8::{uuid4().int & ((1 << 64) - 1):x}"
    # Format valid IPv6 groups through its integer representation.
    if ip is None:
        import ipaddress
        address = str(ipaddress.IPv6Address(int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int & ((1 << 64) - 1))))
    return TestClient(app, client=(address, 12345))


def create(c, **body):
    return c.post("/api/v1/sessions", json={"purpose": "buyer", "consent": True,
        "consent_version": "upclose-prototype-v1", **body})


def owner(c):
    response = create(c)
    assert response.status_code == 201, response.text
    data = response.json()
    return data["session_id"], {"Authorization": "Bearer " + data["session_token"]}, data["session_token"]


def snapshot(session_factory, session_id: str):
    with session_factory() as db:
        s = db.get(Session, session_id)
        return s.state.value, s.listing_version


@pytest.mark.parametrize("consent", [False, 1, "true", None])
def test_actual_true_consent_only(app, session_factory, consent):
    c = client(app)
    assert create(c, consent=consent).status_code == 422


def test_missing_unknown_consent_and_private_validation(app):
    c = client(app)
    assert c.post("/api/v1/sessions", json={"purpose": "buyer"}).status_code == 422
    assert create(c, consent_version="unknown").status_code == 422
    private = "do-not-echo-private-input"
    response = create(c, purpose=private)
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert private not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_creation_hashes_token_and_canonical_parts(app, session_factory, caplog):
    c = client(app)
    sid, headers, token = owner(c)
    response = c.get(f"/api/v1/sessions/{sid}", headers=headers)
    assert response.status_code == 200
    assert [part["part_no"] for part in response.json()["parts"]] == list(range(1, 9))
    assert [part["zone"] for part in response.json()["parts"]] == ["driver_side", "passenger_side", "front_rear_body", "tires", "dash_electrical", "engine_bay", "underbody", "interior"]
    assert token not in response.text
    assert token not in caplog.text
    with session_factory() as db:
        row = db.get(Session, sid)
        assert row.token_hash == token_hash(token)
        assert len(token) >= 43
        assert db.scalar(select(func.count()).select_from(RequestKey).where(RequestKey.authority_id == row.id)) == 0
        assert token not in json.dumps({column.name: str(getattr(row, column.name)) for column in Session.__table__.columns})


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic abc", "Bearer " + "x" * 43])
def test_invalid_bearer_401(app, authorization):
    c = client(app)
    headers = {"Authorization": authorization} if authorization else {}
    response = c.get(f"/api/v1/sessions/{uuid4()}", headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_INVALID"


def test_owner_cannot_read_or_change_foreign_resource(app):
    a, b = client(app), client(app)
    sid_a, headers_a, _ = owner(a)
    sid_b, headers_b, _ = owner(b)
    part_b = b.get(f"/api/v1/sessions/{sid_b}", headers=headers_b).json()["parts"][0]["id"]
    assert a.get(f"/api/v1/sessions/{sid_b}", headers=headers_a).status_code == 404
    assert a.put(f"/api/v1/sessions/{sid_b}/listing", headers=headers_a, json={"listing_text": "foreign"}).status_code == 404
    assert a.post(f"/api/v1/parts/{part_b}/skip", headers=headers_a, json={}).status_code == 404
    assert a.post(f"/api/v1/sessions/{sid_b}/finalize", headers=headers_a, json={}).status_code == 404
    assert a.get(f"/api/v1/sessions/{uuid4()}", headers=headers_a).status_code == 404


def test_listing_version_and_idempotency_replay_conflict(app, session_factory):
    c = client(app)
    sid, headers, _ = owner(c)
    keyed = {**headers, "Idempotency-Key": "listing-one"}
    route = f"/api/v1/sessions/{sid}/listing"
    first = c.put(route, headers=keyed, json={"listing_text": "Original claim"})
    assert first.status_code == 200 and first.json() == {"listing_version": 2}
    replay = c.put(route, headers=keyed, json={"listing_text": "Original claim"})
    assert replay.json() == first.json()
    conflict = c.put(route, headers=keyed, json={"listing_text": "Changed claim"})
    assert conflict.status_code == 409
    assert c.get(f"/api/v1/sessions/{sid}", headers=headers).json()["listing_text"] == "Original claim"
    with session_factory() as db:
        rows = db.scalars(select(RequestKey).where(RequestKey.authority_id == sid)).all()
        assert len(rows) == 1 and rows[0].response == {"listing_version": 2}


def test_creation_is_not_replayed_even_with_same_key(app, session_factory):
    c = client(app)
    body = {"purpose": "buyer", "consent": True, "consent_version": "upclose-prototype-v1"}
    first = c.post("/api/v1/sessions", json=body, headers={"Idempotency-Key": "create-one"})
    second = c.post("/api/v1/sessions", json=body, headers={"Idempotency-Key": "create-one"})
    assert first.status_code == second.status_code == 201
    assert first.json()["session_id"] != second.json()["session_id"]
    assert first.json()["session_token"] != second.json()["session_token"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(RequestKey).where(RequestKey.authority_id.in_([first.json()["session_id"], second.json()["session_id"]]))) == 0


def test_finalization_skips_pending_freezes_listing_and_repeats(app, session_factory):
    c = client(app)
    sid, headers, token = owner(c)
    first = c.post(f"/api/v1/sessions/{sid}/finalize", headers=headers, json={})
    second = c.post(f"/api/v1/sessions/{sid}/finalize", headers=headers, json={})
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert first.json()["report_slug"] != token
    assert token not in first.text
    response = c.get(f"/api/v1/sessions/{sid}", headers=headers).json()
    assert response["state"] == "finalized"
    assert all(p["state"] == "skipped" for p in response["parts"])
    assert c.put(f"/api/v1/sessions/{sid}/listing", headers=headers, json={"listing_text": "too late"}).status_code == 409
    with session_factory() as db:
        report = db.scalar(select(Report).where(Report.session_id == sid))
        assert db.scalar(select(func.count()).select_from(ReportRevision).where(ReportRevision.report_id == report.id)) == 1
        revision = db.get(ReportRevision, report.current_revision_id)
        assert revision.state.value == "building"
        assert len(revision.manifest["finalized_selection"]["parts"]) == 8


@pytest.mark.parametrize("upload_state", ["open", "completing"])
def test_finalize_refuses_open_and_completing_upload(app, session_factory, upload_state):
    c = client(app)
    sid, headers, _ = owner(c)
    with session_factory.begin() as db:
        part = db.scalar(select(Part).where(Part.session_id == sid, Part.part_no == 1))
        db.add(Upload(session_id=part.session_id, part_id=part.id, object_key=uuid4().hex,
            provider_upload_id=uuid4().hex, expected_bytes=1, content_type="video/mp4", state=upload_state))
    assert c.post(f"/api/v1/sessions/{sid}/finalize", headers=headers, json={}).status_code == 409
    assert snapshot(session_factory, sid)[0] == "capturing"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Report).where(Report.session_id == sid)) == 0


def test_processing_allowed_and_part_one_never_auto_finalizes(app, session_factory):
    c = client(app)
    sid, headers, _ = owner(c)
    with session_factory.begin() as db:
        db.execute(update(Part).where(Part.session_id == sid, Part.part_no == 1).values(state="processed"))
        db.execute(update(Part).where(Part.session_id == sid, Part.part_no == 2).values(state="processing"))
    assert c.get(f"/api/v1/sessions/{sid}", headers=headers).json()["state"] == "capturing"
    assert c.post(f"/api/v1/sessions/{sid}/finalize", headers=headers, json={}).status_code == 202
    data = c.get(f"/api/v1/sessions/{sid}", headers=headers).json()
    assert data["parts"][1]["state"] == "processing"


def test_skip_pending_only_and_repeated_skip(app, session_factory):
    c = client(app)
    sid, headers, _ = owner(c)
    parts = c.get(f"/api/v1/sessions/{sid}", headers=headers).json()["parts"]
    route = f"/api/v1/parts/{parts[0]['id']}/skip"
    assert c.post(route, headers=headers, json={}).status_code == 200
    assert c.post(route, headers=headers, json={}).json() == {"state": "skipped"}
    with session_factory.begin() as db:
        db.execute(update(Part).where(Part.id == parts[1]["id"]).values(state="processing"))
    assert c.post(f"/api/v1/parts/{parts[1]['id']}/skip", headers=headers, json={}).status_code == 409


def test_rate_limit_shared_across_apps_and_forwarded_header_untrusted(session_factory):
    ip = "192.0.2.42"
    settings = Settings()
    a = create_app(settings, session_factory=session_factory)
    b = create_app(settings, session_factory=session_factory)
    for app in [a, b, a]:
        assert create(client(app, ip), listing_text="synthetic").status_code == 201
    response = client(b, ip).post("/api/v1/sessions", json={"purpose": "buyer", "consent": True,
        "consent_version": settings.consent_version}, headers={"X-Forwarded-For": "192.0.2.99"})
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 86400
    with session_factory() as db:
        contents = [row.source_hash for row in db.scalars(select(RateLimitBucket)).all()]
        assert ip not in json.dumps(contents)


def test_rate_limit_concurrent_atomic(app):
    ip = str(__import__('ipaddress').IPv6Address(int(__import__('ipaddress').IPv6Address('2001:db8::')) + uuid4().int % (1 << 64)))
    def attempt(_):
        return create(client(app, ip)).status_code
    with ThreadPoolExecutor(max_workers=6) as executor:
        statuses = list(executor.map(attempt, range(6)))
    assert sorted(statuses) == [201, 201, 201, 429, 429, 429]


def test_explicit_proxy_trust_and_invalid_proxy_config(session_factory):
    settings = Settings(trusted_proxy_hosts=("127.0.0.1/32",))
    app = create_app(settings, session_factory=session_factory)
    c = client(app, "127.0.0.1")
    for i in range(4):
        # A trusted proxy can carry independent source addresses; direct clients cannot.
        response = c.post("/api/v1/sessions", json={"purpose": "buyer", "consent": True,
            "consent_version": settings.consent_version}, headers={"X-Forwarded-For": f"198.51.100.{i}"})
        assert response.status_code == 201
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(trusted_proxy_hosts=("0.0.0.0/0",))


def test_concurrent_finalize_converges(app, session_factory):
    c = client(app)
    sid, headers, _ = owner(c)
    def finalize(_):
        return client(app).post(f"/api/v1/sessions/{sid}/finalize", headers=headers, json={})
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(finalize, range(4)))
    assert all(r.status_code == 202 for r in responses)
    assert len({r.json()["report_slug"] for r in responses}) == 1
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ReportRevision).where(ReportRevision.session_id == sid)) == 1


def test_idempotency_service_rejects_nested_secret_response(session_factory, app):
    c = client(app)
    sid, headers, _ = owner(c)
    with session_factory.begin() as db:
        with pytest.raises(ValueError):
            execute_idempotent(db, authority_id=sid, route="/test", key="secret-key", body={},
                               operation=lambda: {"nested": {"session_token": "must-not-store"}})


def test_unauthenticated_request_never_opens_database():
    class ForbiddenFactory:
        def begin(self):
            raise AssertionError("Missing auth must not touch the database")
    app = create_app(session_factory=ForbiddenFactory())
    response = client(app).get(f"/api/v1/sessions/{uuid4()}")
    assert response.status_code == 401


@pytest.mark.parametrize("state", ["deleting", "deleted"])
def test_deleted_owner_cannot_read_or_mutate_content(app, session_factory, state):
    c = client(app)
    sid, headers, _ = owner(c)
    part_id = c.get(f"/api/v1/sessions/{sid}", headers=headers).json()["parts"][0]["id"]
    with session_factory.begin() as db:
        db.execute(update(Session).where(Session.id == sid).values(state=state, listing_text="private tombstone content"))
    for response in [c.get(f"/api/v1/sessions/{sid}", headers=headers),
                     c.put(f"/api/v1/sessions/{sid}/listing", headers=headers, json={"listing_text": "new"}),
                     c.post(f"/api/v1/parts/{part_id}/skip", headers=headers, json={}),
                     c.post(f"/api/v1/sessions/{sid}/finalize", headers=headers, json={})]:
        assert response.status_code == 404
        assert "private tombstone content" not in response.text


def test_service_idempotency_replays_once_and_ignores_early_expiry(app, session_factory):
    c = client(app)
    sid, _, _ = owner(c)
    calls = []
    def operation():
        calls.append(True)
        return {"listing_version": 2}
    with session_factory.begin() as db:
        first = execute_idempotent(db, authority_id=sid, route="/test", key="same", body={"a": 1}, operation=operation)
        # Informational expiry is not authority to discard a session-lifetime replay record.
        db.execute(update(RequestKey).where(RequestKey.authority_id == sid).values(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc)))
    with session_factory.begin() as db:
        replay = execute_idempotent(db, authority_id=sid, route="/test", key="same", body={"a": 1}, operation=operation)
        assert first == replay == {"listing_version": 2}
        with pytest.raises(ProblemError) as error:
            execute_idempotent(db, authority_id=sid, route="/test", key="same", body={"a": 2}, operation=operation)
        assert error.value.status == 409
    assert len(calls) == 1


def test_repeated_identical_listing_put_is_naturally_idempotent(app):
    c = client(app)
    sid, headers, _ = owner(c)
    path = f"/api/v1/sessions/{sid}/listing"
    first = c.put(path, headers=headers, json={"listing_text": "same"})
    repeat = c.put(path, headers=headers, json={"listing_text": "same"})
    assert first.json() == repeat.json() == {"listing_version": 2}
