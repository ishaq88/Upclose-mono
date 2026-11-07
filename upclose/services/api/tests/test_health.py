import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from app.readiness import database_ready
from app.settings import Settings


def test_liveness_does_not_probe_dependencies() -> None:
    def forbidden_probe(settings: Settings) -> bool:
        raise AssertionError("liveness must not probe dependencies")

    with TestClient(create_app(database_probe=forbidden_probe)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/health").status_code == 200


def test_ready_when_dependencies_pass() -> None:
    with TestClient(create_app(database_probe=lambda settings: True)) as client:
        assert client.get("/ready").status_code == 200
        assert client.get("/ready").json() == {"status": "ready"}


@pytest.mark.parametrize("raises", [False, True])
def test_unavailable_database_is_sanitized(raises: bool) -> None:
    def unavailable(settings: Settings) -> bool:
        if raises:
            raise RuntimeError("secret database password")
        return False

    with TestClient(create_app(database_probe=unavailable)) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["request_id"]
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"s3_bucket": ""},
        {"s3_internal_endpoint": "invalid"},
        {"s3_public_endpoint": "http://user:password@localhost:4566"},
        {"aws_access_key_id": ""},
        {"aws_secret_access_key": ""},
        {"aws_default_region": ""},
    ],
)
def test_bad_storage_configuration_is_not_ready(overrides: dict[str, str]) -> None:
    settings = Settings(**overrides)
    with TestClient(create_app(settings, database_probe=lambda settings: True)) as client:
        assert client.get("/ready").status_code == 503


def test_cors_only_allows_configured_origin() -> None:
    with TestClient(create_app(database_probe=lambda settings: True)) as client:
        allowed = client.options(
            "/health",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
        )
        denied = client.options(
            "/health",
            headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "GET"},
        )
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers


def test_settings_default_fixture_and_reject_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_MODE", raising=False)
    assert Settings().model_mode == "fixture"
    with pytest.raises(ValidationError):
        Settings(model_mode="unknown")


@pytest.mark.parametrize("current,expected,ready", [((), (), True), (("old",), ("new",), False)])
def test_database_probe_compares_migration_heads(
    monkeypatch: pytest.MonkeyPatch,
    current: tuple[str, ...],
    expected: tuple[str, ...],
    ready: bool,
) -> None:
    # Test wiring only; PostgreSQL migration execution is an integration check.
    class Connection:
        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def execute(self, query: object) -> None:
            assert str(query) == "SELECT 1"

        def get_current_heads(self) -> tuple[str, ...]:
            return current

    class Engine:
        disposed = False

        def connect(self) -> Connection:
            return Connection()

        def dispose(self) -> None:
            self.disposed = True

    class Scripts:
        def get_heads(self) -> tuple[str, ...]:
            return expected

    engine = Engine()
    monkeypatch.setattr("app.readiness.create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr("app.readiness.MigrationContext.configure", lambda connection: connection)
    monkeypatch.setattr("app.readiness.ScriptDirectory.from_config", lambda config: Scripts())
    assert database_ready(Settings()) is ready
    assert engine.disposed
