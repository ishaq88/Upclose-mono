"""Dealer/flipper account core (T15a): registration, login, and tokens.

This module is the contract the account HTTP API (separate card) builds on.
It owns no routes. All failures are ``ProblemError`` values from
``.problems`` so routers can render them as ``application/problem+json``
without translating exceptions.

Security invariants:
- Passwords are never stored, logged, returned, or included in ``repr``.
  Only a PBKDF2-HMAC-SHA256 digest plus a per-account random salt persist.
  ``password_hash`` encodes its parameters as
  ``pbkdf2_sha256$<iterations>$<hex digest>`` so iterations can rise later;
  verification reads the stored count, not the current default.
- Account tokens are opaque (``secrets.token_urlsafe(32)``). Only their
  SHA-256 hash reaches the database, mirroring owner tokens in ``.auth``.
  The plaintext token is returned exactly once, inside ``IssuedToken``.
- Login failure is constant-work and indistinguishable: an unknown email,
  a wrong password, and a disabled account all raise the identical
  ``INVALID_CREDENTIALS`` error, and the unknown-email path still runs a
  full PBKDF2 verification against a dummy digest. Password and digest
  comparisons use ``hmac.compare_digest``.
- Repeated login failures are throttled through the shared
  ``RateLimitBucket`` table (hashed key, UTC daily window, atomic
  increment), matching ``app.services.rate_limits`` semantics: once the
  failure budget for the window is spent, further attempts -- even with
  the correct password -- fail with ``429 RATE_LIMITED`` until the window
  resets. A successful login clears the failure bucket.

Settings (all optional; sane defaults apply when unset on ``Settings``):
``account_token_ttl_seconds`` (default 30 days), ``account_login_max_failures``
(default 5 per daily window), ``account_pbkdf2_iterations`` (default 600_000;
stored per-hash, so raising it only affects new/changed passwords).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import ceil
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.models.rate_limits import RateLimitBucket
from app.models.schema import Account, AccountToken
from app.settings import Settings

from .auth import token_hash
from .problems import ProblemError, validation

DEFAULT_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days
DEFAULT_LOGIN_MAX_FAILURES = 5
DEFAULT_PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 10
_SALT_BYTES = 32  # 64 hex chars, matches the password_salt check constraint
_DUMMY_PASSWORD = "upclose-dummy-password-for-constant-work"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def invalid_credentials() -> ProblemError:
    """The one and only credential failure; never reveals which part failed."""
    return ProblemError(401, "INVALID_CREDENTIALS", "Invalid credentials",
                        "The email or password is incorrect.")


def email_taken() -> ProblemError:
    return ProblemError(409, "EMAIL_TAKEN", "Email already registered",
                        "An account with this email already exists.")


def normalize_email(email: str) -> str:
    """Return the canonical (stripped, lowercased) email or raise VALIDATION."""
    if not isinstance(email, str):
        raise validation()
    normalized = email.strip().lower()
    if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise validation()
    return normalized


def validate_password(password: str) -> None:
    """Enforce the minimum password policy or raise VALIDATION."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise validation()


def hash_password(password: str, *, salt: bytes | None = None,
                  iterations: int = DEFAULT_PBKDF2_ITERATIONS) -> tuple[str, str]:
    """Return ``(encoded_hash, salt_hex)`` for ``password``.

    ``encoded_hash`` is ``pbkdf2_sha256$<iterations>$<hex digest>``. A fresh
    random salt is generated unless one is supplied (verification/tests).
    """
    if iterations < 1:
        raise ValueError("iterations must be positive")
    raw_salt = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), raw_salt, iterations)
    return f"pbkdf2_sha256${iterations}${digest.hex()}", raw_salt.hex()


def verify_password(password: str, password_hash: str, password_salt: str) -> bool:
    """Check ``password`` against a stored encoded hash/salt pair.

    Uses the iteration count stored in ``password_hash`` and compares with
    ``hmac.compare_digest``. Malformed stored values return ``False``.
    """
    try:
        scheme, iterations, expected_hex = password_hash.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(password_salt)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), expected_hex)


@dataclass(frozen=True)
class IssuedToken:
    """A freshly issued account token. ``token`` is plaintext, shown once."""

    token: str = field(repr=False)  # never render the secret
    token_id: UUID = field()
    expires_at: datetime = field()

    def __repr__(self) -> str:
        return f"IssuedToken(token_id={self.token_id!r}, expires_at={self.expires_at!r})"


@dataclass(frozen=True)
class AuthResult:
    """Successful login outcome: the account plus its one-time token."""

    account: Account
    issued: IssuedToken


class AccountService:
    """Account registration, login, token lifecycle, and password changes.

    Construct with the app session factory and settings, mirroring
    ``SessionService``::

        service = AccountService(session_factory, Settings())
        account = service.register("Dealer@Example.com", "a-long-password")
        result = service.login("dealer@example.com", "a-long-password")
        account = service.verify_token(result.issued.token)

    ``source_key`` on :meth:`login` is an opaque caller-supplied source
    identifier (for example the hashed client address from
    ``app.services.rate_limits.source_fingerprint``); it is hashed again
    before storage and only narrows the throttle bucket. It must never be
    a password or a token.
    """

    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings) -> None:
        self.sessions = sessions
        self.settings = settings
        self._dummy_cache: tuple[int, str] | None = None  # (iterations, encoded hash)

    # -- configuration (settings attributes are optional; see module docs) --

    @property
    def token_ttl_seconds(self) -> int:
        value = getattr(self.settings, "account_token_ttl_seconds", DEFAULT_TOKEN_TTL_SECONDS)
        return value if isinstance(value, int) and value > 0 else DEFAULT_TOKEN_TTL_SECONDS

    @property
    def login_max_failures(self) -> int:
        value = getattr(self.settings, "account_login_max_failures", DEFAULT_LOGIN_MAX_FAILURES)
        return value if isinstance(value, int) and value > 0 else DEFAULT_LOGIN_MAX_FAILURES

    @property
    def pbkdf2_iterations(self) -> int:
        value = getattr(self.settings, "account_pbkdf2_iterations", DEFAULT_PBKDF2_ITERATIONS)
        return value if isinstance(value, int) and value > 0 else DEFAULT_PBKDF2_ITERATIONS

    # -- registration -------------------------------------------------------

    def register(self, email: str, password: str) -> Account:
        """Create an account. Raises VALIDATION or EMAIL_TAKEN (409)."""
        normalized = normalize_email(email)
        validate_password(password)
        encoded, salt_hex = hash_password(password, iterations=self.pbkdf2_iterations)
        account = Account(email=normalized, password_hash=encoded, password_salt=salt_hex)
        try:
            with self.sessions.begin() as db:
                db.add(account)
                db.flush()
                db.expunge(account)
        except IntegrityError:  # unique email lost a concurrent race
            raise email_taken() from None
        return account

    # -- login --------------------------------------------------------------

    def login(self, email: str, password: str, *, source_key: str | None = None) -> AuthResult:
        """Verify credentials and issue a token.

        Unknown email, wrong password, and disabled accounts all raise the
        identical ``INVALID_CREDENTIALS`` error after equivalent PBKDF2
        work. Raises ``RATE_LIMITED`` (429) once the daily failure budget
        for this email/source is spent. Malformed emails raise VALIDATION
        without touching the throttle or the database.
        """
        normalized = normalize_email(email)
        if not isinstance(password, str):
            raise invalid_credentials()
        fingerprint = self._login_fingerprint(normalized, source_key)
        failed = False
        with self.sessions.begin() as db:
            now = self._now(db)
            self._raise_if_throttled(db, fingerprint, now)
            account = db.scalar(select(Account).where(Account.email == normalized))
            if account is None:
                # Constant work: unknown emails cost one full verification.
                verify_password(password, self._dummy_hash(), self._dummy_salt())
                failed = True
            elif not verify_password(password, account.password_hash, account.password_salt):
                failed = True
            elif account.disabled:
                failed = True
            if failed:
                # Commit the throttle increment first; raising here would
                # roll it back and the throttle would never engage.
                self._record_failure(db, fingerprint, now)
            else:
                self._clear_failures(db, fingerprint, now)
                issued = self._issue(db, account, now)
                db.expunge(account)
                return AuthResult(account=account, issued=issued)
        raise invalid_credentials()

    # -- tokens -------------------------------------------------------------

    def issue_token(self, account_id: UUID) -> IssuedToken:
        """Issue an additional token for an existing, enabled account."""
        with self.sessions.begin() as db:
            account = db.get(Account, account_id)
            if account is None or account.disabled:
                raise invalid_credentials()
            return self._issue(db, account, self._now(db))

    def verify_token(self, token: str) -> Account:
        """Resolve a plaintext token to its account or raise TOKEN_INVALID.

        Expired, revoked, unknown, and disabled-account tokens are all the
        same 401; the token itself is never logged or echoed.
        """
        from .account_auth import token_invalid  # deferred: avoids a cycle

        if not isinstance(token, str) or not token:
            raise token_invalid()
        with self.sessions.begin() as db:
            row = db.scalar(select(AccountToken).where(AccountToken.token_hash == token_hash(token)))
            if row is None or row.revoked_at is not None:
                raise token_invalid()
            if row.expires_at <= self._now(db):
                raise token_invalid()
            account = db.get(Account, row.account_id)
            if account is None or account.disabled:
                raise token_invalid()
            db.expunge(account)
            return account

    def revoke_token(self, token: str) -> bool:
        """Revoke one token by plaintext. Idempotent: False if not live."""
        if not isinstance(token, str) or not token:
            return False
        with self.sessions.begin() as db:
            row = db.scalar(select(AccountToken).where(AccountToken.token_hash == token_hash(token)))
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = self._now(db)
            return True

    def revoke_all_tokens(self, account_id: UUID, *, except_token_id: UUID | None = None) -> int:
        """Revoke every live token for an account; returns the count revoked."""
        with self.sessions.begin() as db:
            return self._revoke_all(db, account_id, except_token_id=except_token_id)

    # -- password change / account state -------------------------------------

    def change_password(self, account_id: UUID, current_password: str, new_password: str,
                        *, keep_token: str | None = None) -> None:
        """Change a password after verifying the current one.

        Revokes all of the account's other live tokens; the token matching
        ``keep_token`` (plaintext, e.g. the caller's own session token)
        survives. A wrong current password raises the same
        ``INVALID_CREDENTIALS`` error as login; a weak new password raises
        VALIDATION; an unknown/disabled account raises INVALID_CREDENTIALS.
        """
        validate_password(new_password)
        if not isinstance(current_password, str):
            raise invalid_credentials()
        with self.sessions.begin() as db:
            account = db.get(Account, account_id)
            if account is None or account.disabled:
                verify_password(current_password, self._dummy_hash(), self._dummy_salt())
                raise invalid_credentials()
            if not verify_password(current_password, account.password_hash, account.password_salt):
                raise invalid_credentials()
            encoded, salt_hex = hash_password(new_password, iterations=self.pbkdf2_iterations)
            account.password_hash = encoded
            account.password_salt = salt_hex
            keep_id: UUID | None = None
            if keep_token:
                keep = db.scalar(select(AccountToken).where(
                    AccountToken.token_hash == token_hash(keep_token),
                    AccountToken.account_id == account.id))
                keep_id = keep.id if keep is not None else None
            self._revoke_all(db, account.id, except_token_id=keep_id)

    def set_disabled(self, account_id: UUID, disabled: bool) -> Account:
        """Enable/disable an account; disabling revokes all its tokens."""
        with self.sessions.begin() as db:
            account = db.get(Account, account_id)
            if account is None:
                raise invalid_credentials()
            account.disabled = disabled
            if disabled:
                self._revoke_all(db, account.id)
            db.flush()
            db.expunge(account)
            return account

    # -- internals ------------------------------------------------------------

    def _issue(self, db: DBSession, account: Account, now: datetime) -> IssuedToken:
        token = secrets.token_urlsafe(32)
        row = AccountToken(account_id=account.id, token_hash=token_hash(token),
                           expires_at=now + timedelta(seconds=self.token_ttl_seconds))
        db.add(row)
        db.flush()
        return IssuedToken(token=token, token_id=row.id, expires_at=row.expires_at)

    def _revoke_all(self, db: DBSession, account_id: UUID, *, except_token_id: UUID | None = None) -> int:
        statement = (update(AccountToken)
                     .where(AccountToken.account_id == account_id, AccountToken.revoked_at.is_(None))
                     .values(revoked_at=self._now(db)))
        if except_token_id is not None:
            statement = statement.where(AccountToken.id != except_token_id)
        return db.execute(statement).rowcount or 0

    @staticmethod
    def _now(db: DBSession) -> datetime:
        return db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)

    @staticmethod
    def _login_fingerprint(normalized_email: str, source_key: str | None) -> str:
        material = "account-login:" + normalized_email
        if source_key:
            material += ":" + source_key
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _window_start(now: datetime) -> datetime:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _raise_if_throttled(self, db: DBSession, fingerprint: str, now: datetime) -> None:
        count = db.scalar(select(RateLimitBucket.count).where(
            RateLimitBucket.source_hash == fingerprint,
            RateLimitBucket.window_start == self._window_start(now))) or 0
        if count >= self.login_max_failures:
            retry_after = max(1, ceil((self._window_start(now) + timedelta(days=1) - now).total_seconds()))
            raise ProblemError(429, "RATE_LIMITED", "Rate limit exceeded",
                               "Too many failed login attempts. Try again later.", retry_after)

    def _record_failure(self, db: DBSession, fingerprint: str, now: datetime) -> None:
        statement = pg_insert(RateLimitBucket).values(
            source_hash=fingerprint, window_start=self._window_start(now), count=1)
        statement = statement.on_conflict_do_update(
            index_elements=[RateLimitBucket.source_hash, RateLimitBucket.window_start],
            set_={"count": RateLimitBucket.count + 1})
        db.execute(statement)

    def _clear_failures(self, db: DBSession, fingerprint: str, now: datetime) -> None:
        db.execute(delete(RateLimitBucket).where(
            RateLimitBucket.source_hash == fingerprint,
            RateLimitBucket.window_start == self._window_start(now)))

    def _dummy_hash(self) -> str:
        iterations = self.pbkdf2_iterations
        if self._dummy_cache is None or self._dummy_cache[0] != iterations:
            digest = hashlib.pbkdf2_hmac("sha256", _DUMMY_PASSWORD.encode("utf-8"),
                                         bytes.fromhex(self._dummy_salt()), iterations)
            self._dummy_cache = (iterations, f"pbkdf2_sha256${iterations}${digest.hex()}")
        return self._dummy_cache[1]

    @staticmethod
    def _dummy_salt() -> str:
        return (b"\x00" * _SALT_BYTES).hex()

