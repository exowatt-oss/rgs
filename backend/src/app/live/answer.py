"""Built-in LLM answerer: the sidekick that answers gate triggers (and "ask" questions).

When a `gate.trigger` is still unclaimed `LIVE_FALLBACK_AFTER_S` after it was
published, the runtime asks this answerer to take it: claim as `rgs`
(first-wins — an external agent that claimed in time keeps it), ask an
optional operator-configured Anthropic-compatible HTTP endpoint
to answer *grounded in the live transcript*, and post a short finding.

RGS is standalone, so answers are grounded in the call itself plus
general knowledge for definitions, and says plainly when the call does not
contain the answer. When the gateway is not configured or fails, the claim is
released as `expired` (no finding) so another agent can still answer.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select, update

from app.db import SessionLocal
from app.live import service
from app.live.settings import get_live_settings
from app.models.live import LiveSegment, LiveSession, LiveSpeaker, LiveTrigger

log = structlog.get_logger(__name__)

AGENT_NAME = "rgs"
MAX_TRANSCRIPT_CHARS = 14000
ACTION_TITLES = {
    "lookup_past": "Earlier in this call",
    "fact_check": "Fact check",
    "find_owner": "Who owns this",
    "recall_action_items": "Action items",
    "define_term": "Definition",
    "look_up": "Look-up",
    "ask": "Answer",
}
ACTION_HINTS = {
    "find_owner": "Name the person or team the call says owns or handles it.",
    "lookup_past": "Say what was decided, said or agreed about it.",
    "fact_check": "Check the stated figure, date or fact against what was said.",
    "recall_action_items": "List the commitments / follow-ups with owners.",
    "define_term": "Define the term in one or two sentences (general knowledge is fine).",
    "look_up": (
        "Answer what the call itself shows; if it needs records you were not given "
        "(code history, tickets, documents), say so in one sentence and name what to check."
    ),
    "ask": "Answer the question.",
}

SYSTEM_PROMPT = (
    "You are RGS, an assistant listening alongside the people "
    "on a live call. Answer the question that just came up, "
    "grounded in the call transcript you are given. Rules: be brief (at most 60 words, plain "
    "markdown, no preamble); cite who said what when relevant; never invent names, numbers or "
    "decisions; if the transcript does not contain the answer say so in one short sentence "
    "(for definitions you may use general knowledge). The newest words may still be arriving: "
    "use the not-yet-final tail too. Reply by calling the `answer` tool."
)

ANSWER_TOOL = {
    "name": "answer",
    "description": "Post the answer card shown to everyone on the call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "<= 8 words that answer the question"},
            "answer_md": {"type": "string", "description": "<= 60 words, plain markdown"},
            "grounded": {
                "type": "boolean",
                "description": "true when the transcript supports the answer",
            },
        },
        "required": ["title", "answer_md", "grounded"],
    },
}


class Responder(Protocol):
    def respond(self, session_id: UUID, trigger_id: UUID) -> list[dict]: ...


class LLMError(Exception):
    pass


def gateway_config() -> tuple[str, str] | None:
    """Optional Anthropic-compatible endpoint. Never inherit unrelated LLM env vars."""
    base = os.environ.get("RGS_ANSWER_API_URL", "")
    key = os.environ.get("RGS_ANSWER_API_KEY", "")
    if not base.strip() or not key.strip():
        return None
    return base.strip().rstrip("/"), key.strip()


class GatewayLLM:
    """Minimal Anthropic-Messages-compatible HTTP client."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    @property
    def configured(self) -> bool:
        return gateway_config() is not None

    def complete(self, system: str, user: str, *, max_tokens: int = 400) -> str:
        cfg = gateway_config()
        if cfg is None:
            raise LLMError("LLM gateway not configured")
        base, key = cfg
        settings = get_live_settings()
        try:
            with httpx.Client(
                timeout=settings.live_answer_timeout_s, transport=self._transport
            ) as client:
                resp = client.post(
                    f"{base}/v1/messages",
                    headers={
                        "x-api-key": key,
                        "authorization": f"Bearer {key}",
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": settings.live_answer_model,
                        "max_tokens": max_tokens,
                        "system": system,
                        "messages": [{"role": "user", "content": user}],
                        "tools": [ANSWER_TOOL],
                        "tool_choice": {"type": "tool", "name": ANSWER_TOOL["name"]},
                    },
                )
        except httpx.HTTPError as exc:
            raise LLMError(f"gateway call failed: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"gateway HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        for part in data.get("content") or []:
            if part.get("type") == "tool_use" and isinstance(part.get("input"), dict):
                return json.dumps(part["input"])
        parts = [p.get("text", "") for p in data.get("content") or [] if p.get("type") == "text"]
        return "".join(parts).strip()


def _transcript_text(db, session_id: UUID, upto_seq: int | None = None) -> str:
    names = {
        sp.label: sp.display_name
        for sp in db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == session_id))
    }
    stmt = select(LiveSegment).where(LiveSegment.session_id == session_id)
    if upto_seq:
        stmt = stmt.where(LiveSegment.seq <= upto_seq)
    rows = db.scalars(stmt.order_by(LiveSegment.seq.desc()).limit(400)).all()
    lines: list[str] = []
    total = 0
    for seg in rows:  # newest first, so the budget keeps the most recent lines
        line = f"{names.get(seg.speaker_label, seg.speaker_label)}: {seg.text}"
        total += len(line) + 1
        if total > MAX_TRANSCRIPT_CHARS:
            break
        lines.append(line)
    return "\n".join(reversed(lines))


def parse_answer(raw: str) -> dict[str, Any] | None:
    """Pull `{title, answer_md, grounded}` out of the model reply (tolerates fences)."""
    text = raw.strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and str(data.get("answer_md") or "").strip():
                return {
                    "title": str(data.get("title") or "").strip()[:120],
                    "answer_md": str(data["answer_md"]).strip()[:3500],
                    "grounded": bool(data.get("grounded", True)),
                }
        except ValueError:
            pass
        # Malformed JSON (e.g. unescaped quotes in the answer): pull the fields.
        body = re.search(
            r'"answer_md"\s*:\s*"(.*?)"\s*(?:,\s*"(?:grounded|title)"|\}\s*$)', text, re.S
        )
        if body:
            head = re.search(r'"title"\s*:\s*"(.*?)"\s*,', text, re.S)
            return {
                "title": (head.group(1) if head else "").strip()[:120],
                "answer_md": body.group(1).replace('\\"', '"').strip()[:3500],
                "grounded": not re.search(r'"grounded"\s*:\s*false', text),
            }
    if text:
        return {"title": "", "answer_md": text[:3500], "grounded": True}
    return None


def build_prompt(
    *, title: str, action: str, query: str, context: str, transcript: str, tail: str = ""
) -> str:
    hint = ACTION_HINTS.get(action, ACTION_HINTS["ask"])
    parts = [f"Call: {title}", f"Question type: {action}. {hint}", f"Question: {query}"]
    if context:
        parts.append(f"Lines around the moment it came up:\n{context}")
    parts.append(f"Transcript so far (oldest first):\n{transcript or '(nothing yet)'}")
    if tail:
        parts.append(f"Still being transcribed (newest, not final):\n{tail}")
    return "\n\n".join(parts)


class LLMAnswerer:
    def __init__(
        self,
        llm: GatewayLLM | None = None,
        *,
        tail: Callable[[UUID], str] | None = None,
    ) -> None:
        self.llm = llm or GatewayLLM()
        # Live not-yet-final text for a session (the runtime's interim tail).
        self.tail = tail

    def answer(
        self, db, session: LiveSession, *, action: str, query: str, context: str, upto_seq=None
    ) -> dict[str, Any] | None:
        transcript = _transcript_text(db, session.id, upto_seq)
        tail = ""
        if self.tail is not None:
            try:
                tail = (self.tail(session.id) or "").strip()
            except Exception:  # noqa: BLE001 — the tail is a nicety
                tail = ""
        db.rollback()  # never hold a transaction across the LLM call
        raw = self.llm.complete(
            SYSTEM_PROMPT,
            build_prompt(
                title=session.title,
                action=action,
                query=query,
                context=context,
                transcript=transcript,
                tail=tail,
            ),
        )
        return parse_answer(raw)

    def respond(self, session_id: UUID, trigger_id: UUID) -> list[dict]:
        events: list[dict] = []
        if not self.llm.configured:
            return events
        with SessionLocal() as db:
            session = db.get(LiveSession, session_id)
            if session is None or session.status in service.TERMINAL_STATUSES:
                return events
            won, trigger, claim_event = service.claim_trigger(
                db, session, trigger_id, None, AGENT_NAME
            )
            if not won:
                return events
            if claim_event is not None:
                events.append(claim_event)
            try:
                result = self.answer(
                    db,
                    session,
                    action=trigger["action"],
                    query=trigger["query"],
                    context=trigger.get("context_text") or "",
                    upto_seq=None,
                )
            except Exception as exc:  # noqa: BLE001 — the gateway may be down / over budget
                log.warning("live.answer_failed", error=str(exc)[:200])
                db.rollback()
                result = None
            if not result:
                self._release(db, trigger_id)
                return events
            label = ACTION_TITLES.get(trigger["action"], "Answer")
            title = result["title"] or trigger["query"]
            try:
                session = db.get(LiveSession, session_id)
                _finding, event, _dedup = service.create_finding(
                    db,
                    session,
                    None,
                    title=f"{label}: {title}"[:200],
                    body_md=result["answer_md"],
                    related_seq=trigger.get("anchor_seq") or trigger["seq_to"] or None,
                    trigger_id=trigger_id,
                    source_agent=AGENT_NAME,
                )
            except Exception as exc:  # noqa: BLE001 — e.g. rate limited
                log.info("live.answer_post_failed", error=str(exc)[:200])
                db.rollback()
                self._release(db, trigger_id)
                return events
            if event is not None:
                events.append(event)
        return events

    def ask(self, session_id: UUID, question: str, user_id: UUID | None) -> tuple[dict, dict]:
        """Answer a typed question now; returns `(finding, event)`."""
        from fastapi import HTTPException

        if not self.llm.configured:
            raise HTTPException(503, "The LLM gateway is not configured on this deployment.")
        with SessionLocal() as db:
            session = db.get(LiveSession, session_id)
            if session is None:
                raise HTTPException(404, "Live session not found.")
            try:
                result = self.answer(db, session, action="ask", query=question, context="")
            except LLMError as exc:
                raise HTTPException(502, f"The sidekick could not answer: {exc}") from exc
            if not result:
                raise HTTPException(502, "The sidekick returned no answer.")
            from app.models.user import User

            user = db.get(User, user_id) if user_id else None
            session = db.get(LiveSession, session_id)
            finding, event, _dedup = service.create_finding(
                db,
                session,
                user,
                title=f"Q: {question}"[:200],
                body_md=result["answer_md"],
                source_agent=AGENT_NAME,
            )
            return finding, event

    @staticmethod
    def _release(db, trigger_id: UUID) -> None:
        db.execute(
            update(LiveTrigger)
            .where(LiveTrigger.id == trigger_id, LiveTrigger.claimed_by == AGENT_NAME)
            .values(status="expired", claimed_at=datetime.now(UTC))
        )
        db.commit()
