"""Verified forwarded identity at every HTTP, WebSocket and MCP entry point.

The proxy and OIDC issuer are operator-supplied. Unsigned identity headers are
never trusted in production. Machine identities are denied until an operator
adds an explicit authorization policy; they cannot inherit a human's sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, WebSocket
from forwarded_auth import InvalidIdentityToken, user_from_headers
from forwarded_auth import User as ForwardedUser
from forwarded_auth import get_settings as get_auth_settings
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.models.user import User


def _persist(db: Session, identity: ForwardedUser) -> User:
    if identity.is_bot:
        raise HTTPException(403, "Machine accounts require an explicit authorization adapter.")
    email = identity.email.strip().lower()
    if not email:
        raise HTTPException(401, "Authentication required")
    admins = {
        part.strip().casefold() for part in get_settings().admin_emails.split(",") if part.strip()
    }
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=identity.name, avatar_url=identity.picture, is_admin=False)
        db.add(user)
        db.flush()
    else:
        user.name = identity.name or user.name
        user.avatar_url = identity.picture or user.avatar_url
    # Role assignments are explicit and revocable, never first-login bootstrap.
    user.is_admin = email in admins
    user.last_sign_in_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)
    user.set_gateway_context(actor=None, scope=None)
    return user


def _identity(headers: dict[str, str]) -> ForwardedUser | None:
    if get_settings().is_prod:
        auth = get_auth_settings()
        if auth.mode == "none" or not auth.require_jwt or auth.trust_proxy_headers:
            raise HTTPException(500, "Unsigned identity is forbidden in production")
    try:
        identity = user_from_headers(headers)
    except InvalidIdentityToken as exc:
        raise HTTPException(401, "Authentication required") from exc
    if identity is not None and identity.is_bot:
        raise HTTPException(403, "Machine accounts require an explicit authorization adapter.")
    return identity


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    identity = _identity(dict(request.headers))
    if identity is None:
        raise HTTPException(401, "Authentication required")
    return _persist(db, identity)


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    identity = _identity(dict(request.headers))
    return _persist(db, identity) if identity else None


def user_from_header_mapping(headers: dict[str, str]) -> User | None:
    identity = _identity(headers)
    if identity is None:
        return None
    with SessionLocal() as db:
        user = _persist(db, identity)
        db.expunge(user)
        return user


def get_ws_user(websocket: WebSocket) -> User | None:
    return user_from_header_mapping(dict(websocket.headers))
