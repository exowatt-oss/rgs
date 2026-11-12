"""Identity and logout endpoints. Authentication is verified by forwarded-auth."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from forwarded_auth import logout_url

from app.config import get_settings
from app.deps import get_current_user
from app.models.user import User
from app.schemas.user import MeOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=MeOut)
def me(current: User = Depends(get_current_user)) -> User:
    return current


@router.get("/logout")
def logout(request: Request) -> dict[str, str]:
    # An operator-configured IdP sign-out URL may clear the external session;
    # absent that, this is only an app redirect (not global sign-out).
    rd = get_settings().app_base_url or str(request.base_url)
    return {"logout_url": logout_url(redirect_to=rd)}
