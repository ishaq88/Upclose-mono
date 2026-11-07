"""LAN extra-origin CORS: API preflight and settings validation (DB-free)."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.settings import Settings

LAN_ORIGIN = "http://192.168.1.16:5173"


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/health",
        headers={"Origin": origin, "Access-Control-Request-Method": "PUT"},
    )


def test_extra_origins_parsed_trimmed_and_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "WEB_EXTRA_ORIGINS", f" {LAN_ORIGIN} , {LAN_ORIGIN}, http://localhost:5173 "
    )
    settings = Settings()
    assert settings.web_origin == "http://localhost:5173"
    assert settings.extra_web_origins == (LAN_ORIGIN, "http://localhost:5173")
    assert settings.cors_origins == ["http://localhost:5173", LAN_ORIGIN]


def test_extra_origins_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_EXTRA_ORIGINS", raising=False)
    settings = Settings()
    assert settings.extra_web_origins == ()
    assert settings.cors_origins == ["http://localhost:5173"]


def test_preflight_allows_lan_extra_and_primary_rejects_unlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_EXTRA_ORIGINS", LAN_ORIGIN)
    settings = Settings()
    with TestClient(create_app(settings, database_probe=lambda config: True)) as client:
        lan = _preflight(client, LAN_ORIGIN)
        primary = _preflight(client, "http://localhost:5173")
        denied = _preflight(client, "http://192.168.1.99:5173")
    assert lan.status_code == 200
    assert lan.headers["access-control-allow-origin"] == LAN_ORIGIN
    assert "PUT" in lan.headers["access-control-allow-methods"]
    assert primary.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize(
    "junk",
    [
        "not-a-url",
        "*",
        "ftp://192.168.1.16:5173",
        "http://192.168.1.16:5173/app",
        "http://192.168.1.16:5173?next=/app",
        "http://192.168.1.16:5173#frag",
        "http://user:password@192.168.1.16:5173",
        "http://",
        "http://192.168.1.16:notaport",
        f"{LAN_ORIGIN},junk",
    ],
)
def test_junk_extra_origin_fails_settings_validation(junk: str) -> None:
    with pytest.raises(ValidationError, match="WEB_EXTRA_ORIGINS"):
        Settings(web_extra_origins=junk)


def test_junk_extra_origin_env_fails_settings_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_EXTRA_ORIGINS", "http://192.168.1.16:5173/path")
    with pytest.raises(ValidationError, match="WEB_EXTRA_ORIGINS"):
        Settings()
