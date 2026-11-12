"""Disabled recap integration seam for the standalone export.

The live pipeline and transcript remain available. Rendering, delivery and
people-sharing require a separate operator-maintained integration adapter.
No call content is sent to a third-party service by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.models.live import LiveSession

INFLIGHT: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecapSettings:
    recap_enabled: bool = False


def get_recap_settings() -> RecapSettings:
    return RecapSettings()


def clean_channels(value: Any) -> list[str]:
    if value:
        raise ValueError("Recap delivery requires an integration adapter.")
    return []


def config_out() -> dict[str, Any]:
    return {
        "enabled": False,
        "newspapers": False,
        "delivery": {"slack": False, "email": False},
        "share_with_participants": False,
    }


def recap_out(s: LiveSession) -> dict[str, Any]:
    return {
        "enabled": False,
        "status": None,
        "deliver_via": [],
        "headline": None,
        "summary": None,
        "has_notes": False,
        "edition": None,
        "delivery": {},
        "share_with_participants": False,
        "shared": [],
        "error": None,
        "updated_at": None,
    }


def participants_out(db: Any, session: LiveSession) -> list[dict[str, Any]]:
    return []


def build_meta(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {}


def notes_markdown(notes: dict[str, Any], meta: dict[str, Any]) -> str:
    return ""


def schedule(session_id: UUID) -> None:
    return None


async def resume_inflight() -> int:
    return 0
