"""Server settings; the API process must never receive the Meta key (worker-only)."""

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import ipaddress
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _validate_web_origin(origin: str) -> str:
    """Return a normalized http(s) origin, rejecting anything with a path or credentials."""
    if any(char.isspace() for char in origin) or "\\" in origin:
        raise ValueError(
            f"Invalid WEB_EXTRA_ORIGINS origin {origin!r}: must be an http(s) origin "
            "without a path, query, or fragment (for example http://192.168.1.16:5173)"
        )
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:
        raise ValueError(
            f"Invalid WEB_EXTRA_ORIGINS origin {origin!r}: must be an http(s) origin "
            "without a path, query, or fragment (for example http://192.168.1.16:5173)"
        ) from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            f"Invalid WEB_EXTRA_ORIGINS origin {origin!r}: must be an http(s) origin "
            "without a path, query, or fragment (for example http://192.168.1.16:5173)"
        )
    host = parts.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}" if port is not None else f"{parts.scheme}://{host}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://upclose:upclose@localhost:5432/upclose"
    )
    app_base_url: str = "http://localhost:8000"
    web_origin: str = "http://localhost:5173"
    # Comma-separated extra browser origins for LAN testing, e.g.
    # WEB_EXTRA_ORIGINS=http://192.168.1.16:5173. Stored as the raw comma string
    # (pydantic-settings would otherwise expect JSON); use extra_web_origins /
    # cors_origins for the parsed list. web_origin stays the primary origin.
    web_extra_origins: str = ""
    s3_bucket: str = "upclose-local"
    s3_internal_endpoint: str = "http://localhost:4566"
    s3_public_endpoint: str = "http://localhost:4566"
    aws_access_key_id: SecretStr = SecretStr("local")
    aws_secret_access_key: SecretStr = SecretStr("local")
    aws_default_region: str = "us-east-1"
    model_mode: Literal["fixture", "live", "replay"] = "fixture"
    # Worker-only live/demo settings. Never pass MODEL_API_KEY to the api container;
    # see infra/compose.live-demo.yaml and .docs/evidence/live-demo-exception.md.
    # Caps are env-tunable with no code change: MODEL_SESSION_BUDGET_CENTS / MODEL_DAILY_BUDGET_CENTS.
    model_api_key: SecretStr | None = None
    meta_api_base: str = "https://api.meta.ai"
    model_price_version: str = "meta-docs-2026-10-28"
    model_session_budget_cents: int = Field(default=500, ge=0, le=100000)
    model_daily_budget_cents: int = Field(default=5000, ge=0, le=1000000)
    model_standard_access_receipt: str | None = None
    model_allow_non_standard_demo: bool = False
    public_sharing_enabled: bool = False
    demo_session_id: UUID | None = None
    consent_version: str = "upclose-prototype-v1"
    session_creation_limit_per_day: int = Field(default=3, ge=1, le=1000)
    session_upload_limit_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    upload_url_ttl_seconds: int = Field(default=15 * 60, ge=1, le=7 * 24 * 3600)
    upload_url_refresh_limit_per_minute: int = Field(default=30, ge=1, le=10000)
    upload_orphan_expiry_seconds: int = Field(default=24 * 3600, ge=1)
    upload_completing_grace_seconds: int = Field(default=60, ge=0)
    upload_reconcile_interval_seconds: int = Field(default=60, ge=1)
    # Ingest scratch: empty means a private directory under the system temp dir. Stale scratch is swept
    # only when older than this age, which must exceed the 10-minute stage deadline.
    ingest_scratch_root: str = ""
    ingest_scratch_sweep_age_seconds: int = Field(default=30 * 60, ge=660)
    # Pipeline orchestration (T05b). The reconciler runs inside the worker loop, bounded per sweep.
    pipeline_reconcile_interval_seconds: int = Field(default=15, ge=1)
    pipeline_reconcile_batch: int = Field(default=25, ge=1, le=1000)
    pipeline_proxy_max_bytes: int = Field(default=256 * 1024 * 1024, ge=1)  # bound on a proxy re-download
    trusted_proxy_hosts: tuple[str, ...] = ()
    operator_token: SecretStr | None = None
    # Billing (T09b): fixture by default; "test" needs an sk_test_/rk_test_ key. Live keys are refused.
    # Dev-demo only: DEMO_FREE_UNLOCK=true (fixture payments only) grants a labeled demo
    # entitlement from the Unlock button with no charge. Default off; refused in test mode.
    demo_free_unlock: bool = False
    payments_mode: Literal["fixture", "test"] = "fixture"
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    # Accounts (T15a/T15b): token lifetime, login throttle, and PBKDF2 cost.
    account_token_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=1)
    account_login_max_failures: int = Field(default=5, ge=1)
    account_pbkdf2_iterations: int = Field(default=600_000, ge=1)
    # Email (password reset): fixture by default writes to a local dev outbox file.
    # "live" activates SMTP only when host and sender are both configured; any
    # partial live config falls back to the dev outbox (see app.adapters.email).
    email_mode: Literal["fixture", "live"] = "fixture"
    email_outbox_path: str = ""
    email_from_address: str | None = None
    email_smtp_host: str | None = None
    email_smtp_port: int = Field(default=587, ge=1, le=65535)
    email_smtp_username: str | None = None
    email_smtp_password: SecretStr | None = None
    email_smtp_use_tls: bool = True
    stripe_report_price_id: str | None = None
    report_price_cents: Literal[1900] = 1900  # the purchases table check currently fixes $19.00

    @field_validator("stripe_secret_key", "stripe_webhook_secret", "stripe_report_price_id", "demo_session_id",
                      "model_api_key", "model_standard_access_receipt", "email_from_address", "email_smtp_host",
                      "email_smtp_username", "email_smtp_password", mode="before")
    @classmethod
    def blank_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("web_extra_origins", mode="before")
    @classmethod
    def _coerce_web_extra_origins(cls, value: object) -> object:
        # Accept a sequence when constructed in code; env input arrives as a comma string.
        if isinstance(value, (list, tuple)):
            return ",".join(str(item) for item in value)
        return value

    @field_validator("web_extra_origins")
    @classmethod
    def _validate_web_extra_origins(cls, value: str) -> str:
        origins = [_validate_web_origin(part.strip()) for part in value.split(",") if part.strip()]
        return ",".join(dict.fromkeys(origins))

    @property
    def extra_web_origins(self) -> tuple[str, ...]:
        return tuple(part for part in self.web_extra_origins.split(",") if part)

    @property
    def cors_origins(self) -> list[str]:
        return list(dict.fromkeys([self.web_origin, *self.extra_web_origins]))

    @field_validator("trusted_proxy_hosts")
    @classmethod
    def explicit_proxy_networks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            network = ipaddress.ip_network(value, strict=False)
            if network.prefixlen == 0:
                raise ValueError("Wildcard proxy trust is not permitted")
        return values
