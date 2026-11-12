"""Seam between the live session loop and an optional decision gate.

The gate decides *when* an agent should speak up during a live session (for
example the Jev gate in `app.live.jev_gate`, owned separately). This module
is the only contract both sides share, so either can land first:

* The session runtime builds a `GateSessionContext` roughly every
  `LIVE_GATE_INTERVAL_S` seconds and after each batch of finalized segments,
  and awaits `gate.observe(ctx)`.
* The gate reports a decision either by awaiting `ctx.on_trigger(trigger)`
  (preferred: publishes immediately) or by returning triggers from
  `observe` (published after it returns). Both may be used; duplicates are
  dropped by `GateTrigger.trigger_id`.
* Publishing persists a `live_triggers` row and a durable `gate.trigger`
  event; agents then claim (`claim_live_trigger`) and answer with a finding
  that references the `trigger_id`.

Loading: `load_gate()` imports `app.live.jev_gate` lazily and calls its
`create_gate() -> LiveGate | None`. A missing module, a `None` return or an
import/constructor error all mean "no gate" — the live session never fails
because of the gate. `observe` errors are logged and swallowed by the runtime,
and every call is bounded by `LIVE_GATE_TIMEOUT_S`.

Optional lifecycle hooks (called only when the gate object has them; errors
are swallowed): `mark_resolved(trigger_id, session_id=...)` when a finding
answers a trigger or the built-in responder gives up on it,
`note_surfaced(session_id, text)` for unprompted findings,
`end_session(session_id)` after a session stops, `warmup()` once after
loading and `aclose()` at app shutdown.

Providers (v2): the gate may be backed by more than one decision provider
(TypeSafe Jev, OpenAI Decisions). `ctx.gate_provider` names the session's
choice; the gate reports which provider actually decided on every trigger
(`GateTrigger.provider`) and, after every provider call, awaits
`ctx.on_tick(GateTick)` so the UI can show a "listening" heartbeat. Optional
`confirm_intro(...)` lets the runtime confirm a self-introduction with the
session's provider (see `app.live.intro`).
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)

GateAction = Literal[
    "lookup_past", "fact_check", "find_owner", "recall_action_items", "define_term", "look_up"
]
GATE_ACTIONS: frozenset[str] = frozenset(
    {"lookup_past", "fact_check", "find_owner", "recall_action_items", "define_term", "look_up"}
)

# What the live page shows while listening (`GET /api/live/config` → `gate.actions`):
# one entry per gate action plus the provider-agnostic self-introduction.
# Fictional examples; no production meeting material.
GATE_ACTION_INFO: tuple[dict[str, object], ...] = (
    {
        "id": "find_owner",
        "label": "Find the owner",
        "description": "Ask who owns, handles or is the contact for something.",
        "examples": [
            "Who owns the sample project?",
            "Who's the point of contact for the demo?",
            "Who's handling the documentation?",
        ],
    },
    {
        "id": "lookup_past",
        "label": "Look up a past decision",
        "description": "Ask what an earlier meeting decided, said or agreed.",
        "examples": [
            "What did we decide last time about the demo schedule?",
            "Remind me where we landed on the release date.",
            "Did we ever settle on a sample format?",
        ],
    },
    {
        "id": "fact_check",
        "label": "Check a number or fact",
        "description": "Say a figure or date you're unsure of, or ask to confirm one.",
        "examples": [
            "I think the estimate was around twenty hours, right?",
            "Wasn't the deadline the fifteenth?",
            "Are we sure the test run was two minutes?",
        ],
    },
    {
        "id": "recall_action_items",
        "label": "Recall action items",
        "description": "Ask what people committed to or which follow-ups are open.",
        "examples": [
            "What were our fictional action items from last week?",
            "What did we say we'd follow up on after the demo?",
        ],
    },
    {
        "id": "define_term",
        "label": "Define a term",
        "description": "Ask what an acronym, code name or bit of jargon means.",
        "examples": [
            "What does API stand for?",
            "Sorry, what's a JSON file again?",
        ],
    },
    {
        "id": "look_up",
        "label": "Look something up",
        "description": "Ask to check or find a fact: who made something, when it last happened.",
        "examples": [
            "Can you look up who made the demo dashboard?",
            "Can we check when I last pushed the sample code?",
            "Check when the example task was filed.",
        ],
    },
    {
        "id": "speaker_intro",
        "label": "Name yourself",
        "description": "Say who you are and your speaker label becomes your name.",
        "examples": [
            "Hi, I'm Alice.",
            "This is Bob from the sample team.",
            "My name is Carol, I write documentation.",
        ],
    },
)


@dataclass(frozen=True)
class GateSegment:
    """One finalized live segment as the gate sees it."""

    seq: int
    speaker: str  # stable session label, e.g. "S2"
    speaker_name: str  # display name ("Speaker 2" or a confirmed name)
    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class GateTrigger:
    """A gate decision: "an agent should look this up now"."""

    action: GateAction
    query: str
    seq_from: int
    seq_to: int
    probability: float
    # Provider round-trip for the decision (historical name: Jev was first).
    jev_latency_ms: int | None = None
    trigger_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # Optional context so an agent can answer without re-reading the
    # transcript: the line that fired, the focused sentence, the lines the
    # gate saw ("Speaker: text", <= 3000 chars) and when it decided.
    anchor_seq: int | None = None
    topic: str | None = None
    context_text: str | None = None
    decided_at: datetime | None = None
    provider: str | None = None  # "jev" | "openai"; None = unknown (custom gate)


@dataclass(frozen=True)
class GateTick:
    """One provider call's outcome, published as the ephemeral `gate.tick` event.

    `decision` is the action that fired (or `"none"`), `p` the best line's
    P(not none), `considered_lines` how many new lines were judged. `error` is
    set (and `decision` is `"none"`) when the provider call failed.
    """

    provider: str
    latency_ms: int
    considered_lines: int
    decision: str
    p: float
    anchor_seq: int | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, object]:
        out: dict[str, object] = {
            "provider": self.provider,
            "latency_ms": int(self.latency_ms),
            "considered_lines": int(self.considered_lines),
            "decision": self.decision,
            "p": round(float(self.p), 3),
            "anchor_seq": self.anchor_seq,
        }
        if self.error:
            out["error"] = self.error
        return out


@dataclass(frozen=True)
class GateSessionContext:
    """Read-only snapshot handed to `LiveGate.observe`.

    `segments` holds the most recent finalized segments (oldest first, at
    most `LIVE_GATE_CONTEXT_SEGMENTS`); `new_from_seq` is the first seq the
    gate has not been shown before (segments with `seq >= new_from_seq` are
    new since the previous observe call). `on_trigger` publishes a trigger.
    """

    session_id: UUID
    meeting_id: UUID | None
    title: str
    attendees: tuple[str, ...]
    segments: tuple[GateSegment, ...]
    new_from_seq: int
    interim_text: str
    last_seq: int
    audio_seconds: float
    on_trigger: Callable[[GateTrigger], Awaitable[None]]
    owner_user_id: UUID | None = None  # the session's starter
    gate_provider: str | None = None  # the session's chosen provider ("jev" | "openai")
    on_tick: Callable[[GateTick], Awaitable[None]] | None = None


@runtime_checkable
class LiveGate(Protocol):
    async def observe(self, ctx: GateSessionContext) -> Sequence[GateTrigger] | None:
        """Inspect the live context; publish via `ctx.on_trigger` or return triggers."""
        ...


def load_gate(module: str = "app.live.jev_gate") -> LiveGate | None:
    """Load the offline gate by default, or an explicitly selected API gate."""
    if module == "app.live.jev_gate":
        from app.live.mock_provider import ProviderGate

        return ProviderGate()
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name and module.startswith(exc.name):
            return None
        log.warning("live.gate_import_failed", module=module, error=str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 — the gate is optional
        log.warning("live.gate_import_failed", module=module, error=str(exc))
        return None
    factory = getattr(mod, "create_gate", None)
    if factory is None:
        log.warning("live.gate_missing_factory", module=module)
        return None
    try:
        gate = factory()
    except Exception as exc:  # noqa: BLE001
        log.warning("live.gate_create_failed", module=module, error=str(exc))
        return None
    if gate is None:
        return None
    if not hasattr(gate, "observe"):
        log.warning("live.gate_invalid", module=module)
        return None
    return gate
