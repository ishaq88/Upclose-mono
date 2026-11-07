"""T15b account HTTP API acceptance against isolated PostgreSQL only.

Covers register/login/logout/me, password change, auto-link on session
creation, claim, my-reports isolation, and account read access with owner
entitlement rules unchanged. Runs only against an explicitly named
disposable ``upclose_test_*`` PostgreSQL database (``MODEL_TEST_DATABASE_URL``
or ``SESSION_TEST_DATABASE_URL``), matching ``test_sessions.py``.
"""
from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.models import Entitlement, Purchase, Session
from app.services.accounts import AccountService
from app.settings import Settings
from tests.test_ingest import media_dir, scratch_root  # noqa: F401
from tests.test_orchestration import isolate  # noqa: F401
from tests.test_report_routes import ready, report_of  # noqa: F401
from tests.test_uploads import bucket, make  # noqa: F401

PASSWORD = "correct-horse-10"
NEW_PASSWORD = "brand-new-password-11"


@pytest.fixture(scope="module")
def session_factory():
    url = os.environ.get("SESSION_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("An isolated PostgreSQL MODEL_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing account API tests outside an explicitly named upclose_test_* PostgreSQL database")
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def app(session_factory):
    settings = Settings(account_pbkdf2_iterations=1000)
    return create_app(settings, database_probe=lambda _: True, session_factory=session_factory)


def client(app) -> TestClient:
    address = str(ipaddress.IPv6Address(
        int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int & ((1 << 64) - 1))))
    return TestClient(app, client=(address, 12345))


def unique_email() -> str:
    return f"dealer-{uuid4().hex[:16]}@example.com"


def register(c: TestClient, email: str | None = None, password: str = PASSWORD):
    response = c.post("/api/v1/accounts/register",
                      json={"email": email or unique_email(), "password": password})
    assert response.status_code == 201, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def create_session(c: TestClient, headers: dict[str, str] | None = None):
    response = c.post("/api/v1/sessions", headers=headers or {}, json={
        "purpose": "buyer", "consent": True, "consent_version": "upclose-prototype-v1"})
    assert response.status_code == 201, response.text
    return response.json()


def account_row(session_factory, session_id: str):
    with session_factory() as db:
        return db.get(Session, session_id).account_id


# ------------------------------------------------------------- auth lifecycle


def test_register_login_logout_me(app, session_factory):
    c = client(app)
    email = unique_email()
    body = register(c, email)
    assert set(body) == {"account", "token", "expires_at"}
    assert body["account"]["email"] == email and body["account"]["id"]
    assert "password" not in c.post("/api/v1/accounts/register", json={
        "email": email, "password": PASSWORD}).text
    duplicate = c.post("/api/v1/accounts/register", json={"email": email.upper(), "password": PASSWORD})
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "EMAIL_TAKEN"

    token = body["token"]
    me = c.get("/api/v1/accounts/me", headers=bearer(token))
    assert me.status_code == 200 and me.json()["email"] == email
    assert token not in me.text

    wrong = c.post("/api/v1/accounts/login", json={"email": email, "password": "wrong-password-1"})
    unknown = c.post("/api/v1/accounts/login", json={"email": unique_email(), "password": "wrong-password-1"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["code"] == unknown.json()["code"] == "INVALID_CREDENTIALS"
    assert wrong.json()["detail"] == unknown.json()["detail"]

    logged = c.post("/api/v1/accounts/login", json={"email": email, "password": PASSWORD})
    assert logged.status_code == 200 and logged.json()["token"]
    second_token = logged.json()["token"]

    assert c.post("/api/v1/accounts/logout", headers=bearer(second_token)).status_code == 204
    assert c.get("/api/v1/accounts/me", headers=bearer(second_token)).status_code == 401
    # The register token is a different token and survives.
    assert c.get("/api/v1/accounts/me", headers=bearer(token)).status_code == 200
    assert c.get("/api/v1/accounts/me").status_code == 401
    assert c.get("/api/v1/accounts/me", headers=bearer("x" * 43)).status_code == 401
    bad = c.post("/api/v1/accounts/register", json={"email": "not-an-email", "password": PASSWORD})
    assert bad.status_code == 422 and bad.json()["code"] == "VALIDATION"
    weak = c.post("/api/v1/accounts/register", json={"email": unique_email(), "password": "short"})
    assert weak.status_code == 422


def test_password_change_revokes_other_tokens(app):
    c = client(app)
    email = unique_email()
    first = register(c, email)["token"]
    second = c.post("/api/v1/accounts/login", json={"email": email, "password": PASSWORD}).json()["token"]

    wrong = c.post("/api/v1/accounts/password", headers=bearer(first),
                   json={"current_password": "wrong-password-1", "new_password": NEW_PASSWORD})
    assert wrong.status_code == 401 and wrong.json()["code"] == "INVALID_CREDENTIALS"

    changed = c.post("/api/v1/accounts/password", headers=bearer(first),
                     json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert changed.status_code == 204
    assert c.get("/api/v1/accounts/me", headers=bearer(first)).status_code == 200  # caller kept
    assert c.get("/api/v1/accounts/me", headers=bearer(second)).status_code == 401  # others revoked
    assert c.post("/api/v1/accounts/login", json={"email": email, "password": PASSWORD}).status_code == 401
    assert c.post("/api/v1/accounts/login", json={"email": email, "password": NEW_PASSWORD}).status_code == 200


# ------------------------------------------------------------ linking / claim


def test_auto_link_on_create_and_invalid_header_rejected(app, session_factory):
    c = client(app)
    account = register(c)
    headers = bearer(account["token"])
    created = create_session(c, headers)
    assert set(created) == {"session_id", "session_token"}  # anonymous shape unchanged
    assert str(account_row(session_factory, created["session_id"])) == account["account"]["id"]

    detail = c.get(f"/api/v1/sessions/{created['session_id']}", headers=headers)
    assert detail.status_code == 200 and detail.json()["session_id"] == created["session_id"]

    rejected = c.post("/api/v1/sessions", headers=bearer("y" * 43), json={
        "purpose": "buyer", "consent": True, "consent_version": "upclose-prototype-v1"})
    assert rejected.status_code == 401 and rejected.json()["code"] == "TOKEN_INVALID"


def test_anonymous_flow_unchanged(app, session_factory):
    c = client(app)
    created = create_session(c)
    assert set(created) == {"session_id", "session_token"}
    assert account_row(session_factory, created["session_id"]) is None
    owner_headers = bearer(created["session_token"])
    detail = c.get(f"/api/v1/sessions/{created['session_id']}", headers=owner_headers)
    assert detail.status_code == 200 and len(detail.json()["parts"]) == 8
    # An owner token is not an account token.
    assert c.get("/api/v1/accounts/me", headers=owner_headers).status_code == 401
    assert c.get("/api/v1/accounts/sessions", headers=owner_headers).status_code == 401


def test_claim_happy_conflict_and_wrong_owner(app, session_factory):
    c = client(app)
    first, second = register(c), register(c)
    created = create_session(c)
    session_id, owner_token = created["session_id"], created["session_token"]
    other = create_session(c)

    assert c.post("/api/v1/accounts/claim", json={
        "session_id": session_id, "owner_token": owner_token}).status_code == 401
    wrong = c.post("/api/v1/accounts/claim", headers=bearer(first["token"]),
                   json={"session_id": session_id, "owner_token": "z" * 43})
    assert wrong.status_code == 401  # unknown owner token: indistinguishable
    foreign = c.post("/api/v1/accounts/claim", headers=bearer(first["token"]),
                     json={"session_id": session_id, "owner_token": other["session_token"]})
    assert foreign.status_code == 404  # owner token of another inspection
    assert account_row(session_factory, session_id) is None

    claimed = c.post("/api/v1/accounts/claim", headers=bearer(first["token"]),
                     json={"session_id": session_id, "owner_token": owner_token})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json() == {"session_id": session_id, "account_id": first["account"]["id"]}
    again = c.post("/api/v1/accounts/claim", headers=bearer(first["token"]),
                   json={"session_id": session_id, "owner_token": owner_token})
    assert again.status_code == 200  # idempotent for the same account

    conflict = c.post("/api/v1/accounts/claim", headers=bearer(second["token"]),
                      json={"session_id": session_id, "owner_token": owner_token})
    assert conflict.status_code == 409
    assert str(account_row(session_factory, session_id)) == first["account"]["id"]


# --------------------------------------------------------------- my reports


def test_my_sessions_isolation_and_free_safe_summaries(app, session_factory):
    c = client(app)
    first, second = register(c), register(c)
    mine_a = create_session(client(app), bearer(first["token"]))
    mine_b = create_session(client(app), bearer(first["token"]))
    theirs = create_session(client(app), bearer(second["token"]))
    create_session(client(app))  # anonymous: in nobody's list

    owner_headers = bearer(mine_b["session_token"])
    finalized = c.post(f"/api/v1/sessions/{mine_b['session_id']}/finalize",
                       headers=owner_headers, json={})
    assert finalized.status_code == 202, finalized.text
    slug = finalized.json()["report_slug"]

    response = c.get("/api/v1/accounts/sessions", headers=bearer(first["token"]))
    assert response.status_code == 200, response.text
    rows = response.json()["sessions"]
    assert {row["session_id"] for row in rows} == {mine_a["session_id"], mine_b["session_id"]}
    assert rows[0]["session_id"] == mine_b["session_id"]  # newest first
    by_id = {row["session_id"]: row for row in rows}
    assert by_id[mine_a["session_id"]]["part_state_counts"] == {"pending": 8}
    assert by_id[mine_a["session_id"]]["report_slug"] is None
    assert by_id[mine_a["session_id"]]["report_state"] is None
    done = by_id[mine_b["session_id"]]
    assert done["state"] == "finalized" and done["part_state_counts"] == {"skipped": 8}
    assert done["report_slug"] == slug and done["report_state"] == "building"
    for forbidden in (mine_a["session_token"], mine_b["session_token"], "token_hash",
                      "finding", "evidence", "cost", "price", "listing_text"):
        assert forbidden not in response.text

    other = c.get("/api/v1/accounts/sessions", headers=bearer(second["token"]))
    assert [row["session_id"] for row in other.json()["sessions"]] == [theirs["session_id"]]


def test_account_session_read_is_read_only(app, session_factory):
    c = client(app)
    first, second = register(c), register(c)
    created = create_session(c, bearer(first["token"]))
    session_id = created["session_id"]

    assert c.get(f"/api/v1/sessions/{session_id}", headers=bearer(first["token"])).status_code == 200
    assert c.get(f"/api/v1/sessions/{session_id}", headers=bearer(second["token"])).status_code == 404
    # Writes still require the owner token; an account token grants none.
    assert c.put(f"/api/v1/sessions/{session_id}/listing", headers=bearer(first["token"]),
                 json={"listing_text": "account write"}).status_code == 401
    assert c.post(f"/api/v1/sessions/{session_id}/finalize", headers=bearer(first["token"]),
                  json={}).status_code == 401
    detail = c.get(f"/api/v1/sessions/{session_id}", headers=bearer(created["session_token"]))
    assert detail.status_code == 200 and detail.json()["listing_text"] is None


# ------------------------------------------------- report read + entitlement


def test_account_reads_linked_report_but_entitlement_still_gates_paid(ready, session_factory):
    h = ready.h
    service = AccountService(session_factory, Settings(account_pbkdf2_iterations=1000))
    owner_account = service.register(unique_email(), PASSWORD)
    other_account = service.register(unique_email(), PASSWORD)
    owner_token = service.issue_token(owner_account.id).token
    other_token = service.issue_token(other_account.id).token
    with session_factory.begin() as db:
        db.execute(update(Session).where(Session.id == h.session_id)
                   .values(account_id=owner_account.id))

    slug = report_of(ready).share_slug
    account_headers = bearer(owner_token)
    free = h.http.get(f"/api/v1/reports/{slug}", headers=account_headers)
    assert free.status_code == 200, free.text
    assert free.json()["scope"] == "free"  # account identity alone never unlocks paid
    assert not {"claims", "narration", "repairs", "scores"} & free.json().keys()
    html = h.http.get(f"/r/{slug}", headers=account_headers)
    assert html.status_code == 200
    assert h.http.get(f"/api/v1/reports/{slug}", headers=bearer(other_token)).status_code == 404
    assert h.http.get(f"/r/{slug}", headers=bearer(other_token)).status_code == 404
    assert h.http.get(f"/api/v1/sessions/{h.session_id}", headers=account_headers).status_code == 200
    assert h.http.get(f"/api/v1/sessions/{h.session_id}", headers=bearer(other_token)).status_code == 404

    with h.db.begin() as db:
        purchase = Purchase(session_id=h.session_id, report_id=report_of(ready).id, state="paid",
                            checkout_id=f"cs_synthetic_{uuid4().hex}",
                            payment_intent_id=f"pi_synthetic_{uuid4().hex}")
        db.add(purchase)
        db.flush()
        db.add(Entitlement(session_id=h.session_id, report_id=purchase.report_id,
                           source_purchase_id=purchase.id))
    paid = h.http.get(f"/api/v1/reports/{slug}", headers=account_headers)
    assert paid.status_code == 200 and paid.json()["scope"] == "paid"
    assert paid.json()["claims"] and paid.json()["repairs"]
    # Entitlement does not widen account scope to other accounts.
    assert h.http.get(f"/api/v1/reports/{slug}", headers=bearer(other_token)).status_code == 404
    with session_factory() as db:
        assert db.get(Session, h.session_id).account_id == owner_account.id
