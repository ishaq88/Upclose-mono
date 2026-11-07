"""Bounded dependency probes without provider calls or secret-bearing errors."""

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from app.settings import Settings

DatabaseProbe = Callable[[Settings], bool]


def alembic_config() -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def database_ready(settings: Settings) -> bool:
    engine = create_engine(
        settings.database_url.get_secret_value(),
        connect_args={"connect_timeout": 3},
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = set(MigrationContext.configure(connection).get_current_heads())
            expected = set(ScriptDirectory.from_config(alembic_config()).get_heads())
            return current == expected
    finally:
        engine.dispose()


def storage_config_ready(settings: Settings) -> bool:
    endpoints = (settings.s3_internal_endpoint, settings.s3_public_endpoint)
    return bool(
        settings.s3_bucket.strip()
        and settings.aws_default_region.strip()
        and settings.aws_access_key_id.get_secret_value().strip()
        and settings.aws_secret_access_key.get_secret_value().strip()
        and all(
            (url := urlsplit(endpoint)).scheme in {"http", "https"}
            and url.hostname
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            for endpoint in endpoints
        )
    )
