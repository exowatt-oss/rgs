"""Decision gate (Jev / OpenAI Decisions): a fast, always-on trigger classifier for Live Record.

The gate watches a live meeting's final transcript segments and decides, a
few times a minute, whether something just said should wake an AI agent and
with which action:

    none | lookup_past | fact_check | find_owner | recall_action_items | define_term | look_up

Each tick sends TypeSafe's Jev (System One) ONE request: a shared state
(meeting title, earlier lines as context, candidate NEW lines tagged `L<seq>`)
and one typed `choice` question per candidate line, keyed `L<seq>`, whose
options are the six actions (`none` first: Jev is sensitive to option order
and `none` is the common case). Per-line questions keep each action attached
to the line it fires on, so two requests in one ASR batch are both judged.

A line fires when P(not none) = 1 - p(none) >= threshold (requests that
straddle two actions split their mass, so the chosen option's own probability
under-fires); the action is the most likely non-`none` option. Jev cannot
generate text, so the one-line `query` is the line's question sentence, or a
per-action template around a statement.

Firing rules (per session): threshold, a fire watermark (a line fires at most
once, and never after a later line fired), keyword-Jaccard topic dedupe,
fewer than `max_in_flight` unresolved triggers, and a short post-fire
debounce. Lines spoken while the gate skipped (debounce, in-flight cap, Jev
error) are carried into the next call for up to `carry_window_s`. Jev errors
and timeouts skip the tick and are counted; nothing here raises into the
caller's session loop.

Providers: the request/answer shape above is TypeSafe Jev's; the OpenAI
Decisions API provider (`app.live.openai_decisions.OpenAIDecisionsClient`)
answers the same questions through `POST /v1/decisions`, so everything below
(windowing, firing, dedupe, debounce, carry, back-off) is provider-agnostic.
One gate holds a client per configured provider; each session picks one
(`ctx.gate_provider`, else `LIVE_GATE_PROVIDER`) and can switch mid-meeting
without losing its watermark/dedupe state. Thresholds, timeouts and back-off
are per provider. After every provider call the gate awaits `ctx.on_tick`
with a `GateTick` (the UI's "listening" heartbeat).

Integration surface (see README provider configuration):

* `create_gate(on_trigger=...)` -> `JevGate | None` (None when disabled or
  keyless); `app.live.gate.load_gate()` calls it with no arguments.
* `await gate.observe(ctx)` / `gate.submit(ctx)` from the session loop,
  ~every 5 s and on each `segment.final`. `ctx` may be the live seam's
  `GateSessionContext` (triggers then go to `ctx.on_trigger` and are returned
  as `app.live.gate.GateTrigger`) or a `JevContext` / mapping.
* `gate.mark_resolved(trigger_id)` when a finding answers a trigger.
* `gate.end_session(session_id)` when the session stops; `await gate.aclose()`
  at shutdown.
* Optional `FallbackResponder`: if no agent claims a trigger within
  `claim_wait_s`, search past meetings and post a finding. Claims go through
  the `TriggerClaims` protocol so exactly one claimer wins.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import anyio.to_thread
import httpx
import structlog

from app.live.jev_settings import (
    DEFAULT_JEV_ENDPOINT,
    DEFAULT_JEV_MODEL,
    PROVIDER_JEV,
    PROVIDER_OPENAI,
    JevGateSettings,
    get_jev_settings,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

ACTION_NONE = "none"
ACTIONS: tuple[str, ...] = (
    ACTION_NONE,
    "lookup_past",
    "fact_check",
    "find_owner",
    "recall_action_items",
    "define_term",
    "look_up",
)
FIRING_ACTIONS: frozenset[str] = frozenset(ACTIONS[1:])

# Option descriptions Jev scores against. Jev reads literally, so each one
# names the concrete phrasing it should match and what it must NOT match.
ACTION_CRITERIA: dict[str, str] = {
    ACTION_NONE: (
        "The line needs no lookup: small talk, logistics (screen sharing, audio, "
        "breaks, scheduling, food), status updates, opinions, a question about the "
        "current plan or preference that the people present can answer ('are we still "
        "doing tacos?', 'should we go with A?', 'what do you think?'), a clarifying "
        "question about what someone just said ('is that the quote from A or B?'), a "
        "question answered by the very next line, someone describing or demonstrating what "
        "the assistant / this tool can do or listens for ('you can ask it to define a term or "
        "look something up', 'here it shows what it's listening for'), a request to note or "
        "add an action "
        "item or to-do for later ('can we add an action item to...'), or a REPLY to an "
        "earlier question, "
        "including replies that do not know the answer ('I don't remember', 'I'd have to "
        "check', 'not sure, maybe someone in ops', 'I thought that was just a proposal'): "
        "the earlier question was the request, not the reply."
    ),
    "lookup_past": (
        "The line explicitly refers back to an earlier meeting, call or discussion "
        "and asks what was said, decided, chosen or agreed there (e.g. 'what did we "
        "decide last time about X', 'remind me where we landed on X', 'did we ever "
        "decide on X', 'what did the vendor say when we met last month')."
    ),
    "fact_check": (
        "The line states a specific number, amount, price, date or fact with doubt, or "
        "asks to confirm or correct one (e.g. 'I think the demo took 4 minutes, right?', "
        "'wasn't the deadline the 15th?', 'are we sure there are 2 examples?')."
    ),
    "find_owner": (
        "The line asks WHO owns, is responsible for, is handling, is the point of "
        "contact for, or should be asked about a task, area, document or decision."
    ),
    "recall_action_items": (
        "The line asks about action items, follow-ups, to-dos, open tasks or what "
        "people committed to do (e.g. 'what were our action items from last week?')."
    ),
    "define_term": (
        "The line asks what a term, acronym, product name, code name or piece of "
        "jargon means or stands for (e.g. 'what's an API?', 'what does JSON mean?')."
    ),
    "look_up": (
        "The line asks to look up, check, verify or find out a specific fact from the "
        "company's records, people directory, code or tools - who made, built, wrote or "
        "sent something, when something last happened, whether something was done - "
        "e.g. 'can you look up who made the demo?', 'can we check that the last time I "
        "updated the sample was last week?', 'check when the tutorial shipped', 'who is "
        "Alice's reviewer?'. Includes requests phrased casually or split across lines. "
        "NOT a task for a person present to do later."
    ),
}

# One `choice` question per candidate line (all in one request), so the action
# always belongs to the line it fires on and two requests in one ASR batch are
# both judged. `{line}` is filled with the line id and text.
LINE_INSTRUCTIONS = (
    "Live work meeting. Judge ONLY {line} (other lines are context; speech recognition "
    "may split one sentence across lines, so read the line together with the same "
    "speaker's previous line). Does this line ask for something an assistant with the "
    "team's past meetings, decisions, action items, people directory and company records "
    "should look up or check right now? Pick the best match, or none."
)

QUERY_TEMPLATES: dict[str, str] = {
    "lookup_past": "What was said or decided before about: {topic}",
    "fact_check": "Check against past meetings: {topic}",
    "find_owner": "Who owns or handles: {topic}",
    "recall_action_items": "Related action items for: {topic}",
    "define_term": "What this term means: {topic}",
    "look_up": "Look up: {topic}",
}

_FILLER_RE = re.compile(
    r"^(?:(?:so|um+|uh+|er+|erm|okay|ok|yeah|yep|yes|well|like|right|hey|and|but|"
    r"actually|anyway|alright|hmm+|oh|look|listen|guys|folks|sorry|wait|quick question)"
    r"\b[,.!:;]?\s*)+",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9$%.'\-]*")
_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing done down during
    each few for from further get got had has have having he her here hers him his how
    i if in into is it its itself just know let like me more most my no nor not now of
    off on once only or other our ours out over own really right same she should so
    some such sure than that the their them then there these they thing think this
    those through to too under until up us very was we were what when where which
    while who whom why will with would yeah you your yours actually again anyone
    remind tell said say says mean means meant guys folks okay ok um uh last time
    something somebody someone exactly again still going gonna wanna kind sort maybe
    we'd we're we've i'm it's that's what's who's there's don't didn't wasn't isn't
    """.split()
)


# ---------------------------------------------------------------------------
# Input shapes (adapters tolerate the live-backend's own segment objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JevSegment:
    """One transcript segment as the gate sees it."""

    seq: int
    text: str
    speaker: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    is_final: bool = True


@dataclass(slots=True)
class JevContext:
    """What the session loop hands the gate on each tick.

    `segments` should cover at least the last `context_window_s` seconds of
    final segments (more is fine; the gate windows them itself).
    `recent_findings` are short titles/summaries already surfaced in this
    meeting, shown to Jev as "already surfaced" so it does not re-ask.
    `owner_user_id` is only needed by the fallback responder (it searches
    as that user, through the normal read ACL).
    """

    session_id: str
    segments: Sequence[JevSegment]
    meeting_title: str | None = None
    recent_findings: Sequence[str] = ()
    owner_user_id: Any = None
    # Per-session publisher (the live seam's `GateSessionContext.on_trigger`).
    on_trigger: Callable[[Any], Awaitable[Any]] | None = None
    # The session's provider ("jev" | "openai"); None = the gate's default.
    gate_provider: str | None = None
    # Per-call heartbeat (the live seam's `GateSessionContext.on_tick`).
    on_tick: Callable[[Any], Awaitable[Any]] | None = None


def _pick(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, Mapping):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _speaker_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    name = _pick(value, "display_name", "label", "name", "speaker_label")
    return str(name) if name is not None else str(value)


def coerce_segment(obj: Any) -> JevSegment:
    """Accept a `JevSegment`, a mapping, or any object with segment attributes."""
    if isinstance(obj, JevSegment):
        return obj
    seq = _pick(obj, "seq", "sequence", "segment_seq")
    if seq is None:
        raise ValueError("segment has no seq")
    text = _pick(obj, "text", "content") or ""
    start = _pick(obj, "start_s", "start_seconds", "start")
    end = _pick(obj, "end_s", "end_seconds", "end")
    if start is None and (start_ms := _pick(obj, "start_ms")) is not None:
        start = float(start_ms) / 1000.0
    if end is None and (end_ms := _pick(obj, "end_ms")) is not None:
        end = float(end_ms) / 1000.0
    final = _pick(obj, "is_final", "final")
    if final is None:
        kind = _pick(obj, "type", "kind", "status")
        final = kind is None or str(kind).endswith("final")
    return JevSegment(
        seq=int(seq),
        text=str(text),
        # Prefer the display name ("Speaker 2" / a confirmed name) over a raw label ("S2").
        speaker=_speaker_name(_pick(obj, "speaker_name", "speaker", "speaker_label")),
        start_s=float(start) if start is not None else None,
        end_s=float(end) if end is not None else None,
        is_final=bool(final),
    )


def _finding_text(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    text = _pick(item, "title", "summary", "text", "body", "query")
    return str(text) if text is not None else None


def coerce_context(obj: Any) -> JevContext:
    """Accept a `JevContext`, a mapping, or any object with context attributes."""
    if isinstance(obj, JevContext):
        return obj
    session_id = _pick(obj, "session_id", "id")
    if session_id is None:
        raise ValueError("gate context has no session_id")
    raw_segments = _pick(obj, "segments", "recent_segments") or ()
    raw_findings = _pick(obj, "recent_findings", "findings", "recent_decisions") or ()
    findings = [t for t in (_finding_text(f) for f in raw_findings) if t]
    return JevContext(
        session_id=str(session_id),
        segments=[coerce_segment(s) for s in raw_segments],
        meeting_title=_pick(obj, "meeting_title", "title"),
        recent_findings=findings,
        owner_user_id=_pick(obj, "owner_user_id", "user_id"),
        on_trigger=_pick(obj, "on_trigger"),
        gate_provider=_pick(obj, "gate_provider", "provider"),
        on_tick=_pick(obj, "on_tick"),
    )


def to_live_trigger(trigger: JevTrigger) -> Any:
    """Convert to the live seam's `app.live.gate.GateTrigger` by field intersection.

    Fields the seam does not declare yet (context_text, decided_at, topic,
    anchor_seq, ...) are dropped; they flow through automatically once the seam
    adds them. Falls back to the native trigger if the seam is unavailable.
    """
    try:
        from app.live.gate import GateTrigger as LiveGateTrigger

        names = {f.name for f in dataclasses.fields(LiveGateTrigger)}
        return LiveGateTrigger(**{n: getattr(trigger, n) for n in names if hasattr(trigger, n)})
    except (ImportError, TypeError):
        return trigger


# ---------------------------------------------------------------------------
# Output shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JevTrigger:
    """A fired gate decision: the payload of the `gate.trigger` event."""

    trigger_id: str
    session_id: str
    action: str
    query: str
    topic: str
    seq_from: int
    seq_to: int
    anchor_seq: int
    context_text: str
    probability: float
    decided_at: datetime
    jev_latency_ms: int
    model: str | None = None
    provider: str = PROVIDER_JEV

    def to_event_payload(self) -> dict[str, Any]:
        """JSON-safe dict for `live_events` / `live_triggers` (no session_id: the row has it)."""
        return {
            "trigger_id": self.trigger_id,
            "action": self.action,
            "query": self.query,
            "topic": self.topic,
            "seq_from": self.seq_from,
            "seq_to": self.seq_to,
            "anchor_seq": self.anchor_seq,
            "context_text": self.context_text,
            "probability": round(self.probability, 4),
            "decided_at": self.decided_at.isoformat(),
            "jev_latency_ms": self.jev_latency_ms,
            "model": self.model,
            "provider": self.provider,
        }


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float | None = None

    @property
    def probability(self) -> float:
        if self.choice in self.probabilities:
            return float(self.probabilities[self.choice])
        return float(self.confidence or 0.0)


@dataclass(frozen=True, slots=True)
class JevResult:
    answers: Mapping[str, ChoiceAnswer]
    model: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: float


class JevError(Exception):
    """A failed provider call. The message never contains the key or the transcript.

    `code` is the provider's machine-readable error code when it sent one
    (e.g. OpenAI's `billing_not_active`); `fatal` marks failures that will not
    fix themselves within seconds (auth, billing) so the gate backs off at once.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        code: str | None = None,
        fatal: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.code = code
        self.fatal = fatal or status in (401, 403)


class JevTimeout(JevError):
    pass


def _percentile(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))  # nearest-rank
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class GateMetrics:
    """Process-local counters. Exported via `snapshot()` (and logged on aclose)."""

    ticks: int = 0
    jev_calls: int = 0
    jev_errors: int = 0
    jev_timeouts: int = 0
    skipped_no_new_text: int = 0
    skipped_min_interval: int = 0
    skipped_busy: int = 0
    skipped_debounce: int = 0
    skipped_in_flight: int = 0
    skipped_backoff: int = 0
    decided_none: int = 0
    below_threshold: int = 0
    empty_topic: int = 0
    deduped: int = 0
    held_unfinished: int = 0
    fired: int = 0
    callback_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=2000))

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            k: v for k, v in self.__dict__.items() if k != "latencies_ms" and isinstance(v, int)
        }
        lat = list(self.latencies_ms)
        out["latency_p50_ms"] = _percentile(lat, 50)
        out["latency_p95_ms"] = _percentile(lat, 95)
        return out


# ---------------------------------------------------------------------------
# Jev transport
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


class JevClient:
    """Minimal async System One client: one keep-alive pool, hard per-call timeout.

    Keep one instance per process; a warm connection is ~3x faster than a cold
    TLS handshake (~170 ms vs ~460 ms p50 for a 1k-token payload).
    """

    provider = PROVIDER_JEV

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_JEV_ENDPOINT,
        model: str = DEFAULT_JEV_MODEL,
        timeout_s: float = 2.0,
        max_connections: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Jev API key is empty")
        self._endpoint = endpoint
        self._model = model
        self._timeout_s = timeout_s
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "exo-ani-jev-gate/1",
            },
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
                keepalive_expiry=90.0,
            ),
            transport=transport,
        )

    @property
    def model(self) -> str:
        return self._model

    async def ask(
        self,
        state: str | Mapping[str, Any] | Sequence[Any],
        questions: Mapping[str, Mapping[str, Any]],
        *,
        timeout_s: float | None = None,
    ) -> JevResult:
        budget = timeout_s if timeout_s is not None else self._timeout_s
        payload = {"model": self._model, "state": state, "questions": questions}
        started = time.perf_counter()
        try:
            async with asyncio.timeout(budget):
                resp = await self._client.post(self._endpoint, json=payload)
        except (TimeoutError, httpx.TimeoutException):
            raise JevTimeout(f"Jev call exceeded {budget:.2f}s", retryable=True) from None
        except httpx.HTTPError as exc:
            raise JevError(f"Jev transport error: {type(exc).__name__}", retryable=True) from None
        latency_ms = (time.perf_counter() - started) * 1000.0
        if resp.status_code != 200:
            raise JevError(
                f"Jev HTTP {resp.status_code}",
                status=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_STATUS,
            )
        try:
            body = resp.json()
        except ValueError:
            raise JevError("Jev returned a non-JSON body", status=resp.status_code) from None
        if not isinstance(body, Mapping):
            raise JevError("Jev returned an unexpected body", status=resp.status_code)
        answers: dict[str, ChoiceAnswer] = {}
        for name, raw in (body.get("answers") or {}).items():
            if not isinstance(raw, Mapping):
                continue
            probs = {
                str(k): float(v)
                for k, v in (raw.get("probabilities") or {}).items()
                if isinstance(v, int | float)
            }
            choice = raw.get("choice")
            if choice is None and probs:
                choice = max(probs, key=lambda k: probs[k])
            confidence = raw.get("confidence")
            answers[str(name)] = ChoiceAnswer(
                choice=str(choice) if choice is not None else "",
                probabilities=probs,
                confidence=float(confidence) if isinstance(confidence, int | float) else None,
            )
        usage = body.get("usage") or {}
        return JevResult(
            answers=answers,
            model=body.get("model"),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def clean_utterance(text: str, *, max_chars: int = 200) -> str:
    """Collapse whitespace, drop leading fillers, cap length (word boundary)."""
    out = " ".join(text.split())
    out = _FILLER_RE.sub("", out).strip()
    if out:
        out = out[0].upper() + out[1:]
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return out


_SENTENCE_RE = re.compile(r"[^.?!]+(?:[.?!]+|$)")


def focus_text(text: str, *, max_chars: int = 200) -> str:
    """The part of an utterance that carries the request.

    Question sentences win ("Sorry, dumb question. What does BOP stand for? I
    keep seeing it." -> "What does BOP stand for?"); consecutive questions are
    kept together while they fit. Statements fall back to the whole cleaned
    utterance.
    """
    sentences = [s.strip() for s in _SENTENCE_RE.findall(" ".join(text.split())) if s.strip()]
    questions = [s for s in sentences if s.endswith("?")]
    if not questions:
        return clean_utterance(text, max_chars=max_chars)
    picked = [questions[0]]
    for q in questions[1:]:
        if len(" ".join([*picked, q])) > max_chars:
            break
        picked.append(q)
    return clean_utterance(" ".join(picked), max_chars=max_chars)


def fire_decision(answer: ChoiceAnswer) -> tuple[str | None, float]:
    """(action, fire probability) from the `action` answer.

    The fire probability is P(not none) = 1 - p(none), and the action is the
    most likely non-`none` option. Using the chosen option's own probability
    under-fires on requests that legitimately straddle two actions ("who
    signed off on X last time?" splits between find_owner and lookup_past).
    Without a probability table, fall back to the chosen option.
    """
    probs = {k: v for k, v in answer.probabilities.items() if k in ACTIONS}
    if not probs:
        if answer.choice in FIRING_ACTIONS:
            return answer.choice, answer.probability
        return None, 0.0
    firing = {k: v for k, v in probs.items() if k in FIRING_ACTIONS}
    if not firing:
        return None, 0.0
    action = max(firing, key=lambda k: firing[k])
    p_fire = 1.0 - probs[ACTION_NONE] if ACTION_NONE in probs else sum(firing.values())
    if firing[action] <= 0.0:
        return None, 0.0
    return action, max(0.0, min(1.0, p_fire))


def build_query(action: str, topic: str) -> str:
    """One-line query for the agent: the question itself, or a templated statement."""
    if topic.endswith("?"):
        return topic
    return QUERY_TEMPLATES[action].format(topic=topic.rstrip(".…"))


# Words that carry no topic of their own in a follow-up ("what does the term stand for?").
_REPHRASE_GENERIC = frozenset(
    "term terms stand stands meaning acronym word abbreviation short definition define "
    "again exactly name called mean means".split()
)


def is_rephrase(
    state: _SessionState,
    seg: JevSegment,
    action: str,
    keywords: frozenset[str],
    *,
    window_s: float,
) -> bool:
    """Same speaker, same action, shortly after a fire, adding no new topic words.

    "What is LCOE? Like, is that cost of energy?" fires; "What what is the term
    stand for?" right after is the same request asked again, not a new one.
    """
    if window_s <= 0:
        return False
    content = keywords - _REPHRASE_GENERIC
    for end_s, speaker, fired_action, fired_keywords in state.recent_fires:
        if fired_action != action or speaker != seg.speaker:
            continue
        if end_s is not None and seg.end_s is not None and seg.end_s - end_s > window_s:
            continue
        if content <= fired_keywords:
            return True
    return False


def topic_keywords(text: str) -> frozenset[str]:
    """Content words used for dedupe and for the fallback search query."""
    words = (w.strip(".'-") for w in _WORD_RE.findall(text.lower()))
    return frozenset(
        w
        for w in words
        if w and w not in _STOPWORDS and (len(w) >= 3 or any(c.isdigit() for c in w))
    )


def keyword_query(text: str, *, max_words: int = 8) -> str:
    """Order-preserving keyword string (plainto_tsquery ANDs every term, so keep it short)."""
    seen: list[str] = []
    keep = topic_keywords(text)
    for w in (w.strip(".'-") for w in _WORD_RE.findall(text.lower())):
        if w in keep and w not in seen:
            seen.append(w)
    return " ".join(seen[:max_words])


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _line(seg: JevSegment, *, max_chars: int = 400) -> str:
    who = seg.speaker or "Speaker"
    text = " ".join(seg.text.split())
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return f"{who}: {text}"


def as_sentence_lines(finals: Sequence[JevSegment]) -> list[JevSegment]:
    """ASR fragments -> one line per spoken sentence (see `utterances.sentence_lines`).

    A question cut over two fragments ("Can you look up who made" / "the demo,
    J-E-Z?") is judged as one line, and two requests in one fragment run are
    judged separately. Line seqs stay real segment seqs.
    """
    from app.live.utterances import sentence_lines

    timed = [f for f in finals if f.start_s is not None and f.end_s is not None]
    if len(timed) != len(finals) or not finals:
        return list(finals)
    frags = [(f.seq, f.speaker or "Speaker", f.start_s, f.end_s, f.text) for f in timed]
    lines = sentence_lines(frags)
    out = [
        JevSegment(
            seq=u.seq, text=u.text, speaker=u.speaker, start_s=u.start, end_s=u.end, is_final=True
        )
        for u in lines
    ]
    return sorted(out, key=lambda x: x.seq)


def select_window(
    segments: Sequence[JevSegment],
    *,
    watermark: int,
    settings: JevGateSettings,
    judged_through: int = -1,
) -> tuple[list[JevSegment], list[JevSegment]]:
    """Split final segments into (earlier context, candidate NEW lines).

    Candidates are lines after the fire watermark that are either recent
    (`candidate_window_s`) or were never judged (`seq > judged_through`, e.g.
    they arrived while the gate was debouncing or at its in-flight cap) and are
    not older than `carry_window_s`. Windows are measured on segment end times
    relative to the newest segment (not wall clock), so ASR lag does not shrink
    them. Segments without timing fall back to count limits.
    """
    finals = sorted((s for s in segments if s.is_final and s.text.strip()), key=lambda s: s.seq)
    if not finals:
        return [], []
    ends = [s.end_s for s in finals if s.end_s is not None]
    latest_end = max(ends) if ends else None

    def within(seg: JevSegment, window_s: float) -> bool:
        if latest_end is None or seg.end_s is None:
            return True
        return seg.end_s >= latest_end - window_s

    candidates = [
        s
        for s in finals
        if s.seq > watermark
        and (
            within(s, settings.candidate_window_s)
            or (s.seq > judged_through and within(s, settings.carry_window_s))
        )
    ]
    candidates = candidates[-settings.max_candidates :]
    first_candidate = candidates[0].seq if candidates else finals[-1].seq + 1
    earlier = [
        s for s in finals if s.seq < first_candidate and within(s, settings.context_window_s)
    ]
    if latest_end is None:
        earlier = earlier[-12:]
    budget = settings.max_context_chars - sum(len(_line(s)) + 6 for s in candidates)
    while earlier and sum(len(_line(s)) + 1 for s in earlier) > max(budget, 0):
        earlier.pop(0)
    return earlier, candidates


def build_state(
    *,
    meeting_title: str | None,
    earlier: Sequence[JevSegment],
    candidates: Sequence[JevSegment],
    surfaced: Sequence[str] = (),
) -> str:
    parts = [f"MEETING: {meeting_title.strip() if meeting_title else '(untitled)'}"]
    if surfaced:
        parts.append("ALREADY LOOKED UP IN THIS MEETING (do not ask again):")
        parts.extend(f"- {' '.join(s.split())[:160]}" for s in surfaced[-6:])
    parts.append("EARLIER LINES (context only):")
    parts.extend(_line(s) for s in earlier)
    if not earlier:
        parts.append("(none)")
    parts.append("NEW LINES:")
    parts.extend(f"L{s.seq} {_line(s)}" for s in candidates)
    return "\n".join(parts)


def line_id(seg: JevSegment) -> str:
    return f"L{seg.seq}"


def build_questions(candidates: Sequence[JevSegment]) -> dict[str, dict[str, Any]]:
    """One action question per candidate line, keyed by its line id."""
    return {
        line_id(seg): {
            "type": "choice",
            "instructions": LINE_INSTRUCTIONS.format(
                line=f"{line_id(seg)} ({_line(seg, max_chars=240)})"
            ),
            "criteria": dict(ACTION_CRITERIA),
        }
        for seg in candidates
    }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

OnTrigger = Callable[[JevTrigger], Awaitable[None]]


class JevAsker(Protocol):
    """What the gate needs from a provider client (lets tests/bench inject one).

    Implemented by `JevClient` and `OpenAIDecisionsClient`. A client may expose
    a `provider` attribute (`"jev"` | `"openai"`); without one it is Jev.
    """

    @property
    def model(self) -> str: ...

    async def ask(
        self,
        state: str | Mapping[str, Any] | Sequence[Any],
        questions: Mapping[str, Mapping[str, Any]],
        *,
        timeout_s: float | None = None,
    ) -> JevResult: ...

    async def aclose(self) -> None: ...


def provider_of(client: Any) -> str:
    return str(getattr(client, "provider", None) or PROVIDER_JEV)


@dataclass
class _ProviderState:
    """Per-provider transport health: back-off is per provider, not per gate."""

    provider: str
    client: JevAsker
    consecutive_failures: int = 0
    backoff_until: float = 0.0
    last_error: str | None = None
    calls: int = 0
    errors: int = 0
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=500))


@dataclass
class _SessionState:
    watermark: int = -1  # lines at/below this seq never fire (again)
    judged_through: int = -1  # every line up to this seq has been judged by a provider
    last_call_at: float | None = None
    last_fire_at: float | None = None
    busy: bool = False
    pending: JevContext | None = None
    recent_topics: deque[tuple[float, frozenset[str], str]] = field(
        default_factory=lambda: deque(maxlen=32)
    )
    in_flight: dict[str, float] = field(default_factory=dict)
    # (transcript end_s, speaker, action, content keywords) of recent fires.
    recent_fires: deque[tuple[float | None, str | None, str, frozenset[str]]] = field(
        default_factory=lambda: deque(maxlen=16)
    )
    first_seen: dict[int, float] = field(default_factory=dict)  # seq -> clock, unfinished lines

    def prune(self, now: float, *, ttl_s: float, dedupe_window_s: float) -> None:
        for tid, fired_at in list(self.in_flight.items()):
            if now - fired_at >= ttl_s:
                del self.in_flight[tid]
        while self.recent_topics and now - self.recent_topics[0][0] >= dedupe_window_s:
            self.recent_topics.popleft()


# Self-introduction confirmation (one extra question, either provider).
INTRO_QUESTION = (
    "Live work meeting. In {line}, is the speaker introducing THEMSELVES by name, "
    "saying their own name is {name}? Answer no when {name} is someone else being "
    "mentioned, greeted or addressed, or when the words are not a name."
)
INTRO_CRITERIA = {
    "no": "Not a self-introduction with this name.",
    "yes": "The speaker says their own name is the given name.",
}


class JevGate:
    """Per-process gate serving every live session, with one client per provider.

    Calls for one session are serialized: a tick that arrives while that
    session's provider call is in flight is coalesced (the newest context is
    kept and evaluated as soon as the current call returns). Different
    sessions run concurrently, bounded by `max_concurrent_calls`.
    """

    def __init__(
        self,
        settings: JevGateSettings,
        *,
        client: JevAsker | None = None,
        clients: Mapping[str, JevAsker] | None = None,
        default_provider: str | None = None,
        on_trigger: OnTrigger | None = None,
        clock: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        providers: dict[str, JevAsker] = dict(clients or {})
        if client is not None:
            providers.setdefault(provider_of(client), client)
        if not providers:
            raise ValueError("the gate needs at least one provider client")
        self.settings = settings
        self.metrics = GateMetrics()
        self._providers = {pid: _ProviderState(pid, c) for pid, c in providers.items()}
        wanted = default_provider or settings.default_provider
        self.default_provider = wanted if wanted in self._providers else next(iter(providers))
        self._on_trigger = on_trigger
        self._clock = clock
        self._utcnow = utcnow
        self._sessions: dict[str, _SessionState] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrent_calls)
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- public API ---------------------------------------------------------

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def resolve_provider(self, requested: str | None) -> str:
        """The provider a session asking for `requested` gets (default when unusable)."""
        if requested and requested in self._providers:
            return requested
        return self.default_provider

    def provider_status(self) -> dict[str, dict[str, Any]]:
        """Health per loaded provider, for `GET /api/live/config`."""
        now = self._clock()
        out: dict[str, dict[str, Any]] = {}
        for pid, prov in self._providers.items():
            lat = list(prov.latencies_ms)
            out[pid] = {
                "healthy": now >= prov.backoff_until and prov.consecutive_failures < 3,
                "last_error": prov.last_error,
                "latency_p50_ms": _percentile(lat, 50),
                "model": prov.client.model,
            }
        return out

    async def observe(self, ctx: JevContext | Mapping[str, Any] | Any) -> list[Any]:
        """Run one gate tick for a session. Never raises for provider/callback failures.

        Returns the triggers fired by this call (0 or 1, occasionally more when a
        coalesced tick fires after the debounce window). When the context carries
        its own `on_trigger` (the live seam's `GateSessionContext`), triggers are
        handed to it and returned as `app.live.gate.GateTrigger` instances;
        otherwise native `JevTrigger`s are returned.
        """
        context = coerce_context(ctx)
        state = self._sessions.setdefault(context.session_id, _SessionState())
        self.metrics.ticks += 1
        if state.busy:
            state.pending = context
            self.metrics.skipped_busy += 1
            return []
        state.busy = True
        fired: list[JevTrigger] = []
        try:
            current: JevContext | None = context
            while current is not None:
                fired.extend(await self._tick(state, current))
                current, state.pending = state.pending, None
        finally:
            state.busy = False
        if context.on_trigger is not None:
            return [to_live_trigger(t) for t in fired]
        return list(fired)

    def submit(self, ctx: JevContext | Mapping[str, Any] | Any) -> asyncio.Task[list[Any]]:
        """Fire-and-forget `observe` for session loops that must not await the gate."""
        task = asyncio.get_running_loop().create_task(self.observe(ctx))
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def mark_resolved(self, trigger_id: str, *, session_id: str | None = None) -> None:
        """A finding answered `trigger_id` (or it was dismissed): free its in-flight slot."""
        states = (
            [self._sessions[session_id]]
            if session_id is not None and session_id in self._sessions
            else list(self._sessions.values())
        )
        for state in states:
            if state.in_flight.pop(trigger_id, None) is not None:
                return

    def note_surfaced(self, session_id: str, text: str) -> None:
        """Count an externally surfaced topic (e.g. an agent's own finding) for dedupe."""
        state = self._sessions.setdefault(session_id, _SessionState())
        keywords = topic_keywords(text)
        if keywords:
            state.recent_topics.append((self._clock(), keywords, " ".join(text.split())[:160]))

    def end_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def confirm_intro(
        self,
        *,
        provider: str | None,
        line: str,
        name: str,
        context: Sequence[str] = (),
        meeting_title: str | None = None,
    ) -> tuple[float, str, int] | None:
        """P(the speaker of `line` is introducing themselves as `name`).

        One `choice` question (yes/no) on the session's provider. Returns
        `(p_yes, provider, latency_ms)` or None when the provider is backing off
        or the call fails (the caller then keeps the speaker's label).
        """
        pid = self.resolve_provider(provider)
        prov = self._providers[pid]
        if self._clock() < prov.backoff_until:
            return None
        parts = [f"MEETING: {meeting_title.strip() if meeting_title else '(untitled)'}"]
        if context:
            parts.append("EARLIER LINES (context only):")
            parts.extend(" ".join(c.split())[:300] for c in context[-4:])
        parts.append("LINE:")
        parts.append(" ".join(line.split())[:400])
        question = {
            "intro": {
                "type": "choice",
                "instructions": INTRO_QUESTION.format(line="LINE", name=json_quote(name)),
                "criteria": dict(INTRO_CRITERIA),
            }
        }
        try:
            async with self._sem:
                result = await prov.client.ask(
                    "\n".join(parts), question, timeout_s=self.settings.timeout_for(pid)
                )
        except JevError as exc:
            self._note_failure(prov, exc)
            log.info("jev_gate.intro_confirm_failed", provider=pid, status=exc.status)
            return None
        self._note_success(prov, result.latency_ms)
        answer = result.answers.get("intro")
        if answer is None:
            return None
        probs = answer.probabilities
        if "yes" in probs:
            p_yes = float(probs["yes"])
        elif "no" in probs:
            p_yes = 1.0 - float(probs["no"])
        else:
            p_yes = answer.probability if answer.choice == "yes" else 0.0
        return max(0.0, min(1.0, p_yes)), pid, int(round(result.latency_ms))

    async def warmup(self) -> bool:
        """Open each provider's keep-alive connection before the first real tick. Best effort."""
        ok = True
        for prov in self._providers.values():
            try:
                await prov.client.ask(
                    "MEETING: warmup\nNEW LINES:\nL0 Speaker: hello",
                    {"ok": {"type": "choice", "criteria": {"yes": "greeting", "no": "other"}}},
                )
            except JevError as exc:
                ok = False
                prov.last_error = _error_label(exc)
                log.warning(
                    "jev_gate.warmup_failed",
                    provider=prov.provider,
                    error=str(exc),
                    status=exc.status,
                )
        return ok

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        log.info("jev_gate.closed", **self.metrics.snapshot())
        for prov in self._providers.values():
            await prov.client.aclose()

    # -- internals ----------------------------------------------------------

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("jev_gate.task_failed", error_type=type(task.exception()).__name__)

    def _hold_unfinished(
        self, state: _SessionState, finals: list[JevSegment], now: float
    ) -> list[JevSegment]:
        """Hold back a newest line that is still mid-sentence, for `unfinished_hold_s`.

        "Can we check that the last time I updated" arrives before "the sample
        was last week?"; judged alone it fires on half a request and the full
        one fires again. A held line is judged once it ends (or the hold lapses).
        """
        hold = self.settings.unfinished_hold_s
        if hold <= 0 or not finals:
            return finals
        newest = finals[-1]
        if newest.text.rstrip().endswith((".", "?", "!")):
            return finals
        first = state.first_seen.setdefault(newest.seq, now)
        for seq in [q for q in state.first_seen if q < newest.seq - 50]:
            del state.first_seen[seq]
        if now - first < hold:
            self.metrics.held_unfinished += 1
            return finals[:-1]
        return finals

    def _note_failure(self, prov: _ProviderState, exc: JevError) -> None:
        prov.consecutive_failures += 1
        prov.errors += 1
        prov.last_error = _error_label(exc)
        now = self._clock()
        if exc.fatal or exc.status in (401, 403):
            prov.backoff_until = now + 60.0
        elif prov.consecutive_failures >= 3:
            prov.backoff_until = now + min(30.0, 2.0 ** (prov.consecutive_failures - 3))

    def _note_success(self, prov: _ProviderState, latency_ms: float) -> None:
        prov.consecutive_failures = 0
        prov.last_error = None
        prov.calls += 1
        prov.latencies_ms.append(latency_ms)

    async def _tick(self, state: _SessionState, ctx: JevContext) -> list[JevTrigger]:
        s = self.settings
        now = self._clock()
        pid = self.resolve_provider(ctx.gate_provider)
        prov = self._providers[pid]
        finals = sorted(
            (seg for seg in ctx.segments if seg.is_final and seg.text.strip()), key=lambda x: x.seq
        )
        if s.sentence_lines:
            finals = as_sentence_lines(finals)
            finals = self._hold_unfinished(state, finals, now)
        if not finals or finals[-1].seq <= state.judged_through:
            self.metrics.skipped_no_new_text += 1
            return []
        newest = finals[-1]
        if now < prov.backoff_until:
            self.metrics.skipped_backoff += 1
            return []
        if state.last_fire_at is not None and now - state.last_fire_at < s.debounce_s:
            self.metrics.skipped_debounce += 1
            return []
        state.prune(now, ttl_s=s.in_flight_ttl_s, dedupe_window_s=s.dedupe_window_s)
        if len(state.in_flight) >= s.max_in_flight:
            self.metrics.skipped_in_flight += 1
            return []
        is_question = newest.text.rstrip().endswith("?")
        if (
            not is_question
            and state.last_call_at is not None
            and now - state.last_call_at < s.min_interval_s
        ):
            self.metrics.skipped_min_interval += 1
            return []

        earlier, candidates = select_window(
            finals, watermark=state.watermark, settings=s, judged_through=state.judged_through
        )
        if not candidates:
            state.judged_through = newest.seq
            self.metrics.skipped_no_new_text += 1
            return []
        surfaced = [*ctx.recent_findings, *(t[2] for t in state.recent_topics)]
        state_text = build_state(
            meeting_title=ctx.meeting_title,
            earlier=earlier,
            candidates=candidates,
            surfaced=surfaced,
        )
        questions = build_questions(candidates)

        state.last_call_at = now
        self.metrics.jev_calls += 1
        timeout_s = s.timeout_for(pid)
        started = time.perf_counter()
        try:
            async with self._sem:
                result = await prov.client.ask(state_text, questions, timeout_s=timeout_s)
        except JevTimeout as exc:
            self.metrics.jev_timeouts += 1
            self._note_failure(prov, exc)
            log.warning(
                "jev_gate.timeout", session_id=ctx.session_id, provider=pid, timeout_s=timeout_s
            )
            await self._emit_tick(ctx, pid, started, len(candidates), error=_error_label(exc))
            return []
        except JevError as exc:
            self.metrics.jev_errors += 1
            self._note_failure(prov, exc)
            log.warning(
                "jev_gate.jev_error",
                session_id=ctx.session_id,
                provider=pid,
                status=exc.status,
                error=str(exc),
            )
            await self._emit_tick(ctx, pid, started, len(candidates), error=_error_label(exc))
            return []
        self._note_success(prov, result.latency_ms)
        state.judged_through = newest.seq
        self.metrics.latencies_ms.append(result.latency_ms)
        self.metrics.input_tokens += result.input_tokens
        self.metrics.output_tokens += result.output_tokens

        triggers = self._decide(state, ctx, result, earlier, candidates, provider=pid)
        best_seq, best_p = None, 0.0
        for seg in candidates:
            answer = result.answers.get(line_id(seg))
            if answer is None:
                continue
            action, probability = fire_decision(answer)
            if action is not None and probability > best_p:
                best_seq, best_p = seg.seq, probability
        if triggers:
            top = max(triggers, key=lambda t: t.probability)
            decision, best_seq, best_p = top.action, top.anchor_seq, top.probability
        else:
            decision = ACTION_NONE
        await self._emit_tick(
            ctx,
            pid,
            started,
            len(candidates),
            decision=decision,
            p=best_p,
            anchor_seq=best_seq,
            latency_ms=result.latency_ms,
        )
        for trigger in triggers:
            await self._emit(trigger, ctx.on_trigger)
        return triggers

    async def _emit_tick(
        self,
        ctx: JevContext,
        provider: str,
        started: float,
        considered: int,
        *,
        decision: str = ACTION_NONE,
        p: float = 0.0,
        anchor_seq: int | None = None,
        latency_ms: float | None = None,
        error: str | None = None,
    ) -> None:
        if ctx.on_tick is None:
            return
        from app.live.gate import GateTick

        tick = GateTick(
            provider=provider,
            latency_ms=int(round(latency_ms if latency_ms is not None else _elapsed_ms(started))),
            considered_lines=considered,
            decision=decision,
            p=p,
            anchor_seq=anchor_seq,
            error=error,
        )
        try:
            async with asyncio.timeout(self.settings.callback_timeout_s):
                await ctx.on_tick(tick)
        except Exception as exc:  # noqa: BLE001 - heartbeats never break the session loop
            self.metrics.callback_errors += 1
            log.info("jev_gate.tick_callback_failed", error_type=type(exc).__name__)

    def _decide(
        self,
        state: _SessionState,
        ctx: JevContext,
        result: JevResult,
        earlier: Sequence[JevSegment],
        candidates: Sequence[JevSegment],
        *,
        provider: str = PROVIDER_JEV,
    ) -> list[JevTrigger]:
        """Fire on every qualifying line, oldest first, within the in-flight cap."""
        s = self.settings
        threshold = s.threshold_for(provider)
        qualifying: list[tuple[JevSegment, str, float]] = []
        for seg in candidates:
            answer = result.answers.get(line_id(seg))
            if answer is None:
                continue
            action, probability = fire_decision(answer)
            if action is None or probability < 0.5:
                continue
            if probability < threshold:
                self.metrics.below_threshold += 1
                log.debug(
                    "jev_gate.below_threshold",
                    session_id=ctx.session_id,
                    provider=provider,
                    seq=seg.seq,
                    action=action,
                    probability=round(probability, 3),
                )
                continue
            qualifying.append((seg, action, probability))
        if not qualifying:
            self.metrics.decided_none += 1
            log.debug("jev_gate.none", session_id=ctx.session_id, latency_ms=result.latency_ms)
            return []

        window = [*earlier, *candidates]
        context_text = "\n".join(_line(seg) for seg in window)[-s.max_context_chars :]
        fired: list[JevTrigger] = []
        for seg, action, probability in qualifying:
            topic = focus_text(seg.text)
            if len(topic) < 3:
                self.metrics.empty_topic += 1
                continue
            now = self._clock()
            keywords = topic_keywords(topic)
            if is_rephrase(state, seg, action, keywords, window_s=s.rephrase_window_s) or any(
                jaccard(keywords, seen) >= s.dedupe_jaccard for _, seen, _ in state.recent_topics
            ):
                self.metrics.deduped += 1
                state.watermark = max(state.watermark, seg.seq)
                log.info("jev_gate.deduped", session_id=ctx.session_id, action=action, seq=seg.seq)
                continue
            if len(state.in_flight) >= s.max_in_flight:
                # Judge this line again once a slot frees (carry window permitting).
                self.metrics.skipped_in_flight += 1
                state.judged_through = min(state.judged_through, seg.seq - 1)
                break
            trigger = JevTrigger(
                trigger_id=str(uuid.uuid4()),
                session_id=ctx.session_id,
                action=action,
                query=build_query(action, topic),
                topic=topic,
                seq_from=window[0].seq,
                seq_to=seg.seq,
                anchor_seq=seg.seq,
                context_text=context_text,
                probability=probability,
                decided_at=self._utcnow(),
                jev_latency_ms=int(round(result.latency_ms)),
                model=result.model,
                provider=provider,
            )
            state.watermark = max(state.watermark, seg.seq)
            state.last_fire_at = now
            state.in_flight[trigger.trigger_id] = now
            state.recent_topics.append((now, keywords, topic))
            state.recent_fires.append(
                (seg.end_s, seg.speaker, action, keywords - _REPHRASE_GENERIC)
            )
            self.metrics.fired += 1
            log.info(
                "jev_gate.fired",
                session_id=ctx.session_id,
                trigger_id=trigger.trigger_id,
                provider=provider,
                action=action,
                probability=round(probability, 3),
                seq=seg.seq,
                latency_ms=round(result.latency_ms, 1),
            )
            fired.append(trigger)
        return fired

    async def _emit(
        self, trigger: JevTrigger, session_cb: Callable[[Any], Awaitable[Any]] | None
    ) -> None:
        calls: list[tuple[Callable[[Any], Awaitable[Any]], Any]] = []
        if self._on_trigger is not None:
            calls.append((self._on_trigger, trigger))
        if session_cb is not None:
            calls.append((session_cb, to_live_trigger(trigger)))
        for callback, payload in calls:
            try:
                async with asyncio.timeout(self.settings.callback_timeout_s):
                    await callback(payload)
            except Exception as exc:  # noqa: BLE001 - the session loop must survive callbacks
                self.metrics.callback_errors += 1
                log.error(
                    "jev_gate.callback_failed",
                    trigger_id=trigger.trigger_id,
                    error_type=type(exc).__name__,
                )


# Provider-neutral name for the same class.
DecisionGate = JevGate


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _error_label(exc: JevError) -> str:
    """Short machine-readable failure label for ticks / config (never the message)."""
    if isinstance(exc, JevTimeout):
        return "timeout"
    if exc.code:
        return exc.code
    if exc.status is not None:
        return f"http_{exc.status}"
    return "transport"


def json_quote(text: str) -> str:
    return '"' + " ".join(text.split()).replace('"', "'")[:80] + '"'


# ---------------------------------------------------------------------------
# Claims + optional fast responder
# ---------------------------------------------------------------------------


class TriggerClaims(Protocol):
    """Atomic first-claimer-wins over a trigger id.

    Production: the live-backend implements this on `live_triggers`
    (`UPDATE ... SET claimed_by=:c, claimed_at=now() WHERE id=:t AND
    claimed_by IS NULL RETURNING id`, then treat `claimed_by = :c` as success
    so a re-claim by the same claimer is idempotent). The MCP
    `claim_live_trigger` tool and the fallback responder must go through the
    same implementation so a trigger is answered at most once.
    """

    async def claim(self, trigger_id: str, claimer: str) -> bool: ...


class InMemoryTriggerClaims:
    """Process-local `TriggerClaims`. Correct only if every claimer lives in this
    process (single uvicorn worker); use the DB-backed one otherwise."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}

    async def claim(self, trigger_id: str, claimer: str) -> bool:
        # setdefault is atomic on the event loop (no await between test and set).
        return self._owners.setdefault(trigger_id, claimer) == claimer

    def owner(self, trigger_id: str) -> str | None:
        return self._owners.get(trigger_id)

    def forget(self, trigger_id: str) -> None:
        self._owners.pop(trigger_id, None)


@dataclass(frozen=True, slots=True)
class FallbackFinding:
    """What the fallback responder asks the live-backend to post as a finding."""

    trigger_id: str
    session_id: str
    action: str
    title: str
    body: str
    citations: tuple[Mapping[str, Any], ...]
    claimer: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "kind": self.action,
            "title": self.title,
            "body": self.body,
            "citations": [dict(c) for c in self.citations],
            "source": self.claimer,
        }


SearchFn = Callable[[JevTrigger, Any], Awaitable[Sequence[Mapping[str, Any]]]]
PostFindingFn = Callable[[FallbackFinding], Awaitable[None]]

_FINDING_TITLES: dict[str, str] = {
    "lookup_past": "From earlier meetings",
    "fact_check": "What earlier meetings recorded",
    "find_owner": "Who has handled this before",
    "recall_action_items": "Related action items",
    "define_term": "How this term has been used",
    "look_up": "What earlier meetings recorded",
}


def format_fallback_finding(
    trigger: JevTrigger, items: Sequence[Mapping[str, Any]], *, claimer: str
) -> FallbackFinding:
    lines = [f"Auto-retrieved for: {trigger.topic}"]
    citations: list[Mapping[str, Any]] = []
    for item in items:
        title = str(item.get("title") or "Untitled meeting")
        when = str(item.get("scheduled_start") or "")[:10]
        speaker = item.get("speaker")
        passage = " ".join(str(item.get("passage") or "").split())
        if len(passage) > 220:
            passage = passage[:220].rsplit(" ", 1)[0] + "…"
        who = f"{speaker}: " if speaker else ""
        lines.append(f"• {title}{f' ({when})' if when else ''} — {who}“{passage}”")
        citations.append(
            {
                "meeting_id": item.get("meeting_id"),
                "title": title,
                "url": item.get("url"),
                "source": item.get("source"),
                "start_seconds": item.get("start_seconds"),
            }
        )
    return FallbackFinding(
        trigger_id=trigger.trigger_id,
        session_id=trigger.session_id,
        action=trigger.action,
        title=_FINDING_TITLES.get(trigger.action, "Related context"),
        body="\n".join(lines),
        citations=tuple(citations),
        claimer=claimer,
    )


class FallbackResponder:
    """Answers a trigger itself when no agent claims it within `claim_wait_s`.

    Flow per trigger: wait -> `claims.claim(id, "jev-gate-fallback")` -> if won,
    search past meetings as the session owner -> post one finding (or nothing
    when the search is empty) -> `on_resolved(trigger)`. If an agent claimed
    first, do nothing: the agent's finding resolves the trigger.
    """

    claimer_id = "jev-gate-fallback"

    def __init__(
        self,
        *,
        claims: TriggerClaims,
        search: SearchFn,
        post_finding: PostFindingFn,
        claim_wait_s: float = 3.0,
        max_results: int = 3,
        search_timeout_s: float = 8.0,
        on_resolved: Callable[[JevTrigger], None] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._claims = claims
        self._search = search
        self._post = post_finding
        self._claim_wait_s = claim_wait_s
        self._max_results = max_results
        self._search_timeout_s = search_timeout_s
        self._on_resolved = on_resolved
        self._sleep = sleep
        self._tasks: set[asyncio.Task[Any]] = set()
        self.counts: dict[str, int] = {
            "scheduled": 0,
            "claimed_by_agent": 0,
            "posted": 0,
            "empty": 0,
            "errors": 0,
        }

    def schedule(
        self, trigger: JevTrigger, *, owner_user_id: Any = None
    ) -> asyncio.Task[FallbackFinding | None]:
        self.counts["scheduled"] += 1
        task = asyncio.get_running_loop().create_task(
            self.respond(trigger, owner_user_id=owner_user_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def respond(
        self, trigger: JevTrigger, *, owner_user_id: Any = None
    ) -> FallbackFinding | None:
        await self._sleep(self._claim_wait_s)
        try:
            won = await self._claims.claim(trigger.trigger_id, self.claimer_id)
        except Exception as exc:  # noqa: BLE001
            self.counts["errors"] += 1
            log.error(
                "jev_gate.claim_failed", trigger_id=trigger.trigger_id, error=type(exc).__name__
            )
            return None
        if not won:
            self.counts["claimed_by_agent"] += 1
            return None
        try:
            async with asyncio.timeout(self._search_timeout_s):
                items = list(await self._search(trigger, owner_user_id))[: self._max_results]
            if not items:
                self.counts["empty"] += 1
                return None
            finding = format_fallback_finding(trigger, items, claimer=self.claimer_id)
            await self._post(finding)
            self.counts["posted"] += 1
            return finding
        except Exception as exc:  # noqa: BLE001 - never take the session down
            self.counts["errors"] += 1
            log.error(
                "jev_gate.fallback_failed", trigger_id=trigger.trigger_id, error=type(exc).__name__
            )
            return None
        finally:
            if self._on_resolved is not None:
                self._on_resolved(trigger)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


_SEARCH_SOURCE_HINT: dict[str, str | None] = {
    "lookup_past": None,
    "fact_check": None,
    "find_owner": None,
    "recall_action_items": "summary",
    "define_term": None,
    "look_up": None,
}


def make_context_search(*, scope: str = "all", limit: int = 3) -> SearchFn:
    """Default `SearchFn`: `services.context_search.search_context` as the session owner.

    Runs in a worker thread (sync SQLAlchemy + embeddings call). Uses a short
    keyword query because FTS ANDs every term; relaxes to the 3 longest
    keywords, then drops the source hint, before giving up.
    """

    async def search(trigger: JevTrigger, owner_user_id: Any) -> Sequence[Mapping[str, Any]]:
        if owner_user_id is None:
            return []
        return await anyio.to_thread.run_sync(
            _context_search_sync,
            trigger.topic,
            owner_user_id,
            _SEARCH_SOURCE_HINT.get(trigger.action),
            scope,
            limit,
        )

    return search


def _context_search_sync(
    topic: str, owner_user_id: Any, source: str | None, scope: str, limit: int
) -> list[dict[str, Any]]:
    """RGS has no meeting archive to search; the built-in LLM answerer
    (`app.live.answer`) answers triggers instead of this optional responder."""
    return []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_gate(
    *,
    on_trigger: OnTrigger | None = None,
    settings: JevGateSettings | None = None,
    client: JevAsker | None = None,
    clients: Mapping[str, JevAsker] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    openai_transport: httpx.AsyncBaseTransport | None = None,
) -> JevGate | None:
    """Build the process-wide gate, or None when it is disabled or has no provider key.

    One client per provider whose key is set (`JEV_API_KEY`,
    `OPENAI_DECISIONS_API_KEY`); the default provider is `LIVE_GATE_PROVIDER`
    when usable. The live-backend imports this lazily and keeps one instance
    per process.
    """
    cfg = settings or get_jev_settings()
    if client is not None or clients:
        return JevGate(cfg, client=client, clients=clients, on_trigger=on_trigger)
    if not cfg.configured:
        log.info(
            "jev_gate.disabled",
            enabled=cfg.jev_gate_enabled,
            has_key=bool(cfg.jev_api_key.get_secret_value().strip()),
            has_openai_key=cfg.provider_available(PROVIDER_OPENAI),
        )
        return None
    built: dict[str, JevAsker] = {}
    if cfg.provider_available(PROVIDER_JEV):
        built[PROVIDER_JEV] = JevClient(
            api_key=cfg.jev_api_key.get_secret_value(),
            endpoint=cfg.jev_endpoint,
            model=cfg.jev_model,
            timeout_s=cfg.timeout_s,
            max_connections=cfg.max_concurrent_calls,
            transport=transport,
        )
    if cfg.provider_available(PROVIDER_OPENAI):
        from app.live.openai_decisions import OpenAIDecisionsClient

        built[PROVIDER_OPENAI] = OpenAIDecisionsClient(
            api_key=cfg.openai_decisions_api_key.get_secret_value(),
            base_url=cfg.openai_decisions_base_url,
            model=cfg.openai_decisions_model,
            timeout_s=cfg.openai_timeout_s,
            max_connections=cfg.max_concurrent_calls,
            transport=openai_transport if openai_transport is not None else transport,
        )
    return JevGate(cfg, clients=built, on_trigger=on_trigger)
