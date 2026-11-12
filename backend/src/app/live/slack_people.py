"""No directory/Slack identity binding in the standalone core.

An operator may replace this module in a private integration overlay. Never
resolve or send personal data to third-party services by default.
"""

from __future__ import annotations

from typing import Any


def cached_fields(email: str | None) -> dict[str, None]:
    return {"slack_id": None, "slack_name": None}


def warm(emails: list[str], wait_s: float = 0.0) -> None:
    return None


def from_user(user: dict[str, Any] | None) -> None:
    return None


def remember(email: str | None, person: Any, ttl: float = 3600) -> None:
    return None
