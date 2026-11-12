"""Public test environment: explicit local-only identity and throwaway Postgres."""

from __future__ import annotations

import os

os.environ["APP_ENV"] = "test"
os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("FORWARDED_AUTH_MODE", "none")
os.environ.setdefault("FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE", "1")
os.environ.setdefault("RECAP_ENABLED", "false")
os.environ.setdefault("ASR_PROVIDER", "mock")
os.environ.setdefault("LIVE_GATE_PROVIDER", "mock")

from collections.abc import Generator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db() -> Generator[None, None, None]:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_client(client: TestClient) -> TestClient:
    """Explicit dev identity from forwarded-auth's local-only two-step mode."""
    return client
