"""RGS live API: authenticated sessions, live transcript and agent findings.

Sessions, raw-PCM audio ingest (WebSocket + HTTP fallback), the live event
stream (WebSocket + long-poll with `since_seq` resume), findings and gate
trigger claims. Blocking DB work in async handlers goes through
`runtime.db_call` (worker thread); plain `def` handlers already run in the
threadpool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import anyio
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user, get_ws_user
from app.live import service
from app.live.runtime import AudioGap, NotLive, db_call, manager
from app.live.settings import get_live_settings
from app.models.live import LiveSession, LiveSpeaker
from app.models.user import User

router = APIRouter(prefix="/live", tags=["live"])

MAX_WS_FRAME_BYTES = 65536
MAX_HTTP_AUDIO_BYTES = 1024 * 1024
HEARTBEAT_S = 10.0
LONG_POLL_MAX_S = 25.0


# ---- schemas ----------------------------------------------------------------


class AttendeeIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class CreateIn(BaseModel):
    # Optional call link (Google Meet, Zoom, …); omit for an in-person conversation.
    meet_url: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=300)
    # Optional user-provided attendees help name local speakers.
    attendees: list[AttendeeIn | str] = Field(default_factory=list, max_length=200)
    # "mock" | "jev" | "openai"; omitted = deployment default.
    gate_provider: str | None = Field(default=None, max_length=16)
    # "call" | "adhoc"; omitted = "call" with a link, "adhoc" without one.
    kind: str | None = Field(default=None, max_length=16)
    # Disabled by default; outbound recap integration is optional.
    recap: bool = False
    deliver_via: list[str] | None = Field(default=None, max_length=4)
    # Opt-in: also send the edition + RGS link to the people RGS recognises in the
    # call, by the same channels (off by default).
    share_with_participants: bool = False
    # How many people will talk (diarization hint, 1-8); omitted = unknown.
    expected_speakers: int | None = Field(default=None, ge=1, le=8)


class SessionPatchIn(BaseModel):
    gate_provider: str | None = Field(default=None, max_length=16)
    # Speaker-count hint; 0 clears it. Applies to a live session at the next window.
    expected_speakers: int | None = Field(default=None, ge=0, le=8)
    recap: bool | None = None
    deliver_via: list[str] | None = Field(default=None, max_length=4)
    # Opt in/out of sharing with the participants (takes effect at the recap; on a
    # finished recap, `POST /recap {"share_with_participants": true}` sends now).
    share_with_participants: bool | None = None


class RecapIn(BaseModel):
    # Change where the link goes before re-running; omitted = keep.
    deliver_via: list[str] | None = Field(default=None, max_length=4)
    # Rewrite the notes and re-lay the edition out (same edition, same link, when
    # there is one) instead of resuming/re-sending.
    reprint: bool = False
    # With `reprint`: print a brand-new edition (new canvas/link) instead.
    new_edition: bool = False
    # Opt in/out of sharing with the participants. `true` alone on a finished recap
    # only sends the participant messages (nobody already sent to gets another).
    share_with_participants: bool | None = None
    # Send to the participants again, including those already sent to.
    reshare: bool = False


class StopIn(BaseModel):
    discard: bool = False


class RenameIn(BaseModel):
    """Manual speaker edit (owner). Any combination; at least one field.

    A manual name is final: automatic naming never changes it afterwards.
    """

    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    user_email: str | None = Field(default=None, max_length=320)
    # Local directory person: display_name defaults to their name.
    person_email: str | None = Field(default=None, max_length=320)
    # Merge this speaker into another label (the same person).
    merge_into: str | None = Field(default=None, min_length=1, max_length=16)
    # Undo an earlier merge of this speaker.
    unmerge: bool = False


class LinkIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    url: str = Field(min_length=1, max_length=2048)


class FindingIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(default="", max_length=4000)
    links: list[LinkIn] = Field(default_factory=list, max_length=8)
    related_seq: int | None = None
    trigger_id: UUID | None = None
    source_agent: str | None = Field(default=None, max_length=64)


class ClaimIn(BaseModel):
    claimed_by: str | None = Field(default=None, max_length=64)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


# ---- helpers -------------------------------------------------------------------


def _require_enabled() -> None:
    settings = get_live_settings()
    if not settings.live_record_enabled:
        raise HTTPException(503, "Live transcription is not enabled on this deployment.")
    if not settings.asr_configured:
        raise HTTPException(503, "Live transcription is not configured (Nemotron ASR missing).")


get_live_ws_user = get_ws_user


def _publish(session_id: UUID, *events: dict | None) -> None:
    for event in events:
        if event is not None:
            manager.broker.publish(session_id, event)


# WebSocket close codes → the machine-readable `error.code` sent just before
# the close frame (see the authenticated OpenAPI schema).
WS_ERROR_CODES: dict[int, str] = {
    1000: "stopped",
    1011: "server_error",
    4400: "bad_frame",
    4401: "unauthenticated",
    4403: "forbidden",
    4404: "not_found",
    4408: "resync",
    4409: "not_live",
    4410: "superseded",
    4503: "unavailable",
}


def _ws_code(exc: HTTPException) -> int:
    return {401: 4401, 403: 4403, 404: 4404, 409: 4409, 503: 4503}.get(exc.status_code, 4400)


async def _close(ws: WebSocket, code: int, message: str = "") -> None:
    """Send `{"type":"error","code",...}` (except for a normal 1000) and close."""
    if code != 1000:
        with contextlib.suppress(Exception):
            await ws.send_json(
                {
                    "type": "error",
                    "code": WS_ERROR_CODES.get(code, "error"),
                    "close_code": code,
                    "message": message,
                    "ts": service.envelope(None, "error", {})["ts"],
                }
            )
    with contextlib.suppress(Exception):
        await ws.close(code=code, reason=message[:120])


# ---- config + sessions --------------------------------------------------------------


@router.get("/config")
def live_config(_user: User = Depends(get_current_user)) -> dict[str, Any]:
    from app.live.answer import gateway_config
    from app.live.recap import config_out as recap_config

    settings = get_live_settings()
    return {
        "enabled": settings.live_record_enabled,
        "gate": _gate_config(settings),
        "asr_configured": settings.asr_configured,
        # Audio ledger is recorded; raw PCM is not retained by default.
        "storage_configured": True,
        "llm_configured": gateway_config() is not None,
        "recap": recap_config(),
        "websockets": True,
        "audio": {
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "recommended_frame_ms": 100,
            "max_frame_bytes": MAX_WS_FRAME_BYTES,
            "client_buffer_s": 60,
        },
        "limits": {
            "max_session_hours": settings.live_max_session_hours,
            "findings_per_minute": settings.live_findings_per_minute,
        },
    }


def _gate_config(settings: Any) -> dict[str, Any]:
    """`GET /config` → `gate`: providers, what the gate listens for, example phrases."""
    from app.live.gate import GATE_ACTION_INFO
    from app.live.jev_settings import GATE_PROVIDERS, PROVIDER_LABELS, get_jev_settings

    cfg = get_jev_settings()
    gate = manager.loaded_gate
    status_fn = getattr(gate, "provider_status", None)
    health: dict[str, Any] = status_fn() if callable(status_fn) else {}
    providers = []
    for pid in GATE_PROVIDERS:
        entry: dict[str, Any] = {
            "id": pid,
            "label": PROVIDER_LABELS[pid],
            "available": cfg.provider_available(pid),
        }
        if pid in health:
            entry["healthy"] = health[pid]["healthy"]
            entry["last_error"] = health[pid]["last_error"]
        providers.append(entry)
    return {
        "enabled": bool(settings.jev_gate_enabled and cfg.configured),
        "default_provider": cfg.default_provider,
        "providers": providers,
        "interval_s": settings.live_gate_interval_s,
        "actions": [dict(a) for a in GATE_ACTION_INFO],
    }


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_live_session(
    body: CreateIn,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_enabled()
    if body.recap or body.deliver_via or body.share_with_participants:
        raise HTTPException(501, "Recap delivery requires an integration adapter.")
    provider = service.validate_gate_provider(body.gate_provider)
    attendees = [a if isinstance(a, str) else a.model_dump() for a in body.attendees]
    session = service.create_session(
        db,
        current,
        body.meet_url,
        body.title,
        provider,
        attendees=attendees,
        kind=body.kind,
        deliver_via=_channels(body.deliver_via),
        recap_enabled=body.recap,
        expected_speakers=body.expected_speakers,
        share_with_participants=body.share_with_participants,
    )
    return service.session_out(db, session, current)


def _channels(value: list[str] | None) -> list[str] | None:
    from app.live.recap import clean_channels

    if value is None:
        return None
    try:
        return clean_channels(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/sessions")
def list_live_sessions(
    active: bool = True,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    stmt = select(LiveSession).where(service.readable_clause(current))
    if active:
        stmt = stmt.where(LiveSession.status.in_(("live", "stopping")))
    rows = db.scalars(stmt.order_by(LiveSession.started_at.desc()).limit(limit)).all()
    return [service.session_out(db, s, current, detail=False) for s in rows]


@router.get("/sessions/{session_id}")
def get_live_session(
    session_id: UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    s = service.get_readable(db, session_id, current)
    _warm_people(db, s.id)
    return service.session_out(db, s, current)


def _warm_people(db: Session, session_id: UUID) -> None:
    """Resolve the Slack identity of the session's people (bounded wait; read paths only)."""
    from app.live import slack_people

    emails = db.scalars(
        select(LiveSpeaker.person_email).where(
            LiveSpeaker.session_id == session_id, LiveSpeaker.person_email.is_not(None)
        )
    ).all()
    if emails:
        slack_people.warm(emails)


def _patch_session(
    db: Session, session_id: UUID, user: User, body: SessionPatchIn
) -> tuple[dict, dict | None]:
    s = service.get_readable(db, session_id, user, lock=True)
    service.require_owner(s, user)
    event = None
    if body.recap or body.deliver_via or body.share_with_participants:
        raise HTTPException(501, "Recap delivery requires an integration adapter.")
    if body.expected_speakers is not None:
        s.expected_speakers = body.expected_speakers or None
    if body.gate_provider is not None:
        provider = service.validate_gate_provider(body.gate_provider)
        assert provider is not None
        event = service.set_gate_provider(db, s, provider)
    else:
        db.commit()
    s = service.get_readable(db, session_id, user)
    return service.session_out(db, s, user), event


@router.patch("/sessions/{session_id}")
async def patch_live_session(
    session_id: UUID,
    body: SessionPatchIn,
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Owner-only session settings. Today: switch the gate provider while live."""
    out, event = await db_call(_patch_session, session_id, current, body)
    _publish(session_id, event)
    if event is not None:
        manager.set_gate_provider(session_id, out["gate_provider"])
    if body.expected_speakers is not None:
        manager.set_expected_speakers(session_id, out.get("expected_speakers"))
    return out


def _prepare_stop(
    db: Session, session_id: UUID, user: User, discard: bool
) -> tuple[dict, dict | None, bool]:
    s = service.get_readable(db, session_id, user, lock=True)
    service.require_owner(s, user, allow_admin=True)
    if s.status in service.TERMINAL_STATUSES:
        if discard and s.status != "cancelled":
            raise HTTPException(409, "This session is already finalized.")
        db.commit()
        return service.session_out(db, s, user), None, False
    db.commit()
    event = service.set_status(db, session_id, "stopping")
    s = service.get_readable(db, session_id, user)
    return service.session_out(db, s, user), event, True


@router.post("/sessions/{session_id}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop_live_session(
    session_id: UUID,
    body: StopIn | None = None,
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    discard = bool(body and body.discard)
    out, event, start = await db_call(_prepare_stop, session_id, current, discard)
    _publish(session_id, event)
    if start or discard:
        manager.stop_in_background(session_id, discard=discard)
    return out


def _session_person_email(session: LiveSession, user: User, email: str) -> bool:
    """Only resolve local identities the owner already knows in this session."""
    key = email.strip().lower()
    return bool(key) and (
        key == (user.email or "").strip().lower()
        or key in {a["email"] for a in service.attendees_of(session) if a.get("email")}
        or key in {(shared or "").strip().lower() for shared in (session.shared_with or [])}
    )


def _person_name(db: Session, session: LiveSession, user: User, email: str) -> str | None:
    # Gate *before* consulting either the operator directory or the users table.
    # The default directory contains every locally signed-in user.
    if not _session_person_email(session, user, email):
        return None
    from app.live.people import get_directory

    person = get_directory().by_email(email)
    if person is not None:
        return person.name
    u = db.scalar(select(User).where(User.email == email.strip().lower()))
    if u is not None:
        return u.name or u.email
    return next(
        (
            a["name"]
            for a in service.attendees_of(session)
            if a.get("email") == email.strip().lower() and a.get("name")
        ),
        None,
    )


def _edit_speaker(
    db: Session, session_id: UUID, label: str, user: User, body: RenameIn
) -> tuple[list[dict], dict[str, Any]]:
    s = service.get_readable(db, session_id, user)
    service.require_owner(s, user)
    if not (body.display_name or body.person_email or body.merge_into or body.unmerge):
        raise HTTPException(422, "Give display_name, person_email, merge_into or unmerge.")
    events: list[dict] = []
    if body.unmerge:
        events += service.unmerge_speaker(db, s.id, label)
    if body.display_name or body.person_email or body.user_email is not None:
        person_name = None
        if body.person_email:
            person_name = _person_name(db, s, user, body.person_email)
            if person_name is None:
                # Identical result for absent and out-of-scope accounts, regardless
                # of display_name; free-text names need no account binding.
                raise HTTPException(400, "Unknown person_email; omit it for free-text names.")
            from app.live import slack_people

            # Warm only a person already in scope, never an arbitrary account.
            slack_people.warm([body.person_email], wait_s=1.0)
        if body.user_email and not _session_person_email(s, user, body.user_email):
            raise HTTPException(400, "No RGS user with that email.")
        if body.display_name or person_name:
            s = service.get_readable(db, session_id, user)
            events.append(
                service.rename_speaker(
                    db,
                    s,
                    label,
                    body.display_name,
                    body.user_email,
                    person_email=body.person_email,
                    person_name=person_name,
                )
            )
    if body.merge_into:
        events += service.merge_speaker(db, s.id, label, body.merge_into, manual=True)
    s = service.get_readable(db, session_id, user)
    if s.final_transcript is not None and (body.merge_into or body.unmerge or events):
        # A finished conversation: re-render the stored transcript with the new names.
        from app.live.finalize import render_transcript

        s.final_transcript = render_transcript(db, s.id)
        db.commit()
    rows = db.scalars(
        select(LiveSpeaker).where(LiveSpeaker.session_id == s.id).order_by(LiveSpeaker.color_index)
    ).all()
    out = {sp.label: service.speaker_out(db, sp) for sp in rows}
    if label not in out:
        raise HTTPException(404, "Speaker not found.")
    return events, {**out[label], "speakers": list(out.values())}


def _apply_speaker_edit(session_id: UUID, label: str, body: RenameIn, events: list[dict]) -> None:
    _publish(session_id, *events)
    for event in events:
        if event.get("type") == "speaker.update":
            manager.remember_speaker(session_id, event["data"])
    rt = manager.get(session_id)
    if rt is not None:
        if body.unmerge:
            rt.tracker.unmerge(label)
        if body.merge_into:
            rt.tracker.merge(label, body.merge_into)


@router.patch("/sessions/{session_id}/speakers/{label}")
def rename_live_speaker(
    session_id: UUID,
    label: str,
    body: RenameIn,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Rename / bind / merge / unmerge a speaker (owner). Returns the speaker.

    Also `POST` on the same path. The reply is the speaker object plus
    `speakers` (every speaker after the change).
    """
    events, out = _edit_speaker(db, session_id, label, current, body)
    _apply_speaker_edit(session_id, label, body, events)
    return out


router.add_api_route(
    "/sessions/{session_id}/speakers/{label}",
    rename_live_speaker,
    methods=["POST"],
    name="edit_live_speaker",
)


@router.get("/people")
def search_people(
    q: str = "",
    limit: int = 10,
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Only the caller's own local identity; never enumerate other accounts."""
    from app.live.people import Person, Priors, match_name

    people = [Person(email=current.email, name=current.name or current.email.split("@")[0])]
    limit = max(1, min(limit, 50))
    query = " ".join(q.split())[:100]
    if not query:
        picked = people[:limit]
        return {"people": [p.as_dict() for p in picked], "total": len(people)}
    owner = (current.email or "").lower() or None
    cands = match_name(query, people, Priors(owner_email=owner), limit=limit)
    folded = query.lower()
    contains = [p for p in people if folded in p.name.lower() or folded in (p.email or "").lower()]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in [*contains, *(c.person for c in cands)]:
        key = p.email or p.name
        if key in seen:
            continue
        seen.add(key)
        out.append(p.as_dict())
    return {"people": out[:limit], "total": len(people)}


@router.get("/sessions/{session_id}/final")
def get_final_transcript(
    session_id: UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The stored final transcript (after stop), or the live render so far."""
    from app.live.finalize import render_transcript

    s = service.get_readable(db, session_id, current)
    text = s.final_transcript if s.final_transcript is not None else render_transcript(db, s.id)
    return {
        "session_id": str(s.id),
        "status": s.status,
        "final": s.final_transcript is not None,
        "incomplete": bool(s.incomplete),
        "text": text or "",
    }


# ---- optional recap adapter (disabled in the public core) --------------------------


def _prepare_recap(db: Session, session_id: UUID, user: User, body: RecapIn) -> dict:
    from app.live.recap import INFLIGHT

    s = service.get_readable(db, session_id, user, lock=True)
    service.require_owner(s, user, allow_admin=True)
    if s.status != "finalized":
        raise HTTPException(409, "The recap runs once the session has stopped.")
    if s.recap_status in INFLIGHT:
        raise HTTPException(409, "This recap is already running.")
    if body.share_with_participants is not None:
        s.share_with_participants = body.share_with_participants
    delivery = dict(s.recap_delivery or {})
    if body.reshare:
        s.share_with_participants = True
        delivery.pop("participants", None)
        # A new email idempotency key round, so the relay sends it again.
        delivery["share_round"] = int(delivery.get("share_round") or 0) + 1
    share_only = (
        bool(body.share_with_participants or body.reshare)
        and not body.reprint
        and body.deliver_via is None
        and s.recap_status in ("done", "failed")
        and bool(s.recap_notes)
    )
    if share_only:
        # "Share now" after the recap: only the participant messages; the owner is not
        # sent the link again and the edition is not reprinted.
        if not s.deliver_via:
            raise HTTPException(
                409, "Pick Slack or email first: participants get the link the way you do."
            )
        s.recap_delivery = delivery
        s.recap_status = "sharing"
        db.commit()
        return service.session_out(db, s, user)
    if body.deliver_via is not None:
        s.deliver_via = _channels(body.deliver_via) or []
    if body.reprint:
        s.recap_notes = None
        edition = s.recap_edition or {}
        if body.new_edition or not edition.get("id"):
            s.recap_edition = None
        else:
            # Republish into the same edition so its canvas and share link stay.
            s.recap_edition = {
                **edition,
                "status": "republishing",
                "republish": True,
                "error": None,
            }
    elif (s.recap_edition or {}).get("status") == "failed":
        s.recap_edition = None  # try printing again; the notes are kept
    # Re-send everywhere asked (a resumed run skips channels already sent). People the
    # recap was already shared with keep their results: not sent again unless `reshare`.
    kept = {k: v for k, v in delivery.items() if k in ("participants", "share_round")}
    s.recap_delivery = kept or None
    s.recap_enabled = True
    s.recap_status = "pending"
    s.recap_error = None
    db.commit()
    return service.session_out(db, s, user)


@router.post("/sessions/{session_id}/recap", status_code=status.HTTP_202_ACCEPTED)
async def run_live_recap(
    session_id: UUID,
    body: RecapIn | None = None,
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Recap delivery is not part of the standalone public core."""
    raise HTTPException(501, "Recap delivery requires an integration adapter.")


@router.get("/sessions/{session_id}/notes")
def get_live_notes(
    session_id: UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """The structured notes the recap wrote (`null` until written) plus a Markdown render."""
    from app.live import recap

    s = service.get_readable(db, session_id, current)
    notes = s.recap_notes
    markdown = None
    if notes:
        owner = db.get(User, s.started_by_user_id)
        speakers = list(db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == s.id)).all())
        meta = recap.build_meta(s, owner, speakers)
        markdown = recap.notes_markdown(notes, meta)
    return {
        "session_id": str(s.id),
        "recap": recap.recap_out(s),
        "notes": notes,
        "markdown": markdown,
    }


@router.get("/sessions/{session_id}/transcript")
def get_live_transcript(
    session_id: UUID,
    since_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=5000),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    s = service.get_readable(db, session_id, current)
    _warm_people(db, s.id)
    out = service.transcript(db, s, since_seq, limit)
    out["interim"] = manager.broker.interim(session_id)
    return out


# ---- audio ---------------------------------------------------------------------


def _ws_origin_allowed(websocket: WebSocket) -> bool:
    """Reject cross-site browser sockets; allow verified non-browser clients."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True  # Non-browser clients must still pass verified auth.
    host = websocket.headers.get("host", "")
    if not host or origin == "null":
        return False
    same_origin = {f"http://{host}", f"https://{host}"}
    settings = get_settings()
    dev_origins = {"http://localhost:5173", "http://127.0.0.1:5173"}
    allowed = set(settings.cors_origins) or (dev_origins if not settings.is_prod else set())
    return origin in same_origin or origin in allowed


def _audio_target(db: Session, session_id: UUID, user: User) -> None:
    s = service.get_readable(db, session_id, user)
    service.require_owner(s, user)
    if s.status != "live":
        raise HTTPException(409, "This live session is not accepting audio.")


@router.websocket("/sessions/{session_id}/audio")
async def live_audio_ws(
    websocket: WebSocket,
    session_id: UUID,
    current: User | None = Depends(get_live_ws_user),
) -> None:
    await websocket.accept()
    if not _ws_origin_allowed(websocket):
        await _close(websocket, 4403, "Cross-site WebSocket Origin denied.")
        return
    if current is None:
        await _close(websocket, 4401, "Not signed in.")
        return
    try:
        _require_enabled()
        await db_call(_audio_target, session_id, current)
        rt = await manager.ensure_runtime(session_id)
    except HTTPException as exc:
        await _close(websocket, _ws_code(exc), str(exc.detail))
        return
    except (NotLive, LookupError):
        await _close(websocket, 4409, "This live session is not accepting audio.")
        return
    conn = object()

    async def close_from_runtime(code: int, reason: str) -> None:
        await _close(websocket, code, reason)

    rt.attach_audio(conn, close_from_runtime)
    await websocket.send_json(
        {
            "type": "ready",
            "received_samples": rt.received,
            "sample_rate": 16000,
            "encoding": "pcm_s16le",
        }
    )
    last_ack = time.monotonic()
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if rt.audio_conn is not conn:
                break
            data = msg.get("bytes")
            if data is not None:
                if len(data) > MAX_WS_FRAME_BYTES or len(data) % 2:
                    await _close(websocket, 4400, "Audio frames must be even-length and <= 64 KiB.")
                    break
                try:
                    rt.append_audio(data)
                except NotLive:
                    await _close(websocket, 4409, "This live session is no longer accepting audio.")
                    break
                now = time.monotonic()
                if now - last_ack >= 1.0:
                    last_ack = now
                    await websocket.send_json({"type": "ack", "received_samples": rt.received})
                continue
            text = msg.get("text")
            if text is not None:
                try:
                    payload = json.loads(text)
                except ValueError:
                    await _close(websocket, 4400, "Text frames must be JSON.")
                    break
                if isinstance(payload, dict) and payload.get("type") == "ping":
                    await websocket.send_json({"type": "pong", "received_samples": rt.received})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        rt.detach_audio(conn)


@router.post("/sessions/{session_id}/audio")
async def live_audio_http(
    session_id: UUID,
    request: Request,
    offset_samples: int = Query(ge=0),
    current: User = Depends(get_current_user),
) -> dict[str, int]:
    _require_enabled()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_HTTP_AUDIO_BYTES:
        raise HTTPException(413, "Audio body must be at most 1 MiB.")
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > MAX_HTTP_AUDIO_BYTES:
            raise HTTPException(413, "Audio body must be at most 1 MiB.")
    if len(body) % 2:
        raise HTTPException(400, "PCM body must have an even number of bytes.")
    await db_call(_audio_target, session_id, current)
    try:
        rt = await manager.ensure_runtime(session_id)
        received = rt.append_audio(bytes(body), offset=offset_samples)
    except (NotLive, LookupError):
        raise HTTPException(409, "This live session is not accepting audio.") from None
    except AudioGap as gap:
        raise HTTPException(
            409,
            {"message": "Resend audio from received_samples.", "received_samples": gap.received},
        ) from gap
    return {"received_samples": received}


# ---- events -----------------------------------------------------------------------


def _snapshot(db: Session, session_id: UUID, user: User) -> dict:
    s = service.get_readable(db, session_id, user)
    return {"session": service.session_out(db, s, user), "last_seq": s.last_seq}


def _events_page(
    db: Session, session_id: UUID, user: User, since_seq: int, limit: int
) -> tuple[list[dict], str, int]:
    s = service.get_readable(db, session_id, user)
    return service.events_since(db, session_id, since_seq, limit), s.status, s.last_seq


@router.websocket("/sessions/{session_id}/events")
async def live_events_ws(
    websocket: WebSocket,
    session_id: UUID,
    since_seq: int | None = None,
    current: User | None = Depends(get_live_ws_user),
) -> None:
    await websocket.accept()
    if not _ws_origin_allowed(websocket):
        await _close(websocket, 4403, "Cross-site WebSocket Origin denied.")
        return
    if current is None:
        await _close(websocket, 4401, "Not signed in.")
        return
    sub = manager.broker.subscribe(session_id)
    lock = asyncio.Lock()

    async def send(message: dict[str, Any]) -> None:
        async with lock:  # the ping reader and the stream share the socket
            await websocket.send_json(message)

    try:
        try:
            snap = await db_call(_snapshot, session_id, current)
        except HTTPException as exc:
            await _close(websocket, _ws_code(exc), str(exc.detail))
            return
        await send(
            service.envelope(
                None,
                "snapshot",
                {
                    **snap,
                    "interim": manager.broker.interim(session_id),
                    "gate_tick": manager.broker.gate_tick(session_id),
                },
            )
        )
        delivered = since_seq if since_seq is not None else snap["last_seq"]
        if since_seq is not None:
            while True:
                page, _status, _last = await db_call(
                    _events_page, session_id, current, delivered, 500
                )
                for event in page:
                    await send(event)
                    delivered = event["seq"]
                if len(page) < 500:
                    break
        receiver = asyncio.create_task(_drain_client(websocket, send))
        try:
            while not receiver.done():
                if sub.overflowed:
                    await send(service.envelope(None, "resync", {"last_seq": delivered}))
                    await _close(websocket, 4408, "Client too slow; reconnect with since_seq.")
                    return
                try:
                    event = await asyncio.wait_for(sub.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    rt = manager.get(session_id)
                    await send(
                        service.envelope(
                            None,
                            "session.heartbeat",
                            {
                                "audio_seconds": round(rt.audio_end_s, 1) if rt else None,
                                "asr_state": rt.asr_state if rt else None,
                            },
                        )
                    )
                    continue
                seq = event.get("seq")
                if seq is not None:
                    if seq <= delivered:
                        continue
                    if seq > delivered + 1:
                        # Missed durable events (published before we
                        # subscribed or reordered threads): backfill from DB.
                        page, _s, _l = await db_call(
                            _events_page, session_id, current, delivered, seq - delivered - 1
                        )
                        for missed in page:
                            if missed["seq"] < seq:
                                await send(missed)
                    delivered = seq
                await send(event)
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiver
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sub.close()


async def _drain_client(websocket: WebSocket, send: Callable[[dict], Awaitable[None]]) -> None:
    """Consume client frames (ping only); returns when the client goes away."""
    while True:
        msg = await websocket.receive()
        if msg["type"] == "websocket.disconnect":
            return
        text = msg.get("text")
        if text and '"ping"' in text:
            with contextlib.suppress(Exception):
                await send(service.envelope(None, "pong", {}))


async def long_poll(
    session_id: UUID, user: User, since_seq: int, wait_s: float, limit: int
) -> dict[str, Any]:
    deadline = anyio.current_time() + max(0.0, min(wait_s, LONG_POLL_MAX_S))
    while True:
        events, status_, last_seq = await db_call(_events_page, session_id, user, since_seq, limit)
        remaining = deadline - anyio.current_time()
        if events or remaining <= 0 or status_ in service.TERMINAL_STATUSES:
            break
        # Wake on publish, but re-check at least every 2 s (another process
        # or a publish that raced the query still shows up quickly).
        await manager.broker.wait(session_id, timeout=min(remaining, 2.0))
    pending = await db_call(_pending_triggers, session_id, user)
    return {
        "events": events,
        "last_seq": events[-1]["seq"] if events else min(since_seq, last_seq),
        "interim": manager.broker.interim(session_id),
        "gate_tick": manager.broker.gate_tick(session_id),
        "status": status_,
        "pending_triggers": pending,
    }


def _pending_triggers(db: Session, session_id: UUID, user: User) -> list[dict]:
    s = service.get_readable(db, session_id, user)
    return service.list_triggers(db, s, "pending")


@router.get("/sessions/{session_id}/events")
async def live_events_poll(
    session_id: UUID,
    since_seq: int = Query(default=0, ge=0),
    wait_s: float = Query(default=20.0, ge=0, le=LONG_POLL_MAX_S),
    limit: int = Query(default=200, ge=1, le=1000),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    return await long_poll(session_id, current, since_seq, wait_s, limit)


# ---- findings + triggers ----------------------------------------------------------------


@router.post("/sessions/{session_id}/findings", status_code=status.HTTP_201_CREATED)
def post_live_finding(
    session_id: UUID,
    body: FindingIn,
    response: Response,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    s = service.get_readable(db, session_id, current)
    finding, event, deduped = service.create_finding(
        db,
        s,
        current,
        title=body.title,
        body_md=body.body_md,
        links=[link.model_dump() for link in body.links],
        related_seq=body.related_seq,
        trigger_id=body.trigger_id,
        source_agent=body.source_agent or "web",
    )
    _publish(session_id, event)
    if deduped:
        response.status_code = status.HTTP_200_OK
        response.headers["X-Live-Deduped"] = "1"
    return finding


@router.get("/sessions/{session_id}/findings")
def list_live_findings(
    session_id: UUID,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    return service.list_findings(db, service.get_readable(db, session_id, current))


@router.get("/sessions/{session_id}/triggers")
def list_live_triggers(
    session_id: UUID,
    status_filter: str | None = Query(default="pending", alias="status"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    if status_filter not in (None, "", "pending", "claimed", "answered", "expired", "all"):
        raise HTTPException(400, "Unknown trigger status.")
    s = service.get_readable(db, session_id, current)
    return service.list_triggers(
        db, s, None if status_filter in (None, "", "all") else status_filter
    )


@router.post("/sessions/{session_id}/triggers/{trigger_id}/claim")
def claim_live_trigger(
    session_id: UUID,
    trigger_id: UUID,
    body: ClaimIn | None = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    s = service.get_readable(db, session_id, current)
    won, trigger, event = service.claim_trigger(
        db, s, trigger_id, current, (body.claimed_by if body else None) or "web"
    )
    _publish(session_id, event)
    if not won:
        raise HTTPException(409, {"message": "Trigger already claimed.", "trigger": trigger})
    return trigger


def _contributor(db: Session, session_id: UUID, user: User) -> None:
    service.require_contributor(service.get_readable(db, session_id, user), user)


@router.post("/sessions/{session_id}/ask", status_code=status.HTTP_201_CREATED)
async def ask_live_session(
    session_id: UUID,
    body: AskIn,
    current: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Ask the sidekick a question about the call; the answer lands as a finding."""
    await db_call(_contributor, session_id, current)
    responder = manager.responder
    finding, event = await anyio.to_thread.run_sync(
        responder.ask, session_id, body.question.strip(), getattr(current, "id", None)
    )
    _publish(session_id, event)
    return finding
