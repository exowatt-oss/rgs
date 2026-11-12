"""Run the naming engine (`app.live.naming`) against a stored session.

Sync (SQLAlchemy); the live runtime calls it in a worker thread, and
`scripts/rename_session.py` calls it for a finished session. Loads the
session's segments + speakers, the local directory directory and priors, runs
`naming.run`, and applies the result through `service.apply_name` /
`service.merge_speaker` (both refuse to touch manual names).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from app.db import SessionLocal
from app.live import naming, service
from app.live.people import Person, attendee_people, colleagues, get_directory
from app.live.settings import LiveSettings, get_live_settings
from app.models.live import LiveSegment, LiveSession, LiveSpeaker
from app.models.user import User

log = structlog.get_logger(__name__)

RECENT_DAYS = 30


@dataclass
class PassOutcome:
    events: list[dict] = field(default_factory=list)
    merges: list[tuple[str, str]] = field(default_factory=list)  # (label, into) applied
    result: naming.NamingResult | None = None
    skipped: str | None = None


def _views(rows: list[LiveSpeaker]) -> list[naming.SpeakerView]:
    return [
        naming.SpeakerView(
            label=sp.label,
            display_name=sp.display_name,
            source=sp.name_source or "default",
            confidence=float(sp.name_confidence) if sp.name_confidence is not None else None,
            person_email=sp.person_email,
            speech_seconds=float(sp.speech_seconds or 0.0),
            merged_into=sp.merged_into,
            bound=sp.user_id is not None,
        )
        for sp in rows
    ]


def build_context(
    session_id: UUID, *, people: list[Person] | None = None
) -> naming.NamingContext | None:
    with SessionLocal() as db:
        s = db.get(LiveSession, session_id)
        if s is None:
            return None
        rows = list(db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == session_id)))
        segs = db.execute(
            select(
                LiveSegment.seq,
                LiveSegment.speaker_label,
                LiveSegment.start_seconds,
                LiveSegment.end_seconds,
                LiveSegment.text,
            )
            .where(LiveSegment.session_id == session_id)
            .order_by(LiveSegment.seq)
        ).all()
        owner_user = db.get(User, s.started_by_user_id) if s.started_by_user_id else None
        attendees = service.attendees_of(s)
        since = datetime.now(UTC) - timedelta(days=RECENT_DAYS)
        recent = frozenset(
            e.lower()
            for e in db.scalars(
                select(LiveSpeaker.person_email)
                .join(LiveSession, LiveSession.id == LiveSpeaker.session_id)
                .where(
                    LiveSession.started_by_user_id == s.started_by_user_id,
                    LiveSession.id != session_id,
                    LiveSession.started_at >= since,
                    LiveSpeaker.person_email.is_not(None),
                )
                .limit(200)
            )
            if e
        )
        title, kind, expected = s.title or "", s.kind or "call", s.expected_speakers
    if people is None:
        people = get_directory().people()
    owner = None
    if owner_user is not None:
        owner = next((p for p in people if p.email == (owner_user.email or "").lower()), None)
        if owner is None and owner_user.email:
            owner = Person(email=owner_user.email.lower(), name=owner_user.name or owner_user.email)
    return naming.NamingContext(
        fragments=[
            (int(q), lab, float(a or 0), float(b or 0), t or "") for q, lab, a, b, t in segs
        ],
        speakers=_views(rows),
        people=people,
        attendees=attendee_people(attendees, people),
        owner=owner,
        colleagues=colleagues(people, owner),
        recent_emails=recent,
        title=title,
        kind=kind,
        expected_speakers=expected,
    )


def gateway_llm(settings: LiveSettings) -> Callable[[str], dict[str, Any]] | None:
    from app.live.answer import gateway_config

    if not settings.live_naming_enabled or gateway_config() is None:
        return None

    def call(prompt: str) -> dict[str, Any]:
        return naming.call_gateway(
            prompt, model=settings.live_naming_model, timeout_s=settings.live_naming_timeout_s
        )

    return call


def apply(
    session_id: UUID,
    ctx: naming.NamingContext,
    result: naming.NamingResult,
    *,
    allow_terminal: bool = False,
) -> PassOutcome:
    out = PassOutcome(result=result)
    views = ctx.by_label
    for m in result.merges:
        with SessionLocal() as db:
            if allow_terminal:
                events = _merge_terminal(db, session_id, m)
            else:
                events = service.merge_speaker(
                    db,
                    session_id,
                    m.label,
                    m.into,
                    manual=False,
                    confidence=m.confidence,
                    reason=m.reason,
                )
        if events:
            out.events.extend(events)
            out.merges.append((m.label, m.into))
    # A label carrying two voices is never pinned: drop its automatic name
    # (a capped suggestion may follow below).
    for label in result.mixed:
        sp = views.get(label)
        if sp is None or sp.locked or sp.merged_into or sp.source not in ("self_intro", "inferred"):
            continue
        with SessionLocal() as db:
            event = service.clear_auto_name(db, session_id, label, allow_terminal=allow_terminal)
        if event is not None:
            out.events.append(event)
            views = {
                **views,
                label: dataclasses.replace(
                    sp,
                    display_name=f"Speaker {label.lstrip('S')}",
                    source="default",
                    confidence=None,
                    person_email=None,
                ),
            }
    for d in result.names:
        sp = views.get(d.label)
        if sp is None or not naming.should_apply(d, sp):
            continue
        with SessionLocal() as db:
            event = service.apply_name(
                db,
                session_id,
                d.label,
                d.name,
                source=d.source,
                confidence=d.confidence,
                evidence=d.evidence,
                person_email=d.email,
                suggest_only=d.suggest_only,
                allow_terminal=allow_terminal,
            )
        if event is not None:
            out.events.append(event)
    return out


def _merge_terminal(db, session_id: UUID, m: naming.MergeDecision) -> list[dict]:
    """Merge on a finished session (re-processing): same rules as an automatic merge."""
    rows = {
        sp.label: sp
        for sp in db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == session_id))
    }
    src = rows.get(m.label)
    if src is None or src.name_source == "manual" or src.user_id is not None:
        return []
    return service.merge_speaker(
        db, session_id, m.label, m.into, manual=True, confidence=m.confidence, reason=m.reason
    )


def fresh_context(ctx: naming.NamingContext) -> tuple[naming.NamingContext, set[str]]:
    """`ctx` with automatic names reset to "Speaker N", and the labels that had one."""
    auto: set[str] = set()
    views = []
    for sp in ctx.speakers:
        if sp.source in ("self_intro", "inferred") and not sp.bound:
            auto.add(sp.label)
            sp = dataclasses.replace(
                sp,
                display_name=f"Speaker {sp.label.lstrip('S')}",
                source="default",
                confidence=None,
                person_email=None,
            )
        views.append(sp)
    return dataclasses.replace(ctx, speakers=views, _utterances=None), auto


def run_pass(
    session_id: UUID,
    *,
    use_llm: bool,
    allow_terminal: bool = False,
    settings: LiveSettings | None = None,
    people: list[Person] | None = None,
    llm: Callable[[str], dict[str, Any]] | None = None,
    fresh: bool = False,
) -> PassOutcome:
    """One naming pass. `fresh` re-derives every automatic name from scratch
    (re-processing with new rules): automatic names are ignored as input, and
    one left without any evidence goes back to "Speaker N". Manual names stay.
    """
    settings = settings or get_live_settings()
    if not settings.live_naming_enabled:
        return PassOutcome(skipped="disabled")
    ctx = build_context(session_id, people=people)
    if ctx is None:
        return PassOutcome(skipped="not found")
    if not ctx.fragments:
        return PassOutcome(skipped="no text")
    auto: set[str] = set()
    if fresh:
        ctx, auto = fresh_context(ctx)
    if use_llm and llm is None:
        llm = gateway_llm(settings)
    result = naming.run(
        ctx,
        llm=llm if use_llm else None,
        min_confidence=settings.live_naming_min_confidence,
        suggest_confidence=settings.live_naming_suggest_confidence,
    )
    outcome = apply(session_id, ctx, result, allow_terminal=allow_terminal)
    decided = {d.label for d in result.names if not d.suggest_only}
    for label in sorted(auto - decided):
        with SessionLocal() as db:
            event = service.clear_auto_name(db, session_id, label, allow_terminal=allow_terminal)
        if event is not None:
            outcome.events.append(event)
    log.info(
        "live.naming_pass",
        session_id=str(session_id),
        llm=result.used_llm,
        llm_error=result.llm_error,
        names=[(d.label, d.name, d.confidence, d.suggest_only) for d in result.names],
        merges=[(m.label, m.into, m.confidence) for m in result.merges],
        applied=len(outcome.events),
    )
    return outcome


def needs_llm(ctx_speakers: list[naming.SpeakerView], *, min_speech_s: float = 3.0) -> bool:
    """An active, unlocked speaker with real speech still lacks a sure name."""
    return any(
        sp.merged_into is None
        and not sp.locked
        and sp.speech_seconds >= min_speech_s
        and (not sp.named or (sp.confidence or 0.0) < 0.85)
        for sp in ctx_speakers
    )
