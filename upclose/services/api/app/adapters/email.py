"""Email delivery behind a typed interface; only this module talks SMTP.

Default (fixture/dev) delivery writes messages to a local JSON-lines outbox
file so tests and developers can read them without a provider. A real SMTP
sender activates ONLY with explicit live settings (``email_mode="live"``
plus both an SMTP host and a sender address); absent or partial live config
stays on the dev outbox and the fallback is logged without any message
content. Message bodies, recipients, tokens, and reset links are never
logged here or by callers.
"""
from __future__ import annotations

import json
import logging
import smtplib
import tempfile
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from app.settings import Settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    def send(self, *, to: str, subject: str, body: str) -> None: ...


@dataclass(frozen=True)
class OutboxMessage:
    to: str
    subject: str
    body: str


def default_outbox_path() -> Path:
    return Path(tempfile.gettempdir()) / "upclose-dev-email-outbox.jsonl"


def resolve_outbox_path(settings: Settings) -> Path:
    configured = getattr(settings, "email_outbox_path", "")
    if isinstance(configured, str) and configured.strip():
        return Path(configured)
    return default_outbox_path()


class FileEmailSender:
    """Dev/fixture sender: appends one JSON object per line to the outbox.

    The outbox is a local development artifact, not a log; it intentionally
    contains the reset link so a developer/test can complete the flow.
    """

    is_dev_outbox = True

    def __init__(self, path: Path):
        self.path = Path(path)

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"to": to, "subject": subject, "body": body})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def read_messages(self) -> tuple[OutboxMessage, ...]:
        return read_outbox(self.path)


class SmtpEmailSender:
    """Live SMTP sender. Constructed only with complete live settings."""

    is_dev_outbox = False

    def __init__(self, *, host: str, port: int, from_address: str,
                 username: str | None = None, password: str | None = None,
                 use_tls: bool = True):
        self._host = host
        self._port = port
        self._from = from_address
        self._username = username
        self._password = password
        self._use_tls = use_tls

    def send(self, *, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self._from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self._host, self._port, timeout=15) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password or "")
            smtp.send_message(message)


def read_outbox(path: Path) -> tuple[OutboxMessage, ...]:
    """Read dev-outbox messages; a missing outbox reads as empty."""
    path = Path(path)
    if not path.exists():
        return ()
    messages: list[OutboxMessage] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        messages.append(OutboxMessage(to=str(record["to"]), subject=str(record["subject"]),
                                      body=str(record["body"])))
    return tuple(messages)


def build_email_sender(settings: Settings) -> EmailSender:
    """Return the live SMTP sender only for complete live config.

    Any absent or partial live configuration stays on the dev outbox; the
    fallback is noted in the log with no recipient or message content.
    """
    mode = getattr(settings, "email_mode", "fixture")
    host = getattr(settings, "email_smtp_host", None)
    from_address = getattr(settings, "email_from_address", None)
    if mode == "live":
        if host and from_address:
            password = getattr(settings, "email_smtp_password", None)
            return SmtpEmailSender(
                host=str(host), port=int(getattr(settings, "email_smtp_port", 587)),
                from_address=str(from_address),
                username=getattr(settings, "email_smtp_username", None),
                password=password.get_secret_value() if password is not None else None,
                use_tls=bool(getattr(settings, "email_smtp_use_tls", True)))
        logger.warning("live email config incomplete; using local dev outbox")
    return FileEmailSender(resolve_outbox_path(settings))
