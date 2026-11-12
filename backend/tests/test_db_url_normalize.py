"""Smoke tests for ``Settings.normalize_database_driver``.

The platform's ``services: ["postgres"]`` auto-provisioning currently injects
``DATABASE_URL`` already in SQLAlchemy dialect form
(``postgresql+psycopg://...``). The model validator is defence-in-depth — a
plain ``postgresql://`` from a local ``.env`` (or a future contract change)
still ends up at ``postgresql+psycopg://`` so SQLAlchemy uses the psycopg
(3) dialect we ship instead of its default psycopg2.
"""

from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def _clean_db() -> None:
    """Override conftest's autouse `_clean_db` — these tests don't touch Postgres,
    so we don't want to skip them when the dev DB isn't running."""
    return None


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Platform-injected dialect form — pass through unchanged (idempotent).
        (
            "postgresql+psycopg://u:p@host:5432/db",
            "postgresql+psycopg://u:p@host:5432/db",
        ),
        # Plain libpq form from a local .env — rewrite to dialect form.
        (
            "postgresql://u:p@host:5432/db",
            "postgresql+psycopg://u:p@host:5432/db",
        ),
        # Short postgres:// scheme — also rewrite to the full dialect form.
        (
            "postgres://u:p@host:5432/db",
            "postgresql+psycopg://u:p@host:5432/db",
        ),
    ],
)
def test_normalize_database_driver(raw: str, expected: str) -> None:
    s = Settings(database_url=raw)
    assert s.database_url == expected


def test_normalize_database_driver_is_idempotent() -> None:
    """Re-instantiating with the normalised URL is a no-op."""
    once = Settings(database_url="postgresql://u:p@host:5432/db").database_url
    twice = Settings(database_url=once).database_url
    assert once == twice == "postgresql+psycopg://u:p@host:5432/db"
