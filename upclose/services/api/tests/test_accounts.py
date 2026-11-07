"""T15a account core checks.

Pure hashing/validation checks run anywhere. Database checks follow the
``test_models.py`` / ``test_sessions.py`` pattern: they only run against
an explicitly named disposable ``upclose_test_*`` PostgreSQL database
(``ACCOUNT_TEST_DATABASE_URL``, falling back to ``MODEL_TEST_DATABASE_URL``)
and skip otherwise. Never point them at the shared development database;
use ``.tools/verify-review.ps1`` or ``scripts/verify-local.ps1``, which
own disposable databases.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models.schema import Account, AccountToken, Session
from app.services.account_auth import account_bearer_token, token_invalid
from app.services.accounts import (DEFAULT_PBKDF2_ITERATIONS, AccountService,
    hash_password, invalid_credentials, normalize_email, validate_password, verify_password)
from app.services.auth import token_hash
from app.services.problems import ProblemError
from app.settings import Settings

PASSWORD = "correct-horse-10"
NEW_PASSWORD = "brand-new-password-11"


class FastSettings:
    """Settings stand-in: the service only reads these optional attributes."""

    def __init__(self, *, iterations: int = 1000, ttl: int = 3600, max_failures: int = 5):
        self.account_pbkdf2_iterations = iterations
        self.account_token_ttl_seconds = ttl
        self.account_login_max_failures = max_failures


def unique_email() -> str:
    return f"dealer-{uuid4().hex[:16]}@example.com"


# ---------------------------------------------------------------- pure checks


def test_hash_format_encodes_params_and_random_salt():
    encoded, salt_hex = hash_password(PASSWORD)
    scheme, iterations, digest_hex = encoded.split("$")
    assert scheme == "pbkdf2_sha256"
    assert int(iterations) == DEFAULT_PBKDF2_ITERATIONS >= 600_000
    assert len(digest_hex) == 64 and len(salt_hex) == 64
    other, other_salt = hash_password(PASSWORD)
    assert (other, other_salt) != (encoded, salt_hex)  # per-call random salt


def test_verify_password_roundtrip_and_malformed():
    encoded, salt_hex = hash_password(PASSWORD, iterations=1000)
    assert verify_password(PASSWORD, encoded, salt_hex)
    assert not verify_password("wrong-password", encoded, salt_hex)
    assert not verify_password(PASSWORD, "not-a-hash", salt_hex)
    assert not verify_password(PASSWORD, "argon2$1000$abcd", salt_hex)
    # Stored iteration count governs verification, not the current default.
    assert encoded.startswith("pbkdf2_sha256$1000$")


def test_normalize_email_and_password_policy():
    assert normalize_email("  Dealer@Example.COM ") == "dealer@example.com"
    for bad in ["", "no-at-sign", "a@b", "a b@c.d", "@x.y", "a@.y", None, 42]:
        with pytest.raises(ProblemError) as caught:
            normalize_email(bad)  # type: ignore[arg-type]
        assert caught.value.code == "VALIDATION"
    validate_password("1234567890")
    with pytest.raises(ProblemError) as caught:
        validate_password("short")
    assert caught.value.code == "VALIDATION"


def test_reprs_and_errors_never_leak_secrets():
    account = Account(email="dealer@example.com", password_hash="pbkdf2_sha256$1$deadbeef",
                      password_salt="ab" * 32)
    assert "dealer@example.com" not in repr(account)
    assert "deadbeef" not in repr(account) and "ab" * 32 not in repr(account)
    error = invalid_credentials()
    assert PASSWORD not in error.detail and "deadbeef" not in error.detail


def test_bearer_parsing_and_settings_defaults():
    assert account_bearer_token("Bearer " + "a" * 43) == "a" * 43
    for header in [None, "", "Token abc", "Bearer short", "Bearer has space"]:
        with pytest.raises(ProblemError) as caught:
            account_bearer_token(header)
        assert (caught.value.status, caught.value.code) == (401, "TOKEN_INVALID")
    assert token_invalid().code == "TOKEN_INVALID"
    service = AccountService(sessionmaker(), Settings())  # type: ignore[arg-type]
    assert service.pbkdf2_iterations >= 600_000
    assert service.token_ttl_seconds == 30 * 24 * 3600
    assert service.login_max_failures >= 1


# ------------------------------------------------------------ database checks


@pytest.fixture(scope="module")
def account_factory():
    url = os.environ.get("ACCOUNT_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ACCOUNT_TEST_DATABASE_URL must name an isolated PostgreSQL test database")
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or not (parsed.database or "").startswith("upclose_test_"):
        pytest.fail("Refusing account checks outside an explicitly named upclose_test_* PostgreSQL database")
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
    engine = create_engine(url)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def make_service(account_factory, **overrides) -> AccountService:
    return AccountService(account_factory, FastSettings(**overrides))  # type: ignore[arg-type]


def error_tuple(error: ProblemError) -> tuple:
    return (error.status, error.code, error.title, error.detail)


def test_register_login_happy_path(account_factory):
    service = make_service(account_factory)
    email = unique_email()
    account = service.register("  " + email.upper() + " ", PASSWORD)
    assert account.email == email and not account.disabled
    assert account.password_hash.startswith("pbkdf2_sha256$1000$")
    assert PASSWORD not in account.password_hash
    result = service.login(email, PASSWORD)
    assert result.account.id == account.id
    assert result.issued.token and result.issued.token not in repr(result.issued)
    assert service.verify_token(result.issued.token).id == account.id
    with account_factory() as db:  # only the hash persists
        row = db.scalar(select(AccountToken).where(AccountToken.id == result.issued.token_id))
        assert row is not None and row.token_hash == token_hash(result.issued.token)
        assert row.token_hash != result.issued.token and row.revoked_at is None


def test_duplicate_normalized_email_rejected(account_factory):
    service = make_service(account_factory)
    email = unique_email()
    service.register(email, PASSWORD)
    with pytest.raises(ProblemError) as caught:
        service.register(email.upper(), NEW_PASSWORD)
    assert error_tuple(caught.value)[1] == "EMAIL_TAKEN"
    assert caught.value.status == 409


def test_wrong_password_matches_unknown_email_error(account_factory):
    service = make_service(account_factory)
    email = unique_email()
    service.register(email, PASSWORD)
    with pytest.raises(ProblemError) as wrong:
        service.login(email, "wrong-password-1")
    with pytest.raises(ProblemError) as unknown:
        service.login(unique_email(), "wrong-password-1")
    assert error_tuple(wrong.value) == error_tuple(unknown.value)
    assert error_tuple(wrong.value)[1] == "INVALID_CREDENTIALS"


def test_token_expiry_and_revocation(account_factory):
    service = make_service(account_factory)
    email = unique_email()
    account = service.register(email, PASSWORD)
    issued = service.login(email, PASSWORD).issued
    with account_factory.begin() as db:
        db.execute(text("UPDATE account_tokens SET created_at = now() - interval '2 days', "
                        "expires_at = now() - interval '1 day' WHERE id = :token_id"),
                   {"token_id": issued.token_id})
    with pytest.raises(ProblemError) as caught:
        service.verify_token(issued.token)
    assert caught.value.code == "TOKEN_INVALID"

    second = service.issue_token(account.id)
    assert service.verify_token(second.token).id == account.id
    assert service.revoke_token(second.token) is True
    assert service.revoke_token(second.token) is False  # idempotent
    with pytest.raises(ProblemError):
        service.verify_token(second.token)


def test_password_change_revokes_other_tokens(account_factory):
    service = make_service(account_factory)
    email = unique_email()
    account = service.register(email, PASSWORD)
    first = service.login(email, PASSWORD).issued
    second = service.issue_token(account.id)
    with pytest.raises(ProblemError) as caught:
        service.change_password(account.id, "wrong-current-1", NEW_PASSWORD)
    assert caught.value.code == "INVALID_CREDENTIALS"
    service.change_password(account.id, PASSWORD, NEW_PASSWORD, keep_token=first.token)
    assert service.verify_token(first.token).id == account.id  # kept session survives
    with pytest.raises(ProblemError):
        service.verify_token(second.token)  # other tokens revoked
    with pytest.raises(ProblemError):
        service.login(email, PASSWORD)  # old password dead
    assert service.login(email, NEW_PASSWORD).account.id == account.id


def test_login_rate_limit_throttles_after_failures(account_factory):
    service = make_service(account_factory, max_failures=3)
    email = unique_email()
    service.register(email, PASSWORD)
    for _ in range(3):
        with pytest.raises(ProblemError) as caught:
            service.login(email, "wrong-password-1")
        assert caught.value.code == "INVALID_CREDENTIALS"
    with pytest.raises(ProblemError) as caught:  # even correct password is throttled
        service.login(email, PASSWORD)
    assert (caught.value.status, caught.value.code) == (429, "RATE_LIMITED")
    assert caught.value.retry_after and caught.value.retry_after > 0
    # A different email has its own bucket and is unaffected.
    other = unique_email()
    service.register(other, PASSWORD)
    assert service.login(other, PASSWORD).account.email == other


def test_disabled_account_cannot_login_or_use_tokens(account_factory):
    service = make_service(account_factory)
    email = unique_email()
    account = service.register(email, PASSWORD)
    issued = service.login(email, PASSWORD).issued
    service.set_disabled(account.id, True)
    with pytest.raises(ProblemError) as caught:
        service.verify_token(issued.token)  # disabling revoked it
    assert caught.value.code == "TOKEN_INVALID"
    with pytest.raises(ProblemError) as caught:
        service.login(email, PASSWORD)
    assert caught.value.code == "INVALID_CREDENTIALS"


def test_session_account_link_is_nullable(account_factory):
    service = make_service(account_factory)
    account = service.register(unique_email(), PASSWORD)
    suffix = uuid4().hex
    with account_factory.begin() as db:
        anonymous = Session(token_hash=suffix * 2, consent_version="test-v1",
                            consent_at=datetime.now(timezone.utc))
        owned = Session(token_hash=uuid4().hex * 2, consent_version="test-v1",
                        consent_at=datetime.now(timezone.utc), account_id=account.id)
        db.add_all([anonymous, owned])
        db.flush()
        assert anonymous.account_id is None  # pre-account rows stay anonymous
        assert owned.account_id == account.id


def test_migration_roundtrip_one_step(account_factory):
    url = os.environ.get("ACCOUNT_TEST_DATABASE_URL") or os.environ.get("MODEL_TEST_DATABASE_URL")
    env = {**os.environ, "DATABASE_URL": url}
    engine = create_engine(url)
    try:
        subprocess.run([sys.executable, "-m", "alembic", "downgrade", "f7b1d4e5c0a6"],
                       check=True, env=env)
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert "accounts" not in tables and "account_tokens" not in tables
            columns = {c["name"] for c in inspect(connection).get_columns("sessions")}
            assert "account_id" not in columns
            # Existing data survives the downgrade path untouched in shape.
            assert connection.scalar(text("SELECT count(*) FROM sessions")) is not None
    finally:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env)
        engine.dispose()
    fresh = create_engine(url)
    try:
        with fresh.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert {"accounts", "account_tokens"} <= tables
            columns = {c["name"] for c in inspect(connection).get_columns("sessions")}
            assert "account_id" in columns
    finally:
        fresh.dispose()
    subprocess.run([sys.executable, "-m", "alembic", "check"], check=True, env=env)
