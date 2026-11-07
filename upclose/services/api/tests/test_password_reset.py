"""Password-reset checks (spec: enumeration-resistant request, single-use confirm).

Pure adapter checks run anywhere. Database/HTTP checks only run against an
explicitly named disposable ``upclose_test_*`` PostgreSQL database
(``MODEL_TEST_DATABASE_URL``), matching ``test_accounts.py``; use
``.tools/verify-review.ps1``, which owns a disposable database. Never point
them at the shared development database.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.adapters.email import (FileEmailSender, SmtpEmailSender, build_email_sender,
    read_outbox)
from app.main import create_app
from app.models.schema import PasswordResetToken
from app.services.accounts import AccountService
from app.services.auth import token_hash
from app.services.password_reset import PasswordResetService, reset_token_invalid
from app.services.problems import ProblemError
from app.settings import Settings

PASSWORD = "correct-horse-10"
NEW_PASSWORD = "brand-new-password-11"
TOKEN_RE = re.compile(r"#token=([A-Za-z0-9_-]{22,256})")


def unique_email() -> str:
    return f"dealer-{uuid4().hex[:16]}@example.com"


def extract_token(message) -> str:
    match = TOKEN_RE.search(message.body)
    assert match, message.body
    return match.group(1)


# ---------------------------------------------------------------- pure checks


def test_reset_token_invalid_is_one_generic_400():
    error = reset_token_invalid()
    assert (error.status, error.code) == (400, "reset_token_invalid")


def test_file_outbox_roundtrip(tmp_path: Path):
    outbox = tmp_path / "nested" / "outbox.jsonl"
    sender = FileEmailSender(outbox)
    sender.send(to="dealer@example.com", subject="Reset", body="link #token=abc")
    (message,) = read_outbox(outbox)
    assert (message.to, message.subject) == ("dealer@example.com", "Reset")
    assert "#token=abc" in message.body
    assert read_outbox(tmp_path / "missing.jsonl") == ()


def test_email_sender_selection_fails_closed_to_outbox(tmp_path: Path):
    outbox = tmp_path / "outbox.jsonl"
    fixture = build_email_sender(Settings(email_outbox_path=str(outbox)))
    assert isinstance(fixture, FileEmailSender)
    # Partial live config stays on the dev outbox, never a half-live sender.
    partial = build_email_sender(Settings(
        email_mode="live", email_outbox_path=str(outbox), email_smtp_host="smtp.example.com"))
    assert isinstance(partial, FileEmailSender)
    partial_no_host = build_email_sender(Settings(
        email_mode="live", email_outbox_path=str(outbox),
        email_from_address="noreply@example.com"))
    assert isinstance(partial_no_host, FileEmailSender)
    live = build_email_sender(Settings(
        email_mode="live", email_smtp_host="smtp.example.com",
        email_from_address="noreply@example.com"))
    assert isinstance(live, SmtpEmailSender)


# ------------------------------------------------------------ database checks


@pytest.fixture(scope="module")
def reset_factory():
    url = os.environ.get("MODEL_TEST_DATABASE_URL") or os.environ.get("ACCOUNT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MODEL_TEST_DATABASE_URL must name an isolated PostgreSQL test database")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing password-reset checks outside an explicitly named upclose_test_* PostgreSQL database")
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def outbox_path(tmp_path: Path) -> Path:
    return tmp_path / "outbox.jsonl"


@pytest.fixture
def app(reset_factory, outbox_path):
    settings = Settings(account_pbkdf2_iterations=1000, email_outbox_path=str(outbox_path))
    return create_app(settings, database_probe=lambda _: True, session_factory=reset_factory)


def client(app) -> TestClient:
    address = str(ipaddress.IPv6Address(
        int(ipaddress.IPv6Address("2001:db8::")) + (uuid4().int & ((1 << 64) - 1))))
    return TestClient(app, client=(address, 12345))


def register(app_client: TestClient, email: str) -> dict:
    response = app_client.post("/api/v1/accounts/register",
                               json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


def request_reset(app_client: TestClient, email: str):
    return app_client.post("/api/v1/accounts/password-reset/request", json={"email": email})


def confirm(app_client: TestClient, token: str, new_password: str = NEW_PASSWORD):
    return app_client.post("/api/v1/accounts/password-reset/confirm",
                           json={"token": token, "new_password": new_password})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def test_unknown_and_malformed_email_202_no_outbox(app, outbox_path):
    c = client(app)
    for email in [unique_email(), "not-an-email", ""]:
        response = request_reset(c, email)
        assert response.status_code == 202 and response.content == b""
    assert read_outbox(outbox_path) == ()


def test_disabled_account_202_no_outbox(app, reset_factory, outbox_path):
    c = client(app)
    email = unique_email()
    body = register(c, email)
    AccountService(reset_factory, Settings(account_pbkdf2_iterations=1000)).set_disabled(
        body["account"]["id"], True)
    response = request_reset(c, email)
    assert response.status_code == 202 and response.content == b""
    assert read_outbox(outbox_path) == ()


def test_known_email_sends_fragment_link_and_never_logs_token(app, outbox_path, caplog):
    c = client(app)
    email = unique_email()
    register(c, email)
    with caplog.at_level(logging.DEBUG):
        response = request_reset(c, email)
    assert response.status_code == 202 and response.content == b""
    (message,) = read_outbox(outbox_path)
    assert message.to == email
    token = extract_token(message)
    assert "/reset-password#token=" in message.body
    assert token not in caplog.text


def test_token_hash_only_persisted(app, reset_factory, outbox_path):
    c = client(app)
    email = unique_email()
    register(c, email)
    request_reset(c, email)
    token = extract_token(read_outbox(outbox_path)[0])
    with reset_factory() as db:
        rows = db.scalars(select(PasswordResetToken)).all()
        mine = [r for r in rows if r.token_hash == token_hash(token)]
        assert len(mine) == 1 and mine[0].used_at is None
        assert all(r.token_hash != token for r in rows)


def test_re_request_invalidates_old_token(app, outbox_path):
    c = client(app)
    email = unique_email()
    register(c, email)
    request_reset(c, email)
    request_reset(c, email)
    first, second = (extract_token(m) for m in read_outbox(outbox_path))
    assert first != second
    stale = confirm(c, first)
    assert stale.status_code == 400 and stale.json()["code"] == "reset_token_invalid"
    assert confirm(c, second).status_code == 204


def test_expired_reused_and_tampered_tokens_rejected(app, reset_factory, outbox_path):
    c = client(app)
    email = unique_email()
    register(c, email)
    request_reset(c, email)
    token = extract_token(read_outbox(outbox_path)[0])
    with reset_factory.begin() as db:
        db.execute(text("UPDATE password_reset_tokens SET created_at = now() - interval '2 hours', "
                        "expires_at = now() - interval '1 minute' "
                        "WHERE token_hash = :h"), {"h": token_hash(token)})
    expired = confirm(c, token)
    assert expired.status_code == 400 and expired.json()["code"] == "reset_token_invalid"

    request_reset(c, email)
    token = extract_token(read_outbox(outbox_path)[-1])
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert confirm(c, tampered).status_code == 400
    assert confirm(c, "x" * 43).json()["code"] == "reset_token_invalid"
    assert confirm(c, token).status_code == 204
    reused = confirm(c, token)
    assert reused.status_code == 400 and reused.json()["code"] == "reset_token_invalid"


def test_weak_password_problem_and_token_survives(app, outbox_path):
    c = client(app)
    email = unique_email()
    register(c, email)
    request_reset(c, email)
    token = extract_token(read_outbox(outbox_path)[0])
    weak = confirm(c, token, "short")
    assert weak.status_code == 422 and weak.json()["code"] == "VALIDATION"
    assert confirm(c, token).status_code == 204  # same link still usable


def test_reset_revokes_bearer_and_old_password(app, outbox_path):
    c = client(app)
    email = unique_email()
    old_bearer = register(c, email)["token"]
    request_reset(c, email)
    token = extract_token(read_outbox(outbox_path)[0])
    assert confirm(c, token).status_code == 204
    assert c.get("/api/v1/accounts/me", headers=bearer(old_bearer)).status_code == 401
    assert c.post("/api/v1/accounts/login",
                  json={"email": email, "password": PASSWORD}).status_code == 401
    logged = c.post("/api/v1/accounts/login", json={"email": email, "password": NEW_PASSWORD})
    assert logged.status_code == 200
    assert c.get("/api/v1/accounts/me", headers=bearer(logged.json()["token"])).status_code == 200


def test_throttle_by_source_sends_nothing_over_limit(app, outbox_path):
    c = client(app)  # one source for all six accounts
    emails = [unique_email() for _ in range(6)]
    for email in emails:
        register(client(app), email)
    for email in emails:
        assert request_reset(c, email).status_code == 202
    assert len(read_outbox(outbox_path)) == 5


def test_throttle_by_email_sends_nothing_over_limit(app, outbox_path):
    email = unique_email()
    register(client(app), email)
    for _ in range(6):  # a fresh source each time; the email bucket governs
        assert request_reset(client(app), email).status_code == 202
    assert len(read_outbox(outbox_path)) == 5


def test_reset_clears_login_failure_throttle(app, reset_factory, outbox_path):
    email = unique_email()
    c = client(app)
    register(c, email)
    service = AccountService(reset_factory, Settings(account_pbkdf2_iterations=1000))
    for _ in range(5):
        with pytest.raises(ProblemError) as caught:
            service.login(email, "wrong-password-1")
        assert caught.value.code == "INVALID_CREDENTIALS"
    with pytest.raises(ProblemError) as caught:
        service.login(email, PASSWORD)
    assert caught.value.code == "RATE_LIMITED"
    request_reset(c, email)
    token = extract_token(read_outbox(outbox_path)[0])
    assert confirm(c, token).status_code == 204
    assert service.login(email, NEW_PASSWORD).account.email == email


def test_concurrent_confirms_succeed_exactly_once(app, reset_factory, outbox_path):
    email = unique_email()
    register(client(app), email)
    request_reset(client(app), email)
    token = extract_token(read_outbox(outbox_path)[0])
    settings = Settings(account_pbkdf2_iterations=1000, email_outbox_path=str(outbox_path))
    service = PasswordResetService(reset_factory, settings, FileEmailSender(outbox_path))

    def attempt() -> str:
        try:
            service.confirm_reset(token, NEW_PASSWORD)
            return "ok"
        except ProblemError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _: attempt(), range(2)))
    assert results == ["ok", "reset_token_invalid"]
    # The winning password works exactly as set.
    accounts = AccountService(reset_factory, Settings(account_pbkdf2_iterations=1000))
    assert accounts.login(email, NEW_PASSWORD).account.email == email


def test_disabled_account_confirm_rejected(app, reset_factory, outbox_path):
    c = client(app)
    email = unique_email()
    body = register(c, email)
    request_reset(c, email)
    token = extract_token(read_outbox(outbox_path)[0])
    AccountService(reset_factory, Settings(account_pbkdf2_iterations=1000)).set_disabled(
        body["account"]["id"], True)
    response = confirm(c, token)
    assert response.status_code == 400 and response.json()["code"] == "reset_token_invalid"


def test_migration_roundtrip_one_step(reset_factory):
    url = os.environ.get("MODEL_TEST_DATABASE_URL") or os.environ.get("ACCOUNT_TEST_DATABASE_URL")
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url)
    try:
        subprocess.run([sys.executable, "-m", "alembic", "downgrade", "9f3a7c1e8b42"],
                       check=True, env=env)
        with engine.connect() as connection:
            assert "password_reset_tokens" not in set(inspect(connection).get_table_names())
    finally:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
        engine.dispose()
    fresh = create_engine(url)
    try:
        with fresh.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert "password_reset_tokens" in tables
            columns = {c["name"] for c in inspect(connection).get_columns("password_reset_tokens")}
            assert {"id", "account_id", "token_hash", "expires_at", "used_at"} <= columns
    finally:
        fresh.dispose()
    subprocess.run([sys.executable, "-m", "alembic", "check"], check=True, env=env)
