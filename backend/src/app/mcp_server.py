"""RGS MCP surface — lets agents sit alongside a live call.

Mounted at ``/mcp`` by ``main.py`` (manifest ``surfaces: [http, mcp]``). Every
tool resolves the caller from the request headers exactly like the HTTP API
(verified forwarded/OIDC identity) and applies the same read rules.
Machine identities require a separate, explicit authorization adapter.

The agent loop: ``list_live_sessions`` → ``wait_live_events`` (long-poll) →
on a ``gate.trigger`` call ``claim_live_trigger`` → answer from the call with
``get_live_transcript`` (+ anything else the agent can reach) →
``post_live_finding`` with the ``trigger_id``. ``ask_sidekick`` asks RGS's own
LLM answerer instead.

MCP transport security and application authorization both apply.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import anyio.to_thread
from fastapi import HTTPException
from forwarded_auth.mcp import build_transport_security
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from sqlalchemy import select
from starlette.types import ASGIApp

from app.db import SessionLocal
from app.deps import user_from_header_mapping
from app.live import service
from app.live.finalize import render_transcript
from app.live.runtime import manager
from app.models.live import LiveSession
from app.models.user import User

# Connector UIs group tools by these hints; every tool MUST carry one so it is
# shown as read-only or write/delete instead of falling into "Other tools".
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


def _build_mcp() -> FastMCP:
    """FastMCP with independently configured transport security; mounted under /mcp."""
    return FastMCP(
        "rgs",
        instructions=(
            "RGS transcribes live calls. Use these tools to "
            "follow a live session, answer gate triggers and post findings the people on "
            "the call see in real time."
        ),
        streamable_http_path="/",
        transport_security=build_transport_security(),
    )


class _Caller:
    """Resolved per-call identity + DB helpers."""

    @staticmethod
    def user(ctx: Context) -> User:
        request = getattr(ctx.request_context, "request", None)
        headers = dict(request.headers) if request is not None else {}
        user = user_from_header_mapping(headers)
        if user is None:
            raise ToolError("Not signed in: configure a verified identity provider.")
        return user


async def _run(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args))
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        raise ToolError(f"{exc.status_code}: {detail}") from exc


def _with_db(fn: Callable[..., Any], *args: Any) -> Any:
    with SessionLocal() as db:
        return fn(db, *args)


def _uuid(value: str, what: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ToolError(f"{what} must be a UUID") from exc


async def list_live_sessions(ctx: Context, active: bool = True, limit: int = 20) -> list[dict]:
    """List live sessions you can read, newest first.

    ``active=True`` (default) returns only sessions still transcribing
    (status live/stopping); ``active=False`` includes finished ones.
    """
    user = _Caller.user(ctx)

    def q(db):
        stmt = select(LiveSession).where(service.readable_clause(user))
        if active:
            stmt = stmt.where(LiveSession.status.in_(("live", "stopping")))
        stmt = stmt.order_by(LiveSession.started_at.desc()).limit(max(1, min(limit, 100)))
        return [service.session_out(db, s, user, detail=False) for s in db.scalars(stmt)]

    return await _run(_with_db, q)


async def get_live_session(ctx: Context, session_id: str) -> dict:
    """One session in detail: status, speakers (with names), findings, attendees."""
    user = _Caller.user(ctx)
    sid = _uuid(session_id, "session_id")
    return await _run(
        _with_db, lambda db: service.session_out(db, service.get_readable(db, sid, user), user)
    )


async def get_live_transcript(
    ctx: Context, session_id: str, since_seq: int = 0, limit: int = 500
) -> dict:
    """Finalized transcript segments with ``seq > since_seq`` (oldest first),
    plus ``text`` rendered as "[mm:ss] Name: line" and the current interim line."""
    user = _Caller.user(ctx)
    sid = _uuid(session_id, "session_id")

    def q(db):
        s = service.get_readable(db, sid, user)
        out = service.transcript(db, s, max(0, since_seq), max(1, min(limit, 5000)))
        names = {sp["label"]: sp["display_name"] for sp in out.get("speakers") or []}
        lines = []
        for seg in out.get("segments") or []:
            total = int(float(seg.get("start_s") or 0))
            name = names.get(seg.get("speaker"), seg.get("speaker"))
            lines.append(f"[{total // 60:02d}:{total % 60:02d}] {name}: {seg.get('text')}")
        out["text"] = "\n".join(lines)
        out["interim"] = manager.broker.interim(sid)
        return out

    return await _run(_with_db, q)


async def get_final_transcript(ctx: Context, session_id: str) -> dict:
    """The stored final transcript after stop (or the live render so far)."""
    user = _Caller.user(ctx)
    sid = _uuid(session_id, "session_id")

    def q(db):
        s = service.get_readable(db, sid, user)
        text = s.final_transcript
        if text is None:
            text = render_transcript(db, s.id)
        return {
            "session_id": str(s.id),
            "status": s.status,
            "final": s.final_transcript is not None,
            "text": text or "",
        }

    return await _run(_with_db, q)


async def wait_live_events(
    ctx: Context, session_id: str, since_seq: int = 0, wait_s: float = 20.0
) -> dict:
    """Long-poll durable events after ``since_seq`` (waits up to ``wait_s`` <= 25 s).

    Returns ``{events, last_seq, status, pending_triggers, interim, gate_tick}``.
    Pass ``last_seq`` back as ``since_seq``. Durable event types:
    ``segment.final``, ``speaker.update``, ``gate.trigger``, ``gate.claimed``,
    ``finding.new``, ``session.status``.
    """
    from app.routers.live import long_poll

    user = _Caller.user(ctx)
    sid = _uuid(session_id, "session_id")
    try:
        return await long_poll(sid, user, max(0, since_seq), max(0.0, min(wait_s, 25.0)), 200)
    except HTTPException as exc:
        raise ToolError(f"{exc.status_code}: {exc.detail}") from exc


async def list_live_triggers(ctx: Context, session_id: str, status: str = "pending") -> list[dict]:
    """Gate triggers for a session (``pending`` | ``claimed`` | ``answered`` |
    ``expired`` | ``all``). Each has ``action``, ``query``, ``context_text``."""
    user = _Caller.user(ctx)
    sid = _uuid(session_id, "session_id")
    if status not in ("pending", "claimed", "answered", "expired", "all"):
        raise ToolError("status must be pending, claimed, answered, expired or all")

    def q(db):
        s = service.get_readable(db, sid, user)
        return service.list_triggers(db, s, None if status == "all" else status)

    return await _run(_with_db, q)


async def claim_live_trigger(
    ctx: Context, session_id: str, trigger_id: str, claimed_by: str = "mcp-agent"
) -> dict:
    """Claim a pending gate trigger before answering it (first claim wins).

    Returns ``{"won": bool, "trigger": {...}}``; when ``won`` is false someone
    else is already answering — do not post a finding for it.
    """
    user = _Caller.user(ctx)
    sid = _uuid(session_id, "session_id")
    tid = _uuid(trigger_id, "trigger_id")

    def q(db):
        s = service.get_readable(db, sid, user)
        won, trigger, event = service.claim_trigger(db, s, tid, user, claimed_by[:64])
        return won, trigger, event

    won, trigger, event = await _run(_with_db, q)
    if event is not None:
        manager.broker.publish(sid, event)
    return {"won": bool(won), "trigger": trigger}


async def post_live_finding(
    ctx: Context,
    session_id: str,
    title: str,
    body_md: str,
    trigger_id: str | None = None,
    related_seq: int | None = None,
    links: list[dict] | None = None,
    source_agent: str = "mcp-agent",
) -> dict:
    """Post a finding (short markdown answer) that appears live for everyone on the call.

    Keep it brief (<= 80 words). Pass the ``trigger_id`` you claimed when it
    answers a trigger; ``links`` is ``[{"title": ..., "url": ...}]`` (<= 8).
    Identical titles within a short window are de-duplicated.
    """
    user = _Caller.user(ctx)
    sid = _uuid(session_id, "session_id")
    tid = _uuid(trigger_id, "trigger_id") if trigger_id else None
    clean_links = [
        {"title": str(x.get("title") or x.get("url"))[:200], "url": str(x["url"])[:2048]}
        for x in (links or [])[:8]
        if isinstance(x, dict) and str(x.get("url") or "").startswith(("http://", "https://"))
    ]

    def q(db):
        s = service.get_readable(db, sid, user)
        return service.create_finding(
            db,
            s,
            user,
            title=title.strip()[:200] or "Finding",
            body_md=body_md.strip()[:4000],
            links=clean_links,
            related_seq=related_seq,
            trigger_id=tid,
            source_agent=(source_agent or "mcp-agent")[:64],
        )

    finding, event, _deduped = await _run(_with_db, q)
    if event is not None:
        manager.broker.publish(sid, event)
    return finding


async def ask_sidekick(ctx: Context, session_id: str, question: str) -> dict:
    """Ask RGS's built-in LLM a question about the call so far; the answer is
    also posted as a finding everyone on the call sees."""
    user = _Caller.user(ctx)
    sid = _uuid(session_id, "session_id")
    if not question.strip():
        raise ToolError("question is required")
    await _run(
        _with_db,
        lambda db: service.require_contributor(service.get_readable(db, sid, user), user),
    )
    finding, event = await _run(
        manager.responder.ask, sid, question.strip()[:1000], service_user_id(user)
    )
    if event is not None:
        manager.broker.publish(sid, event)
    return finding


def service_user_id(user: User) -> UUID | None:
    with SessionLocal() as db:
        return service.persisted_user_id(db, user)


WRITE_EXTERNAL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

# Every entry is (function, stable name, human title, connector annotations).
_TOOLS: tuple[tuple[Callable[..., Any], str, str, ToolAnnotations], ...] = (
    (list_live_sessions, "list_live_sessions", "List live sessions", READ_ONLY),
    (get_live_session, "get_live_session", "Get a live session", READ_ONLY),
    (get_live_transcript, "get_live_transcript", "Read the live transcript", READ_ONLY),
    (get_final_transcript, "get_final_transcript", "Read the final transcript", READ_ONLY),
    (wait_live_events, "wait_live_events", "Wait for live events", READ_ONLY),
    (list_live_triggers, "list_live_triggers", "List gate triggers", READ_ONLY),
    (claim_live_trigger, "claim_live_trigger", "Claim a gate trigger", WRITE),
    (post_live_finding, "post_live_finding", "Post a finding to the call", WRITE),
    (ask_sidekick, "ask_sidekick", "Ask the sidekick", WRITE_EXTERNAL),
)


def build_mcp_app() -> ASGIApp:
    """Fresh FastMCP Streamable-HTTP ASGI app with the RGS tools registered.

    A fresh instance per app (not a module singleton) keeps
    ``StreamableHTTPSessionManager.run()`` valid once per instance; the parent
    app nests this sub-app's lifespan (see ``main._lifespan``).
    """
    mcp = _build_mcp()
    for tool, name, title, tool_annotations in _TOOLS:
        mcp.tool(name=name, title=title, annotations=tool_annotations)(tool)
    return mcp.streamable_http_app()
