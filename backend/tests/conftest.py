"""Public test environment: explicit local-only identity and throwaway Postgres."""

from __future__ import annotations

import asyncio
import os
import time

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
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db() -> Generator[None, None, None]:
    # TRUNCATE takes AccessExclusive locks across the FK cascade. A live stop
    # task from the preceding TestClient lifespan can otherwise hold a
    # conflicting lock on one of those tables. The client fixture drains those
    # tasks before lifespan shutdown; this bounded retry is only a final guard
    # against transient PostgreSQL deadlocks/lock contention.
    for attempt in range(4):
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                conn.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
            break
        except OperationalError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if sqlstate not in {"40P01", "55P03"} or attempt == 3:
                raise
            time.sleep(0.05 * (2**attempt))
    try:
        yield
    finally:
        # All request and background sessions must be closed before retiring
        # the test engine's pooled connections.
        engine.dispose()


async def _wait_for_background_work() -> None:
    """Let route-scheduled work finish while TestClient's event loop is alive."""
    from app.live.runtime import _BACKGROUND

    deadline = asyncio.get_running_loop().time() + 70.0
    while pending := tuple(task for task in _BACKGROUND if not task.done()):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"background tasks did not finish: {len(pending)} still pending")
        await asyncio.wait_for(
            asyncio.gather(*pending, return_exceptions=True), timeout=remaining
        )


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        try:
            yield c
        finally:
            # In particular, await stop_in_background() tasks before the
            # application lifespan closes its loop. Cancelling a task while it
            # is in anyio.to_thread.run_sync does not stop the DB work in that
            # worker thread, which can race the next test's TRUNCATE.
            if c.portal is not None:
                c.portal.call(_wait_for_background_work)


@pytest.fixture()
def auth_client(client: TestClient) -> TestClient:
    """Explicit dev identity from forwarded-auth's local-only two-step mode."""
    return client
