"""RGS live persistence and authorization.

Synchronous SQLAlchemy helpers; async callers offload them to a worker thread
without blocking. Every helper that appends durable events returns the event
envelopes so the caller can publish them *after* the commit.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import structlog
from fastapi import HTTPException
from sqlalchemy import func, or_, select, true, update
from sqlalchemy.orm import Session

from app.live import audio_store, slack_people
from app.live.gate import GATE_ACTIONS, GateTrigger
from app.live.settings import get_live_settings
from app.models.live import (
    LiveEvent,
    LiveFinding,
    LiveSegment,
    LiveSession,
    LiveSpeaker,
    LiveTrigger,
)
from app.models.user import User

log = structlog.get_logger(__name__)

_MEET_CODE_RE = re.compile(r"^[a-z]{3}-[a-z]{4}-[a-z]{3}$")
TERMINAL_STATUSES = ("finalized", "failed", "cancelled")
FINDINGS_GRACE_AFTER_STOP = timedelta(minutes=15)
FINDING_DEDUPE_WINDOW = timedelta(minutes=10)
MAX_LINKS = 8


# ---- parsing --------------------------------------------------------------


def parse_meet_link(raw: str | None) -> tuple[str | None, str | None]:
    """Return `(code, canonical_url)` for an optional call link.

    A Google Meet link or bare code becomes `("abc-defg-hij", meet URL)`; any
    other http(s) link (Zoom, Teams, …) is kept as-is with its URL as the code;
    empty means "no link" (an in-person conversation).
    """
    value = (raw or "").strip()
    if not value:
        return None, None
    if len(value) > 2048:
        raise HTTPException(400, "That link is too long.")
    if _MEET_CODE_RE.match(value.lower()):
        code = value.lower()
        return code, f"https://meet.google.com/{code}"
    candidate = value
    if "://" not in candidate and "/" in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "Paste a call link (for example meet.google.com/abc-defg-hij).")
    host = parsed.hostname.lower()
    if host == "meet.google.com":
        path = parsed.path.strip("/").split("/")
        code = (path[0] if path else "").lower()
        if _MEET_CODE_RE.match(code) is None:
            raise HTTPException(400, "That does not look like a Google Meet meeting code.")
        return code, f"https://meet.google.com/{code}"
    url = parsed.geturl()
    return url.lower()[:64], url


def title_key(title: str) -> str:
    folded = unicodedata.normalize("NFKC", title).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", folded)).strip()[:200]


# ---- authorization --------------------------------------------------------


def readable_clause(user: User):
    """Session readers: starter, admins, or explicitly shared read-only viewers.

    Machine identities are rejected by the public identity adapter. Never
    grant blanket access to a signed machine actor.
    """
    if user.is_admin:
        return true()
    email = (user.email or "").strip().lower()
    if not email:
        return or_(LiveSession.started_by_user_id == user.id)
    return or_(
        LiveSession.started_by_user_id == user.id,
        LiveSession.shared_with.contains([email]),
    )


def get_readable(db: Session, session_id: UUID, user: User, *, lock: bool = False) -> LiveSession:
    stmt = select(LiveSession).where(LiveSession.id == session_id, readable_clause(user))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    session = db.scalar(stmt)
    if session is None:
        raise HTTPException(404, "Live session not found.")
    return session


def require_owner(session: LiveSession, user: User, *, allow_admin: bool = False) -> None:
    if session.started_by_user_id == user.id:
        return
    if allow_admin and user.is_admin:
        return
    raise HTTPException(403, "Only the person who started this live session can do that.")


def is_shared_viewer(session: LiveSession, user: User) -> bool:
    """Reads only because the recap was shared with them (not owner, admin or agent)."""
    return not (session.started_by_user_id == user.id or user.is_admin)


def require_contributor(session: LiveSession, user: User | None) -> None:
    """Post findings, claim triggers, ask: authenticated readers except shared viewers."""
    if user is None:
        raise HTTPException(401, "Authentication required.")
    if is_shared_viewer(session, user):
        raise HTTPException(403, "This conversation was shared with you to read only.")


# ---- events ---------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def envelope(seq: int | None, type_: str, data: dict[str, Any], ts: datetime | None = None):
    return {"seq": seq, "type": type_, "ts": _iso(ts or datetime.now(UTC)), "data": data}


def append_event(db: Session, session_id: UUID, type_: str, data: dict[str, Any]) -> dict:
    """Allocate the next seq atomically and persist one durable event.

    Caller commits. The single `UPDATE ... RETURNING` serializes allocators on
    the session row, so concurrent writers never collide or leave gaps.
    """
    seq = db.execute(
        update(LiveSession)
        .where(LiveSession.id == session_id)
        .values(last_seq=LiveSession.last_seq + 1)
        .returning(LiveSession.last_seq)
    ).scalar_one()
    if "seq" in data:
        data = {**data, "seq": seq}
    now = datetime.now(UTC)
    db.add(LiveEvent(session_id=session_id, seq=seq, type=type_, payload=data, created_at=now))
    return envelope(seq, type_, data, now)


def events_since(db: Session, session_id: UUID, since_seq: int, limit: int = 200) -> list[dict]:
    rows = db.scalars(
        select(LiveEvent)
        .where(LiveEvent.session_id == session_id, LiveEvent.seq > since_seq)
        .order_by(LiveEvent.seq)
        .limit(max(1, min(limit, 1000)))
    ).all()
    return [envelope(r.seq, r.type, r.payload, r.created_at) for r in rows]


# ---- serialization --------------------------------------------------------


def _user_ref(user: User | None) -> dict | None:
    if user is None:
        return None
    return {"id": str(user.id), "name": user.name, "email": user.email}


def speaker_out(db: Session, sp: LiveSpeaker) -> dict[str, Any]:
    suggestion = None
    if sp.suggested_user_id is not None:
        u = db.get(User, sp.suggested_user_id)
        if u is not None:
            suggestion = {
                "user_id": str(u.id),
                "name": u.name or u.email,
                "email": u.email,
                "score": round(float(sp.suggestion_score or 0.0), 3),
                "source": "voiceprint",
            }
    if suggestion is None and sp.suggested_name:
        # An uncertain naming guess is shown without renaming.
        suggestion = {
            "user_id": None,
            "name": sp.suggested_name,
            "email": sp.suggested_email,
            "score": round(float(sp.suggestion_score or 0.0), 3),
            "source": "naming",
        }
    person = None
    if sp.person_email:
        # Slack identity from the in-process cache only (a miss resolves in the background).
        person = {
            "email": sp.person_email,
            "name": sp.display_name,
            **slack_people.cached_fields(sp.person_email),
        }
    return {
        "label": sp.label,
        "display_name": sp.display_name,
        # How the name was set: "default" | "manual" | "self_intro" | "inferred".
        "source": sp.name_source or "default",
        # Confidence (0-1) of an automatic name and what gave it away; null for
        # default names, 1.0 for manual ones.
        "confidence": (
            1.0
            if sp.name_source == "manual"
            else round(float(sp.name_confidence), 3)
            if sp.name_confidence is not None and sp.name_source
            else None
        ),
        "evidence": sp.name_evidence if sp.name_source else None,
        # Local directory person behind the name, when resolved.
        "person": person,
        "color_index": sp.color_index,
        "speech_seconds": round(float(sp.speech_seconds or 0.0), 1),
        "user": _user_ref(db.get(User, sp.user_id)) if sp.user_id else None,
        "suggestion": suggestion,
        "merged_into": sp.merged_into,
    }


def segment_out(seg: LiveSegment) -> dict[str, Any]:
    return {
        "seq": seg.seq,
        "speaker": seg.speaker_label,
        "start_s": float(seg.start_seconds),
        "end_s": float(seg.end_seconds),
        "text": seg.text,
    }


def finding_out(db: Session, f: LiveFinding) -> dict[str, Any]:
    return {
        "id": str(f.id),
        "seq": f.seq,
        "title": f.title,
        "body_md": f.body_md,
        "links": list(f.links or []),
        "related_seq": f.related_seq,
        "trigger_id": str(f.trigger_id) if f.trigger_id else None,
        "source_agent": f.source_agent,
        "created_by": _user_ref(db.get(User, f.created_by_user_id))
        if f.created_by_user_id
        else None,
        "created_at": _iso(f.created_at),
    }


def trigger_out(t: LiveTrigger) -> dict[str, Any]:
    return {
        "trigger_id": str(t.id),
        "seq": t.seq,
        "action": t.action,
        "query": t.query,
        "seq_from": t.seq_from,
        "seq_to": t.seq_to,
        "probability": t.probability,
        "jev_latency_ms": t.jev_latency_ms,
        "provider": t.provider,
        "anchor_seq": t.anchor_seq,
        "topic": t.topic,
        "context_text": t.context_text,
        "decided_at": _iso(t.decided_at) if t.decided_at else None,
        "status": t.status,
        "claimed_by": t.claimed_by,
        "finding_id": str(t.finding_id) if t.finding_id else None,
        "created_at": _iso(t.created_at),
    }


def persisted_user_id(db: Session, user: User | None) -> UUID | None:
    """`user.id` when it is a real `users` row; transient bot identities -> None."""
    if user is None:
        return None
    return user.id if db.get(User, user.id) is not None else None


def attendees_of(s: LiveSession) -> list[dict[str, Any]]:
    out = []
    for a in (s.attendees or [])[:200]:
        if isinstance(a, dict):
            out.append({"email": a.get("email"), "name": a.get("name")})
    return out


def clean_attendees(raw: list[Any] | None) -> list[dict[str, str | None]]:
    """Normalise an optional roster: strings ("Alice Example") or {name, email}."""
    out: list[dict[str, str | None]] = []
    for item in (raw or [])[:200]:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name") or "").split())[:200] or None
        email = str(item.get("email") or "").strip().lower()[:320] or None
        if name or email:
            out.append({"name": name, "email": email})
    return out


def recap_out(s: LiveSession) -> dict[str, Any]:
    from app.live.recap import recap_out as _recap_out

    return _recap_out(s)


def session_out(
    db: Session, s: LiveSession, viewer: User, *, detail: bool = True
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(s.id),
        # Reserved compatibility fields; RGS has no separate meeting rows.
        "meeting_id": None,
        "meet_code": s.meet_code,
        "meet_url": s.meet_url,
        "title": s.title,
        "status": s.status,
        "linked_calendar_event": False,
        "started_by": _user_ref(db.get(User, s.started_by_user_id)),
        "is_owner": s.started_by_user_id == viewer.id,
        # Read-only: the owner shared the recap with this viewer.
        "shared_with_you": s.started_by_user_id != viewer.id
        and (viewer.email or "").strip().lower() in (s.shared_with or []),
        "started_at": _iso(s.started_at),
        "stopped_at": _iso(s.stopped_at),
        "audio_seconds": round(s.audio_samples / max(s.sample_rate, 1), 1),
        "last_seq": s.last_seq,
        "asr_state": s.asr_state,
        "transcript_id": None,
        "has_final_transcript": bool(s.final_transcript),
        "incomplete": bool(s.incomplete),
        "error": s.error,
        "gate_provider": effective_gate_provider(s),
        "kind": s.kind,
        "recap": recap_out(s),
        # How many people the owner said would talk (diarization hint; null = unknown).
        "expected_speakers": s.expected_speakers,
        # Opt-in raw audio retention (owner only): when the stored audio expires.
        "audio_retain_until": _iso(s.audio_retain_until)
        if s.started_by_user_id == viewer.id
        else None,
    }
    if detail:
        out["speakers"] = [
            speaker_out(db, sp)
            for sp in db.scalars(
                select(LiveSpeaker)
                .where(LiveSpeaker.session_id == s.id)
                .order_by(LiveSpeaker.color_index)
            ).all()
        ]
        out["attendees"] = attendees_of(s)
        # Who a recap share would go to (the people RGS recognised who spoke).
        from app.live.recap import participants_out

        out["participants"] = participants_out(db, s)
    return out


def status_data(s: LiveSession) -> dict[str, Any]:
    return {
        "status": s.status,
        "asr_state": s.asr_state,
        "audio_seconds": round(s.audio_samples / max(s.sample_rate, 1), 1),
        "meeting_id": None,
        "transcript_id": None,
        "has_final_transcript": bool(s.final_transcript),
        "incomplete": bool(s.incomplete),
        "error": s.error,
        "gate_provider": effective_gate_provider(s),
        "recap_status": s.recap_status,
    }


def effective_gate_provider(s: LiveSession) -> str:
    """The provider this session's gate uses (stored choice, else the deployment default).

    A stored provider whose key has since been removed reads as the default:
    that is what the gate actually runs.
    """
    from app.live.jev_settings import get_jev_settings

    return get_jev_settings().resolve_provider(s.gate_provider)


def validate_gate_provider(value: str | None) -> str | None:
    """400 for an unknown or unusable provider; None passes (= default)."""
    from app.live.jev_settings import GATE_PROVIDERS, get_jev_settings

    if value is None:
        return None
    provider = value.strip().lower()
    if provider not in GATE_PROVIDERS:
        raise HTTPException(400, f"gate_provider must be one of {', '.join(GATE_PROVIDERS)}.")
    if not get_jev_settings().provider_available(provider):
        raise HTTPException(409, f"The {provider} gate provider is not configured here.")
    return provider


def set_gate_provider(db: Session, session: LiveSession, provider: str) -> dict | None:
    """Switch a live session's gate provider; returns the `session.status` event (or None)."""
    if session.status not in ("live", "stopping"):
        raise HTTPException(409, "Only a live session can switch its gate provider.")
    if session.gate_provider == provider:
        db.commit()
        return None
    session.gate_provider = provider
    db.flush()
    event = append_event(db, session.id, "session.status", status_data(session))
    db.commit()
    log.info("live.gate_provider_switched", session_id=str(session.id), provider=provider)
    return event


# ---- session lifecycle ----------------------------------------------------


def create_session(
    db: Session,
    user: User,
    meet_url: str | None,
    title: str | None,
    gate_provider: str | None = None,
    attendees: list[Any] | None = None,
    *,
    kind: str | None = None,
    deliver_via: list[str] | None = None,
    recap_enabled: bool = False,
    expected_speakers: int | None = None,
    share_with_participants: bool = False,
) -> LiveSession:
    if persisted_user_id(db, user) is None:
        raise HTTPException(403, "Only people can start a live session.")
    code, url = parse_meet_link(meet_url)
    now = datetime.now(UTC)
    if code is not None:
        existing = db.scalar(
            select(LiveSession)
            .where(
                LiveSession.meet_code == code,
                LiveSession.status.in_(("live", "stopping")),
                LiveSession.started_at >= now - timedelta(hours=12),
                readable_clause(user),
            )
            .order_by(LiveSession.started_at.desc())
            .limit(1)
        )
        if existing is not None:
            raise HTTPException(
                409,
                {
                    "message": "This call is already being transcribed live; open it instead.",
                    "session_id": str(existing.id),
                },
            )
    clean_title = (title or "").strip()[:300] or None
    default_title = f"Live — {code}" if code and url and "meet.google.com" in url else None
    kind = kind or ("call" if url else "adhoc")
    if kind not in ("call", "adhoc"):
        raise HTTPException(422, "kind must be 'call' or 'adhoc'.")
    if kind == "adhoc" and not clean_title:
        default_title = f"Ad-hoc conversation — {now:%b} {now.day}, {now:%H:%M} UTC"
    session = LiveSession(
        id=uuid4(),
        started_by_user_id=user.id,
        meet_url=url,
        meet_code=code,
        attendees=clean_attendees(attendees),
        title=clean_title or default_title or f"Live — {now:%b %d, %H:%M} UTC",
        status="live",
        sample_rate=16000,
        started_at=now,
        # Resolved at start so a later LIVE_GATE_PROVIDER change does not move it.
        gate_provider=_resolve_create_provider(gate_provider),
        kind=kind,
        recap_enabled=bool(recap_enabled),
        deliver_via=list(deliver_via) if deliver_via is not None else [],
        share_with_participants=bool(share_with_participants),
        expected_speakers=expected_speakers,
        audio_retain_until=audio_store.retain_until(user.email, now=now),
    )
    db.add(session)
    db.flush()
    append_event(db, session.id, "session.status", status_data(session))
    db.commit()
    db.refresh(session)
    log.info("live.session_started", session_id=str(session.id), has_link=code is not None)
    return session


def set_status(
    db: Session,
    session_id: UUID,
    status: str | None,
    *,
    error: str | None = None,
    asr_state: str | None = None,
) -> dict | None:
    """Update status/asr_state; append a `session.status` event when changed."""
    s = db.scalar(
        select(LiveSession)
        .where(LiveSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if s is None:
        return None
    changed = False
    if s.status in TERMINAL_STATUSES:
        status = None  # terminal is final
    if status is not None and s.status != status:
        s.status = status
        changed = True
        if status == "stopping" and s.stopped_at is None:
            s.stopped_at = datetime.now(UTC)
        if status in TERMINAL_STATUSES:
            s.finalized_at = datetime.now(UTC)
            if s.stopped_at is None:
                s.stopped_at = s.finalized_at
    if error is not None and s.error != error:
        s.error = error[:1000]
        changed = True
    if asr_state is not None and s.asr_state != asr_state:
        s.asr_state = asr_state
        changed = True
    if not changed:
        db.commit()
        return None
    event = append_event(db, s.id, "session.status", status_data(s))
    db.commit()
    return event


# ---- transcript + speakers ------------------------------------------------


@dataclass(frozen=True)
class SpeakerSnapshot:
    label: str
    color_index: int
    speech_seconds: float
    centroid: list[float] | None
    weight: float
    merged_into: str | None


def _resolve_create_provider(requested: str | None) -> str | None:
    from app.live.jev_settings import get_jev_settings

    cfg = get_jev_settings()
    if not cfg.available_providers:
        return requested  # gate off here: keep the choice for the record
    return cfg.resolve_provider(requested)


def record_window(
    db: Session,
    session_id: UUID,
    *,
    segments: list[tuple[str, float, float, str]],
    committed_until: float,
    speakers: list[SpeakerSnapshot],
    announce: set[str],
) -> list[dict]:
    """Persist one window's committed segments + speaker state atomically.

    `announce` names speaker labels whose public state changed (new, merged,
    renamed) and need a `speaker.update` event.
    """
    events: list[dict] = []
    existing = {
        sp.label: sp
        for sp in db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == session_id))
    }
    for snap in speakers:
        sp = existing.get(snap.label)
        if sp is None:
            sp = LiveSpeaker(
                session_id=session_id,
                label=snap.label,
                display_name=f"Speaker {snap.label.lstrip('S')}",
                color_index=snap.color_index,
            )
            db.add(sp)
            existing[snap.label] = sp
            announce = announce | {snap.label}
        sp.speech_seconds = snap.speech_seconds
        sp.centroid = snap.centroid
        sp.centroid_weight = snap.weight
        if snap.merged_into != sp.merged_into:
            sp.merged_into = snap.merged_into
            announce = announce | {snap.label}
    db.flush()
    for label in sorted(announce):
        if label in existing:
            events.append(
                append_event(db, session_id, "speaker.update", speaker_out(db, existing[label]))
            )
    for speaker, start, end, text in segments:
        data = {
            "seq": None,
            "speaker": speaker,
            "start_s": round(start, 3),
            "end_s": round(end, 3),
            "text": text,
        }
        event = append_event(db, session_id, "segment.final", data)
        db.add(
            LiveSegment(
                session_id=session_id,
                seq=event["seq"],
                speaker_label=speaker,
                start_seconds=Decimal(str(round(start, 3))),
                end_seconds=Decimal(str(round(end, 3))),
                text=text,
            )
        )
        events.append(event)
    db.execute(
        update(LiveSession)
        .where(LiveSession.id == session_id)
        .values(committed_until_s=Decimal(str(round(committed_until, 3))))
    )
    db.commit()
    return events


def transcript(db: Session, session: LiveSession, since_seq: int = 0, limit: int = 1000) -> dict:
    segs = db.scalars(
        select(LiveSegment)
        .where(LiveSegment.session_id == session.id, LiveSegment.seq > since_seq)
        .order_by(LiveSegment.seq)
        .limit(max(1, min(limit, 5000)))
    ).all()
    speakers = db.scalars(
        select(LiveSpeaker)
        .where(LiveSpeaker.session_id == session.id)
        .order_by(LiveSpeaker.color_index)
    ).all()
    return {
        "segments": [segment_out(s) for s in segs],
        "speakers": [speaker_out(db, sp) for sp in speakers],
        "last_seq": session.last_seq,
        "status": session.status,
    }


def _speaker_row(db: Session, session_id: UUID, label: str) -> LiveSpeaker | None:
    return db.scalar(
        select(LiveSpeaker)
        .where(LiveSpeaker.session_id == session_id, LiveSpeaker.label == label)
        .with_for_update()
    )


def _resolve_label(rows: dict[str, LiveSpeaker], label: str) -> str:
    seen: set[str] = set()
    while label in rows and rows[label].merged_into and label not in seen:
        seen.add(label)
        label = rows[label].merged_into or label
    return label


def rename_speaker(
    db: Session,
    session: LiveSession,
    label: str,
    display_name: str | None,
    user_email: str | None,
    *,
    person_email: str | None = None,
    person_name: str | None = None,
) -> dict:
    """Manual rename (owner). Never overridden by automatic naming afterwards.

    `person_email` binds the speaker to a local directory person; when no
    `display_name` is given, the person's name (`person_name`) is used.
    """
    sp = _speaker_row(db, session.id, label)
    if sp is None:
        raise HTTPException(404, "Speaker not found.")
    name = " ".join((display_name or "").split())[:200] or (person_name or "").strip()[:200]
    if not name:
        raise HTTPException(400, "display_name is required.")
    sp.display_name = name
    sp.name_source = "manual"  # a manual name is never overridden by automatic naming
    sp.name_confidence = 1.0
    sp.name_evidence = None
    sp.suggested_name = None
    sp.suggested_email = None
    if person_email is not None:
        sp.person_email = person_email.strip().lower() or None
    elif display_name is not None and sp.person_email and person_name is None:
        # A free-text rename drops a stale directory identity.
        sp.person_email = None
    if user_email is not None:
        email = user_email.strip().lower()
        if email:
            bound = db.scalar(select(User).where(User.email == email))
            if bound is None:
                raise HTTPException(400, "No RGS user with that email.")
            sp.user_id = bound.id
            sp.person_email = sp.person_email or email
        else:
            sp.user_id = None
    elif sp.person_email and sp.user_id is None:
        # Bind the RGS account too when the person has one (best effort).
        bound = db.scalar(select(User).where(User.email == sp.person_email))
        if bound is not None:
            sp.user_id = bound.id
    db.flush()
    event = append_event(db, session.id, "speaker.update", speaker_out(db, sp))
    db.commit()
    return event


def merge_speaker(
    db: Session,
    session_id: UUID,
    label: str,
    into: str,
    *,
    manual: bool,
    confidence: float | None = None,
    reason: str | None = None,
) -> list[dict]:
    """Merge `label` into `into` (its lines render as `into`). Returns events.

    Automatic merges (naming pass) never touch a manually named or bound
    speaker being merged away, and never merge two speakers that carry
    different manual names. A manual merge always applies (unless it would
    form a cycle). The survivor inherits the merged speaker's speech time,
    and its name when it has none.
    """
    rows = {
        sp.label: sp
        for sp in db.scalars(
            select(LiveSpeaker).where(LiveSpeaker.session_id == session_id).with_for_update()
        )
    }
    src, dst_label = rows.get(label), _resolve_label(rows, into)
    dst = rows.get(dst_label)
    if src is None or dst is None:
        if manual:
            raise HTTPException(404, "Speaker not found.")
        db.commit()
        return []
    if dst_label == label or _resolve_label(rows, label) == dst_label:
        if manual and src.merged_into is None and dst_label == label:
            raise HTTPException(400, "A speaker cannot be merged into itself.")
        db.commit()
        return []
    if not manual:
        status = db.scalar(select(LiveSession.status).where(LiveSession.id == session_id))
        locked = src.name_source == "manual" or src.user_id is not None
        if (
            status not in ("live", "stopping")
            or src.merged_into is not None
            or dst.merged_into is not None
            or locked
        ):
            db.commit()
            return []
    src.merged_into = dst_label
    dst.speech_seconds = float(dst.speech_seconds or 0.0) + float(src.speech_seconds or 0.0)
    src_named = src.name_source not in (None, "default")
    dst_named = dst.name_source not in (None, "default")
    if src_named and not dst_named:
        dst.display_name = src.display_name
        dst.name_source = src.name_source
        dst.name_confidence = src.name_confidence
        dst.name_evidence = src.name_evidence
        dst.person_email = src.person_email
        dst.user_id = dst.user_id or src.user_id
    # Re-point anything that was merged into the label that just went away.
    for other in rows.values():
        if other.merged_into == label:
            other.merged_into = dst_label
    db.flush()
    events = [append_event(db, session_id, "speaker.update", speaker_out(db, src))]
    data = speaker_out(db, dst)
    if reason or confidence is not None:
        data = {**data, "merge": {"from": label, "confidence": confidence, "reason": reason}}
    events.append(append_event(db, session_id, "speaker.update", data))
    db.commit()
    log.info(
        "live.speaker_merged",
        session_id=str(session_id),
        label=label,
        into=dst_label,
        manual=manual,
        confidence=confidence,
    )
    return events


def unmerge_speaker(db: Session, session_id: UUID, label: str) -> list[dict]:
    """Undo a merge: the label's own lines render as itself again (owner)."""
    rows = {
        sp.label: sp
        for sp in db.scalars(
            select(LiveSpeaker).where(LiveSpeaker.session_id == session_id).with_for_update()
        )
    }
    src = rows.get(label)
    if src is None:
        raise HTTPException(404, "Speaker not found.")
    if src.merged_into is None:
        db.commit()
        return []
    dst = rows.get(_resolve_label(rows, label))
    src.merged_into = None
    if dst is not None and dst is not src:
        dst.speech_seconds = max(
            0.0, float(dst.speech_seconds or 0.0) - float(src.speech_seconds or 0.0)
        )
    db.flush()
    events = [append_event(db, session_id, "speaker.update", speaker_out(db, src))]
    if dst is not None and dst is not src:
        events.append(append_event(db, session_id, "speaker.update", speaker_out(db, dst)))
    db.commit()
    return events


def apply_name(
    db: Session,
    session_id: UUID,
    label: str,
    name: str,
    *,
    source: str,
    confidence: float,
    evidence: str,
    person_email: str | None = None,
    suggest_only: bool = False,
    allow_terminal: bool = False,
) -> dict | None:
    """Automatic naming (self-introduction or the naming pass).

    Never touches a manual/bound or merged-away speaker. `suggest_only`
    records a guess shown as "maybe <name>?" without renaming. Returns the
    `speaker.update` event, or None when nothing changed.
    """
    sp = _speaker_row(db, session_id, label)
    status = db.scalar(select(LiveSession.status).where(LiveSession.id == session_id))
    name = " ".join((name or "").split())[:200]
    live = status in ("live", "stopping") or (allow_terminal and status == "finalized")
    if (
        sp is None
        or not name
        or not live
        or sp.name_source == "manual"
        or sp.user_id is not None
        or sp.merged_into is not None
    ):
        db.commit()
        return None
    email = (person_email or "").strip().lower() or None
    if suggest_only:
        if sp.suggested_name == name and sp.suggested_email == email:
            db.commit()
            return None
        sp.suggested_name = name
        sp.suggested_email = email
        sp.suggestion_score = float(confidence)
    else:
        unchanged = (
            sp.display_name == name
            and sp.person_email == email
            and abs(float(sp.name_confidence or 0.0) - float(confidence)) < 0.02
        )
        if unchanged:
            db.commit()
            return None
        sp.display_name = name
        sp.name_source = source if source in ("self_intro", "inferred") else "inferred"
        sp.name_confidence = float(confidence)
        sp.name_evidence = (evidence or "")[:300] or None
        sp.person_email = email
        sp.suggested_name = None
        sp.suggested_email = None
    db.flush()
    data = speaker_out(db, sp)
    event = append_event(db, session_id, "speaker.update", data)
    db.commit()
    log.info(
        "live.speaker_named",
        session_id=str(session_id),
        label=label,
        source=source,
        confidence=round(float(confidence), 3),
        suggest_only=suggest_only,
    )
    return event


def clear_auto_name(
    db: Session, session_id: UUID, label: str, *, allow_terminal: bool = False
) -> dict | None:
    """Back to "Speaker N" when an automatic name lost its evidence (re-processing)."""
    sp = _speaker_row(db, session_id, label)
    status = db.scalar(select(LiveSession.status).where(LiveSession.id == session_id))
    live = status in ("live", "stopping") or (allow_terminal and status == "finalized")
    if (
        sp is None
        or not live
        or sp.name_source not in ("self_intro", "inferred")
        or sp.user_id is not None
    ):
        db.commit()
        return None
    sp.display_name = f"Speaker {label.lstrip('S')}"
    sp.name_source = None
    sp.name_confidence = None
    sp.name_evidence = None
    sp.person_email = None
    db.flush()
    event = append_event(db, session_id, "speaker.update", speaker_out(db, sp))
    db.commit()
    return event


def apply_self_intro(
    db: Session,
    session_id: UUID,
    label: str,
    display_name: str,
    *,
    confidence: float,
    evidence: str,
) -> dict | None:
    """Name a speaker from their self-introduction; never overrides a manual name.

    Returns the `speaker.update` event (with `source`, `confidence`,
    `evidence`), or None when the speaker is gone, manually named, bound to a
    user, already carries this name, or the session is no longer live.
    """
    sp = db.scalar(
        select(LiveSpeaker)
        .where(LiveSpeaker.session_id == session_id, LiveSpeaker.label == label)
        .with_for_update()
    )
    status = db.scalar(select(LiveSession.status).where(LiveSession.id == session_id))
    name = " ".join((display_name or "").split())[:200]
    if (
        sp is None
        or not name
        or status not in ("live", "stopping")
        or sp.name_source == "manual"
        or sp.user_id is not None
        or sp.merged_into is not None
        or sp.display_name == name
    ):
        db.commit()
        return None
    sp.display_name = name
    sp.name_source = "self_intro"
    sp.name_confidence = float(confidence)
    sp.name_evidence = (evidence or "")[:300] or None
    db.flush()
    data = {
        **speaker_out(db, sp),
        "confidence": round(float(confidence), 3),
        "evidence": (evidence or "")[:160],
    }
    event = append_event(db, session_id, "speaker.update", data)
    db.commit()
    log.info(
        "live.speaker_self_intro",
        session_id=str(session_id),
        label=label,
        confidence=round(float(confidence), 3),
    )
    return event


def set_suggestion(
    db: Session, session_id: UUID, label: str, user_id: UUID, score: float
) -> dict | None:
    sp = db.scalar(
        select(LiveSpeaker)
        .where(LiveSpeaker.session_id == session_id, LiveSpeaker.label == label)
        .with_for_update()
    )
    if sp is None or sp.user_id is not None:
        db.commit()
        return None
    if sp.suggested_user_id == user_id:
        sp.suggestion_score = score
        db.commit()
        return None
    sp.suggested_user_id = user_id
    sp.suggestion_score = score
    db.flush()
    event = append_event(db, session_id, "speaker.update", speaker_out(db, sp))
    db.commit()
    return event


# ---- audio ledger -----------------------------------------------------------


def persist_audio_chunk(
    db: Session, session_id: UUID, chunk_seq: int, start_sample: int, pcm: bytes
) -> int:
    """Advance the durable audio ledger for one chunk. Returns samples.

    RGS does not keep call audio: only the transcript is stored. The ledger
    still records how much audio arrived (duration, idle detection). Owners
    opted in to short retention (`audio_store`) also keep the chunk itself.
    """
    s = db.scalar(select(LiveSession).where(LiveSession.id == session_id))
    if s is None:
        raise HTTPException(404, "Live session not found.")
    if s.audio_chunk_count != chunk_seq or s.audio_samples != start_sample:
        raise RuntimeError(
            f"audio ledger mismatch: have chunk {s.audio_chunk_count}/{s.audio_samples}, "
            f"got {chunk_seq}/{start_sample}"
        )
    samples = start_sample + len(pcm) // 2
    db.execute(
        update(LiveSession)
        .where(LiveSession.id == session_id, LiveSession.audio_chunk_count == chunk_seq)
        .values(
            audio_chunk_count=chunk_seq + 1,
            audio_samples=samples,
            last_audio_at=datetime.now(UTC),
        )
    )
    audio_store.keep_chunk(db, s, chunk_seq, start_sample, pcm)
    db.commit()
    return samples


# ---- findings + triggers ------------------------------------------------------


def _clean_links(raw: list[Any] | None) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for item in raw or []:
        if isinstance(item, str):
            item = {"title": item, "url": item}
        if not isinstance(item, dict):
            raise HTTPException(400, "links must be objects with title and url.")
        url = str(item.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or len(url) > 2048:
            raise HTTPException(400, "links must be http(s) URLs.")
        title = str(item.get("title") or url).strip()[:200]
        links.append({"title": title, "url": url})
    if len(links) > MAX_LINKS:
        raise HTTPException(400, f"At most {MAX_LINKS} links per finding.")
    return links


def create_finding(
    db: Session,
    session: LiveSession,
    user: User | None,
    *,
    title: str,
    body_md: str,
    links: list[Any] | None = None,
    related_seq: int | None = None,
    trigger_id: UUID | None = None,
    source_agent: str | None = None,
) -> tuple[dict, dict | None, bool]:
    """Returns `(finding, event_or_None, deduped)`; caller publishes events."""
    require_contributor(session, user)
    settings = get_live_settings()
    title = (title or "").strip()
    body_md = (body_md or "").strip()
    if not 1 <= len(title) <= 200:
        raise HTTPException(400, "title must be 1..200 characters.")
    if len(body_md) > 4000:
        raise HTTPException(400, "body_md must be at most 4000 characters.")
    clean_links = _clean_links(links)
    agent = re.sub(r"[^\w.:@/ -]", "", (source_agent or "agent").strip())[:64] or "agent"
    now = datetime.now(UTC)
    # Serialize finding writers on the session row (rate limit + dedupe).
    s = db.scalar(
        select(LiveSession)
        .where(LiveSession.id == session.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if s is None:
        raise HTTPException(404, "Live session not found.")
    if s.status in TERMINAL_STATUSES and (
        s.finalized_at is None or now - s.finalized_at > FINDINGS_GRACE_AFTER_STOP
    ):
        raise HTTPException(409, "This live session has ended.")
    if related_seq is not None and not 0 < related_seq <= s.last_seq:
        raise HTTPException(400, "related_seq does not exist in this session.")
    trigger = None
    if trigger_id is not None:
        trigger = db.scalar(
            select(LiveTrigger)
            .where(LiveTrigger.id == trigger_id, LiveTrigger.session_id == s.id)
            .with_for_update()
        )
        if trigger is None:
            raise HTTPException(404, "Trigger not found in this session.")
        if trigger.finding_id is not None:
            prior = db.get(LiveFinding, trigger.finding_id)
            if prior is not None:
                db.commit()
                return finding_out(db, prior), None, True
    key = title_key(title)
    dup = db.scalar(
        select(LiveFinding)
        .where(
            LiveFinding.session_id == s.id,
            LiveFinding.title_key == key,
            LiveFinding.created_at >= now - FINDING_DEDUPE_WINDOW,
        )
        .limit(1)
    )
    if dup is not None:
        db.commit()
        return finding_out(db, dup), None, True
    recent = db.scalar(
        select(func.count(LiveFinding.id)).where(
            LiveFinding.session_id == s.id,
            LiveFinding.created_at >= now - timedelta(minutes=1),
        )
    )
    if (recent or 0) >= settings.live_findings_per_minute:
        db.rollback()
        raise HTTPException(429, "Too many findings for this session; slow down.")
    finding = LiveFinding(
        id=uuid4(),
        session_id=s.id,
        seq=0,
        title=title,
        title_key=key,
        body_md=body_md,
        links=clean_links,
        related_seq=related_seq,
        trigger_id=trigger.id if trigger else None,
        source_agent=agent,
        created_by_user_id=persisted_user_id(db, user),
        created_at=now,
    )
    db.add(finding)
    db.flush()
    event = append_event(db, s.id, "finding.new", {"seq": None, **finding_out(db, finding)})
    finding.seq = event["seq"]
    event["data"]["seq"] = finding.seq
    if trigger is not None:
        trigger.status = "answered"
        trigger.finding_id = finding.id
    db.commit()
    return finding_out(db, finding), event, False


def list_findings(db: Session, session: LiveSession) -> list[dict]:
    rows = db.scalars(
        select(LiveFinding)
        .where(LiveFinding.session_id == session.id)
        .order_by(LiveFinding.seq)
        .limit(500)
    ).all()
    return [finding_out(db, f) for f in rows]


def _expire_triggers(db: Session, session_id: UUID) -> None:
    settings = get_live_settings()
    now = datetime.now(UTC)
    db.execute(
        update(LiveTrigger)
        .where(
            LiveTrigger.session_id == session_id,
            LiveTrigger.status == "pending",
            LiveTrigger.created_at < now - timedelta(seconds=settings.live_trigger_expire_s),
        )
        .values(status="expired")
    )
    # A claim that never produced a finding goes back to pending.
    db.execute(
        update(LiveTrigger)
        .where(
            LiveTrigger.session_id == session_id,
            LiveTrigger.status == "claimed",
            LiveTrigger.claimed_at < now - timedelta(seconds=settings.live_trigger_claim_ttl_s),
        )
        .values(status="pending", claimed_by=None, claimed_by_user_id=None, claimed_at=None)
    )


def list_triggers(db: Session, session: LiveSession, status: str | None = "pending") -> list[dict]:
    _expire_triggers(db, session.id)
    db.commit()
    stmt = select(LiveTrigger).where(LiveTrigger.session_id == session.id)
    if status:
        stmt = stmt.where(LiveTrigger.status == status)
    rows = db.scalars(stmt.order_by(LiveTrigger.seq).limit(200)).all()
    return [trigger_out(t) for t in rows]


def publish_trigger(db: Session, session_id: UUID, trigger: GateTrigger) -> dict | None:
    """Persist a gate trigger + its durable `gate.trigger` event (idempotent)."""
    if trigger.action not in GATE_ACTIONS:
        log.warning("live.gate_trigger_bad_action", action=trigger.action)
        return None
    query = (trigger.query or "").strip()[:1000]
    if not query:
        return None
    try:
        tid = UUID(str(trigger.trigger_id))
    except ValueError:
        tid = uuid4()
    if db.get(LiveTrigger, tid) is not None:
        return None
    s = db.scalar(select(LiveSession).where(LiveSession.id == session_id))
    if s is None or s.status in TERMINAL_STATUSES:
        return None
    probability = max(0.0, min(1.0, float(trigger.probability)))
    anchor = getattr(trigger, "anchor_seq", None)
    topic = (getattr(trigger, "topic", None) or "").strip()[:500] or None
    context_text = (getattr(trigger, "context_text", None) or "")[-4000:] or None
    decided_at = _as_datetime(getattr(trigger, "decided_at", None))
    provider = (getattr(trigger, "provider", None) or None) and str(trigger.provider)[:16]
    row = LiveTrigger(
        id=tid,
        session_id=session_id,
        seq=0,
        action=trigger.action,
        query=query,
        seq_from=int(trigger.seq_from),
        seq_to=int(trigger.seq_to),
        probability=probability,
        jev_latency_ms=trigger.jev_latency_ms,
        provider=provider,
        anchor_seq=int(anchor) if anchor is not None else None,
        topic=topic,
        context_text=context_text,
        decided_at=decided_at,
        status="pending",
    )
    data = {
        "trigger_id": str(tid),
        "action": trigger.action,
        "query": query,
        "seq_from": row.seq_from,
        "seq_to": row.seq_to,
        "probability": probability,
        "jev_latency_ms": trigger.jev_latency_ms,
        "provider": provider,
        "anchor_seq": row.anchor_seq,
        "topic": topic,
        "context_text": context_text,
        "decided_at": _iso(decided_at) if decided_at else None,
    }
    event = append_event(db, session_id, "gate.trigger", data)
    row.seq = event["seq"]
    db.add(row)
    db.commit()
    return event


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def claim_trigger(
    db: Session,
    session: LiveSession,
    trigger_id: UUID,
    user: User | None,
    claimed_by: str | None,
) -> tuple[bool, dict, dict | None]:
    """First-wins claim. Returns `(won, trigger, event_or_None)`."""
    require_contributor(session, user)
    _expire_triggers(db, session.id)
    t = db.scalar(
        select(LiveTrigger)
        .where(LiveTrigger.id == trigger_id, LiveTrigger.session_id == session.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if t is None:
        db.rollback()
        raise HTTPException(404, "Trigger not found in this session.")
    if t.status != "pending":
        db.commit()
        return False, trigger_out(t), None
    now = datetime.now(UTC)
    t.status = "claimed"
    t.claimed_by = re.sub(r"[^\w.:@/ -]", "", (claimed_by or "agent").strip())[:64] or "agent"
    t.claimed_by_user_id = persisted_user_id(db, user)
    t.claimed_at = now
    db.flush()
    event = append_event(
        db,
        session.id,
        "gate.claimed",
        {"trigger_id": str(t.id), "claimed_by": t.claimed_by, "claimed_at": _iso(now)},
    )
    db.commit()
    return True, trigger_out(t), event


def recent_segments(db: Session, session_id: UUID, limit: int) -> list[LiveSegment]:
    rows = db.scalars(
        select(LiveSegment)
        .where(LiveSegment.session_id == session_id)
        .order_by(LiveSegment.seq.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))
