"""Password reset: enumeration-resistant request and single-use confirm.

Security invariants:
- Request always succeeds identically (202 at the HTTP layer) for unknown,
  disabled, malformed, and throttled input; only a live account gets mail.
- Only the SHA-256 of the reset token persists. The plaintext token leaves
  the process once, inside the emailed link fragment, and is never logged.
- Confirm consumes the token with one conditional UPDATE so concurrent
  confirms succeed exactly once, then revokes every account bearer token
  and clears the account's login-failure throttle.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session as DBSession, sessionmaker

from app.adapters.email import EmailSender
from app.models.rate_limits import RateLimitBucket
from app.models.schema import Account, AccountToken, PasswordResetToken
from app.settings import Settings

from .accounts import AccountService, hash_password, normalize_email, validate_password
from .auth import token_hash
from .problems import ProblemError

logger = logging.getLogger(__name__)

RESET_TOKEN_TTL = timedelta(minutes=30)
RESET_LIMIT_PER_HOUR = 5
_TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22,256}")


def reset_token_invalid() -> ProblemError:
    """The single reset-token failure; never reveals which check failed."""
    return ProblemError(400, "reset_token_invalid", "Password reset failed",
                        "The reset link is invalid or has expired.")


def _bucket_key(domain: str, material: str) -> str:
    return hashlib.sha256(f"password-reset:{domain}:{material}".encode("utf-8")).hexdigest()


class PasswordResetService:
    def __init__(self, sessions: sessionmaker[DBSession], settings: Settings,
                 email_sender: EmailSender) -> None:
        self.sessions = sessions
        self.settings = settings
        self.email_sender = email_sender
        self._accounts = AccountService(sessions, settings)

    # -- request ------------------------------------------------------------

    def request_reset(self, email: object, source_fingerprint: str) -> None:
        """Best-effort reset issuance. Never raises for account-state reasons.

        Rate limited to 5/hour keyed by BOTH the caller source and the
        normalized email; over-limit requests silently send nothing.
        """
        try:
            normalized: str | None = normalize_email(email)  # type: ignore[arg-type]
        except ProblemError:
            normalized = None
        keys = [_bucket_key("source", source_fingerprint)]
        if normalized is not None:
            keys.append(_bucket_key("email", normalized))
        reset_url: str | None = None
        recipient: str | None = None
        with self.sessions.begin() as db:
            now = self._now(db)
            window = now.replace(minute=0, second=0, microsecond=0)
            counts = [self._increment(db, key, window) for key in keys]
            if any(count > RESET_LIMIT_PER_HOUR for count in counts):
                return
            if normalized is None:
                return
            account = db.scalar(select(Account).where(Account.email == normalized))
            if account is None or account.disabled:
                return
            db.execute(update(PasswordResetToken)
                       .where(PasswordResetToken.account_id == account.id,
                              PasswordResetToken.used_at.is_(None))
                       .values(used_at=now))
            token = secrets.token_urlsafe(_TOKEN_BYTES)
            db.add(PasswordResetToken(account_id=account.id,
                                      token_hash=token_hash(token),
                                      expires_at=now + RESET_TOKEN_TTL))
            db.flush()
            origin = (getattr(self.settings, "web_origin", "") or "").rstrip("/")
            reset_url = f"{origin}/reset-password#token={token}"
            recipient = normalized
        # Send after commit; a delivery failure must not change the response.
        try:
            self.email_sender.send(
                to=recipient or "", subject="Reset your UpClose password",
                body=("Use this link within 30 minutes to reset your password:\n\n"
                      f"{reset_url}\n\nIf you did not request this, ignore this email.\n"))
        except Exception:
            logger.warning("password reset email delivery failed")

    # -- confirm ------------------------------------------------------------

    def confirm_reset(self, token: object, new_password: object) -> None:
        """Consume ``token`` exactly once and set a new password.

        A weak new password raises the existing VALIDATION problem before
        the token is touched, so the caller may retry with the same link.
        Every token/account problem raises the one generic 400.
        """
        validate_password(new_password)  # type: ignore[arg-type]
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise reset_token_invalid()
        hashed = token_hash(token)
        with self.sessions.begin() as db:
            now = self._now(db)
            row = db.scalar(select(PasswordResetToken)
                            .where(PasswordResetToken.token_hash == hashed))
            if row is None or row.used_at is not None or row.expires_at <= now:
                raise reset_token_invalid()
            account = db.get(Account, row.account_id)
            if account is None or account.disabled:
                raise reset_token_invalid()
            # Exactly-once: only the confirm that flips used_at wins.
            consumed = db.execute(
                update(PasswordResetToken)
                .where(PasswordResetToken.id == row.id,
                       PasswordResetToken.used_at.is_(None),
                       PasswordResetToken.expires_at > now)
                .values(used_at=now)).rowcount
            if not consumed:
                raise reset_token_invalid()
            encoded, salt_hex = hash_password(
                new_password, iterations=self._accounts.pbkdf2_iterations)  # type: ignore[arg-type]
            account.password_hash = encoded
            account.password_salt = salt_hex
            db.execute(update(AccountToken)
                       .where(AccountToken.account_id == account.id,
                              AccountToken.revoked_at.is_(None))
                       .values(revoked_at=now))
            db.execute(delete(RateLimitBucket).where(
                RateLimitBucket.source_hash
                == AccountService._login_fingerprint(account.email, None)))

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _now(db: DBSession) -> datetime:
        return db.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)

    @staticmethod
    def _increment(db: DBSession, key: str, window: datetime) -> int:
        statement = pg_insert(RateLimitBucket).values(
            source_hash=key, window_start=window, count=1)
        statement = statement.on_conflict_do_update(
            index_elements=[RateLimitBucket.source_hash, RateLimitBucket.window_start],
            set_={"count": RateLimitBucket.count + 1}).returning(RateLimitBucket.count)
        return int(db.scalar(statement) or 0)
