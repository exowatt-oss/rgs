"""User-shaped response schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MeOut(BaseModel):
    """Trimmed user shape returned to the frontend."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    name: str | None = None
    avatar_url: str | None = None
    is_admin: bool = False
    created_at: datetime
    last_sign_in_at: datetime | None = None
    actor: str | None = None
    gateway_scope: str | None = None
