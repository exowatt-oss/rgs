"""Liveness + readiness checks."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db import engine

router = APIRouter(tags=["health"])
log = structlog.get_logger("health")


@router.get("/healthz")
def healthz() -> dict:
    """Liveness probe — always 200 if the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(response: Response) -> dict:
    """Readiness probe — 200 if the DB is reachable, 503 otherwise.

    The exception detail is logged server-side rather than returned in the
    response body so we don't leak hostnames, driver info, etc. to anyone
    who can hit `/readyz`.
    """
    db_ok = True

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        log.warning("readyz_db_check_failed", err=str(exc))

    if not db_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
    }
