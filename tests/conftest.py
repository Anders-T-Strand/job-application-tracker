import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db
from app.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        secrets_dir=tmp_path / "secrets",
        ms_client_id="test-client",
        llm_provider="anthropic",  # never reach for a real local Ollama from tests
        anthropic_api_key="test-key",
        poll_enabled=False,
        discovery_enabled=False,
    )


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = db.connect(settings.db_path)
    db.migrate(connection)
    yield connection
    connection.close()
