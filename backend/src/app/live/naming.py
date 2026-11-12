"""Who is speaking: name diarized speaker labels from the conversation itself.

Live diarization gives labels (S1, S2, ...); this module turns them into
people, as aggressively as the evidence allows. It is used by the live
runtime (fast path on every new line, a periodic LLM pass, and a last pass at
stop) and by `scripts/rename_session.py` for stored sessions.

Signals, cheapest first:

1. **Self-introductions** (`app.live.intro.detect_intro`) on each speaker's
   *utterances* (`app.live.utterances.coalesce`), so "Alice Ex" + "ample"
   is one sentence again. A garbled surname after a name is kept for matching.
2. **Directory resolution** (`app.live.people`): the spoken name is matched
   against locally known users with phonetic / fuzzy / nickname scoring.
3. **Name mentions** ("thanks Alice", "Carol, can you...") become cues and
   candidates for the LLM: the person addressed is probably the next one to
   answer, and is not the one talking.
4. **The optional LLM pass** (operator-configured endpoint, `LIVE_NAMING_MODEL`) reads the
   transcript, the speakers, the candidates and the cues, and returns a
   name + confidence + evidence per label, plus labels that are the same
   person (merge).
5. **Echo merges**: a short label whose words are mostly another label's
   words at the same moment is a diarization split, merged deterministically.

Decisions never touch a manually named or merged-away speaker. A rename
needs `live_naming_min_confidence`; a weaker guess becomes a suggestion
("maybe Alice Example?").
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.live import intro
from app.live.people import (
    Candidate,
    Person,
    Priors,
    confident_pick,
    display_name,
    fold,
    is_common_word,
    match_name,
    name_mentions,
    name_similarity,
    token_similarity,
)
from app.live.utterances import Utterance, coalesce

log = structlog.get_logger(__name__)

STRONG_INTRO_CONFIDENCE = intro.STRONG_CONFIDENCE  # 0.9
ATTENDEE_INTRO_CONFIDENCE = intro.ATTENDEE_CONFIDENCE  # 0.95
WEAK_INTRO_FULLNAME_SIMILARITY = 0.9
MERGE_MIN_CONFIDENCE = 0.75
SAME_PERSON_MERGE_CONFIDENCE = 0.8
ECHO_MAX_WORDS = 25
MAX_INTROS_PER_LINE = 4
# A label whose transcript says it is two different people carries two voices
# (diarizer mixing): its names stay suggestions at most this confident.
MIXED_MAX_CONFIDENCE = 0.45
MIXED_MIN_WEIGHT = 0.7  # the runner-up identity needs at least one real cue...
MIXED_RATIO = 0.5  # ...and half the weight of the leading one
ECHO_MIN_RATIO = 0.6
ECHO_TOLERANCE_S = 0.6
MAX_TRANSCRIPT_CHARS = 14000
MAX_CANDIDATES = 40


@dataclass(frozen=True)
class SpeakerView:
    label: str
    display_name: str
    source: str = "default"  # default | manual | self_intro | inferred
    confidence: float | None = None
    person_email: str | None = None
    speech_seconds: float = 0.0
    merged_into: str | None = None
    bound: bool = False  # bound to an RGS user (manual)

    @property
    def locked(self) -> bool:
        return self.source == "manual" or self.bound

    @property
    def named(self) -> bool:
        return self.source != "default" and not intro.is_default_name(self.label, self.display_name)


@dataclass(frozen=True)
class Cue:
    kind: str  # intro | mention
    label: str  # who said it
    seq: int
    start: float
    spoken: str
    evidence: str
    strong: bool = False
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class NameDecision:
    label: str
    name: str
    confidence: float
    evidence: str
    source: str  # self_intro | inferred
    email: str | None = None
    suggest_only: bool = False


@dataclass(frozen=True)
class MergeDecision:
    label: str  # merged away
    into: str
    confidence: float
    reason: str


@dataclass
class NamingContext:
    fragments: Sequence[tuple[int, str, float, float, str]]  # (seq, label, start, end, text)
    speakers: Sequence[SpeakerView]
    people: Sequence[Person] = ()
    attendees: Sequence[Person] = ()
    owner: Person | None = None
    colleagues: Sequence[Person] = ()
    recent_emails: frozenset[str] = frozenset()
    title: str = ""
    kind: str = "call"
    expected_speakers: int | None = None
    _utterances: list[Utterance] | None = field(default=None, repr=False)

    @property
    def by_label(self) -> dict[str, SpeakerView]:
        return {sp.label: sp for sp in self.speakers}

    def resolve(self, label: str) -> str:
        views = self.by_label
        seen: set[str] = set()
        while label in views and views[label].merged_into and label not in seen:
            seen.add(label)
            label = views[label].merged_into or label
        return label

    @property
    def utterances(self) -> list[Utterance]:
        if self._utterances is None:
            self._utterances = coalesce(self.fragments, resolve=self.resolve)
        return self._utterances

    @property
    def priors(self) -> Priors:
        return Priors(
            owner_email=self.owner.email if self.owner else None,
            attendee_emails=frozenset(p.email.lower() for p in self.attendees if p.email),
            recent_emails=self.recent_emails,
        )

    @property
    def directory(self) -> list[Person]:
        """Roster plus attendees that are not in it (e.g. external guests)."""
        seen = {p.email for p in self.people if p.email}
        extra = [p for p in self.attendees if not p.email or p.email not in seen]
        return [*self.people, *extra]

    def active(self) -> list[SpeakerView]:
        return [sp for sp in self.speakers if sp.merged_into is None]


# ---- cues ------------------------------------------------------------------------------

_TOKEN_AFTER = re.compile(r"^[\s,]*([A-Za-z][A-Za-z'’\-]{1,20})")


def _extend_name(text: str, match: intro.IntroMatch) -> tuple[str, str]:
    """(name, evidence) plus the next word when it reads like a (garbled) surname."""
    name = match.name
    if len(name.split()) >= 2:
        return name, match.evidence
    start = max(0, text.find(match.evidence))
    idx = text.find(name, start)
    if idx < 0:
        return name, match.evidence
    m = _TOKEN_AFTER.match(text[idx + len(name) :])
    if not m:
        return name, match.evidence
    nxt = m.group(1)
    if fold(nxt) in intro._NOT_NAMES or fold(nxt) in {"and", "but", "so", "here", "from"}:
        return name, match.evidence
    return f"{name} {nxt}", text[start : idx + len(name) + m.end()].strip(" ,.")[:160]


def resolve_spoken(
    spoken: str, ctx: NamingContext, *, limit: int = 5
) -> tuple[list[Candidate], Candidate | None]:
    """Directory candidates for a spoken name, and the confident pick if any."""
    cands = match_name(spoken, ctx.directory, ctx.priors, limit=limit)
    return cands, confident_pick(cands)


_INTRODUCES_OTHER = re.compile(r"(?i)\bthis\s+is\b")
_LOWER_WORD = re.compile(r"\b[a-z][a-z'\-]{2,20}\b")


def known_first_names(ctx: NamingContext) -> frozenset[str]:
    return frozenset(_first(p.name) for p in ctx.directory if _first(p.name))


def capitalize_names(text: str, firsts: frozenset[str]) -> str:
    """ "oh, I am alice, I'm bob" -> "oh, I am Alice, I'm Bob" (local first names only).

    The ASR often writes names in lower case; intro/address detection keys on
    capitals. Common words ("will", "mark", "like") are never touched.
    """

    def fix(m: re.Match[str]) -> str:
        w = m.group(0)
        f = fold(w)
        if is_common_word(w):
            return w
        if f in firsts or (len(f) >= 5 and any(_one_edit(f, g) for g in firsts)):
            return w[:1].upper() + w[1:]
        return w

    return _LOWER_WORD.sub(fix, text)


def _one_edit(a: str, b: str) -> bool:
    """`a` and `b` differ by one inserted/deleted/changed letter (alise ~ alice)."""
    if a == b or abs(len(a) - len(b)) > 1 or a[:1] != b[:1]:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) == 1
    short, long_ = sorted((a, b), key=len)
    i = next((k for k, (x, y) in enumerate(zip(short, long_, strict=False)) if x != y), len(short))
    return short[i:] == long_[i + 1 :]


_STUTTER = re.compile(r"\b(\w+)(?:,? \1\b)+", re.IGNORECASE)


def destutter(text: str) -> str:
    """ "So oh I I am am Alice" -> "So oh I am Alice" (ASR word repeats)."""
    return _STUTTER.sub(r"\1", text)


def _sentence_owner(u: Utterance, nxt: Utterance | None) -> str:
    """Who said `u`'s last sentence: the next label when it finishes it.

    The diarizer often gives the first words of a turn to the previous
    speaker: "I'm Alice, and I" (S1) + "wrote the demo docs." (S2,
    lowercase, right after) is S2's sentence.
    """
    text = u.text.rstrip()
    if (
        nxt is None
        or nxt.speaker == u.speaker
        or text.endswith((".", "?", "!"))
        or nxt.start - u.end > 1.0
        or not nxt.text[:1].islower()
    ):
        return u.speaker
    return nxt.speaker


def find_cues(ctx: NamingContext, utterances: Sequence[Utterance] | None = None) -> list[Cue]:
    """Self-introductions and name mentions in the conversation."""
    cues: list[Cue] = []
    utts = list(utterances if utterances is not None else ctx.utterances)
    firsts = known_first_names(ctx)
    for i, u in enumerate(utts):
        nxt = utts[i + 1] if i + 1 < len(utts) else None
        intro_spans: list[str] = []
        text = destutter(capitalize_names(u.text, firsts))
        # Several intros can share a line ("I am Alice, oh, I am Bob, I'm Carol.").
        rest = " ".join(text.split())
        for n in range(MAX_INTROS_PER_LINE):
            match = intro.detect_intro(rest)
            at = rest.find(match.evidence) if match is not None else -1
            if match is None or at < 0:
                break
            if n and _INTRODUCES_OTHER.search(match.evidence):
                # "I'm Alice, and this is Bob": the second one is someone else.
                rest = rest[at + len(match.evidence) :]
                continue
            spoken, evidence = _extend_name(rest, match)
            cands, _pick = resolve_spoken(spoken, ctx)
            # Keep the plain first name if the two-word reading matches nothing better.
            if spoken != match.name:
                plain, _ = resolve_spoken(match.name, ctx)
                if (plain and not cands) or (
                    plain and cands and plain[0].similarity > cands[0].similarity + 0.05
                ):
                    spoken, cands, evidence = match.name, plain, match.evidence
            # Only an intro in the line's unfinished last sentence can be borrowed.
            last_sentence = not re.search(r"[.?!]", rest[at:])
            cues.append(
                Cue(
                    kind="intro",
                    label=_sentence_owner(u, nxt) if last_sentence else u.speaker,
                    seq=u.seq,
                    start=u.start,
                    spoken=spoken,
                    evidence=evidence,
                    strong=match.strong,
                    candidates=tuple(cands),
                )
            )
            intro_spans.append(fold(spoken))
            rest = rest[at + len(match.evidence) :]
        for spoken, cands in name_mentions(text, ctx.directory, ctx.priors):
            if any(fold(spoken) in span for span in intro_spans):
                continue
            cues.append(
                Cue(
                    kind="mention",
                    label=u.speaker,
                    seq=u.seq,
                    start=u.start,
                    spoken=spoken,
                    evidence=_snippet(text, spoken),
                    candidates=tuple(cands),
                )
            )
    return cues


def _snippet(text: str, word: str, width: int = 70) -> str:
    i = text.find(word)
    if i < 0:
        return text[: width * 2]
    a, b = max(0, i - width), min(len(text), i + len(word) + width)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def intro_decision(cue: Cue, ctx: NamingContext) -> NameDecision | None:
    """Deterministic name for a self-introduction (None = leave it to the LLM)."""
    if cue.kind != "intro":
        return None
    pick = confident_pick(list(cue.candidates))
    attendee_names = [p.name for p in ctx.attendees]
    resolved = intro.resolve_name(cue.spoken.split()[0], attendee_names)
    strong = cue.strong
    if pick is not None:
        p = pick.person
        is_attendee = "attendee" in pick.reasons
        full = len(cue.spoken.split()) >= 2 and pick.similarity >= WEAK_INTRO_FULLNAME_SIMILARITY
        if not strong and not (full or is_attendee):
            return None
        base = ATTENDEE_INTRO_CONFIDENCE if is_attendee else STRONG_INTRO_CONFIDENCE
        confidence = round(min(0.97, base * (0.5 + 0.5 * min(pick.similarity, 1.0))), 3)
        return NameDecision(
            label=cue.label,
            name=p.name,
            email=p.email or None,
            confidence=confidence,
            evidence=_evidence(cue, p.name),
            source="self_intro",
        )
    if resolved.from_attendee:
        return NameDecision(
            label=cue.label,
            name=resolved.name,
            confidence=ATTENDEE_INTRO_CONFIDENCE,
            evidence=_evidence(cue, resolved.name),
            source="self_intro",
        )
    if not strong:
        return None
    if cue.candidates and cue.candidates[0].similarity >= 0.8:
        # Sounds like somebody we know, but not clearly one person: ask the LLM.
        return None
    tidy = " ".join(w if w[:1].isupper() else w.capitalize() for w in cue.spoken.split())
    return NameDecision(
        label=cue.label,
        name=tidy,
        confidence=STRONG_INTRO_CONFIDENCE,
        evidence=_evidence(cue, tidy),
        source="self_intro",
    )


def _evidence(cue: Cue, name: str) -> str:
    said = f'said "{cue.evidence}"'
    if fold(cue.spoken) != fold(name):
        said += f" (heard as {cue.spoken!r})"
    return said[:160]


# ---- address -> reply ----------------------------------------------------------------------

_NAME = r"(?P<name>[A-Z][a-z'’\-]{2,20})"
_INTERJ = r"(?:ok(?:ay)?|so|hey|hi|hello|great|right|alright|and|also|yeah|yes|well|now|um|uh|oh)"
_THANKS = r"(?:thanks|thank\s+you(?:\s+so\s+much)?|cheers|good\s+point|nice\s+one|sorry)"
_ASK = r"(?:who|what|when|where|why|how|can|could|would|will|do|did|does|are|is|have|you|any)"
# (pattern, direction, weight): forward = the next speaker is NAME; back = the previous one.
_VOCATIVES = (
    # "what do you think, Alice?" / "right, Carol?"
    (re.compile(r",\s*" + _NAME + r"\s*\?"), "forward", 1.0),
    # "Carol, can you ..." / "Alice, who owns ..." (anywhere in the line)
    (re.compile(_NAME + r",\s+" + _ASK + r"\b"), "forward", 1.0),
    # "thanks Carol" / "thank you, Alice" -> the person who just spoke
    (re.compile(r"(?i:\b" + _THANKS + r")[,!.]?\s+" + _NAME + r"\b"), "back", 0.8),
    # "Alice, ..." / "Great, Alice. did you ..." at the start of a sentence, unless
    # it is a restarted subject ("Carol, Carol had ...", "Alice, she said ...")
    (
        re.compile(
            r"(?:^|[.!?]\s+)(?:(?i:" + _INTERJ + r")[,!.]?\s+)?" + _NAME + r"\s*[,.!]"
            r"(?!\s*(?i:(?:uh|um)[,]?\s+)?(?:(?P=name)|(?i:he|she|they|has|had|is|was|"
            r"said|says|and|or|would|will|told|thinks|did|does)\b))"
        ),
        "forward",
        0.7,
    ),
)
_NOT_VOCATIVE = frozenset(
    "okay yes yeah great thanks thank sorry right well also good sure hey hello so and "
    "now just actually maybe basically cool nice perfect awesome exactly".split()
)
ADDRESS_REPLY_WINDOW_S = 8.0
ADDRESS_BACK_WINDOW_S = 10.0
ADDRESS_MIN_SIMILARITY = 0.85
ADDRESS_OVERRIDE_CONFIDENCE = 0.85
ADDRESS_BACK_MAX_OFFSET = 40  # chars: "thanks X" must open the turn to point back


@dataclass(frozen=True)
class Address:
    """`by` said `spoken` to someone; `to` is the speaker who answered (or was thanked)."""

    by: str
    to: str
    spoken: str
    person: Person
    weight: float
    seq: int
    evidence: str


def _vocatives(text: str) -> list[tuple[str, str, float, int]]:
    """(name, direction, weight, offset) for each name said *to* someone in `text`."""
    found: dict[str, tuple[str, str, float, int]] = {}
    for pattern, direction, weight in _VOCATIVES:
        for m in pattern.finditer(text):
            name = m.group("name")
            if fold(name) in _NOT_VOCATIVE or is_common_word(name):
                continue
            key = f"{fold(name)}:{direction}"
            if key not in found or found[key][2] < weight:
                found[key] = (name, direction, weight, m.start("name"))
    return list(found.values())


def find_addresses(ctx: NamingContext) -> list[Address]:
    """Address -> reply events: a name said to someone, and who that someone is.

    "Alice, did you see the demo?" -> the next different speaker (within
    8 s) is Alice; "thanks, Alice" -> the previous different speaker (within
    10 s) is. The speaker who says a name is never that person.
    """
    utts = ctx.utterances
    firsts = known_first_names(ctx)
    out: list[Address] = []
    for i, u in enumerate(utts):
        text = capitalize_names(u.text, firsts)
        intro_match = intro.detect_intro(text)
        intro_first = fold(intro_match.name.split()[0]) if intro_match else ""
        for name, direction, weight, offset in _vocatives(text):
            if fold(name) == intro_first:
                continue  # A self-introduction is not an address cue.
            if direction == "back" and offset > ADDRESS_BACK_MAX_OFFSET:
                continue  # mid-turn thanks: the "previous speaker" is this one
            _cands, pick = resolve_spoken(name, ctx)
            if pick is None or pick.similarity < ADDRESS_MIN_SIMILARITY:
                continue
            to = None
            if direction == "forward":
                nxt = next((v for v in utts[i + 1 :] if v.speaker != u.speaker), None)
                if nxt is not None and nxt.start - u.end <= ADDRESS_REPLY_WINDOW_S:
                    to = nxt.speaker
            else:
                prev = next((v for v in reversed(utts[:i]) if v.speaker != u.speaker), None)
                if prev is not None and u.start - prev.end <= ADDRESS_BACK_WINDOW_S:
                    to = prev.speaker
            if to is None or to == u.speaker:
                continue
            out.append(
                Address(
                    by=u.speaker,
                    to=to,
                    spoken=name,
                    person=pick.person,
                    weight=weight,
                    seq=u.seq,
                    evidence=_snippet(text, name, width=50),
                )
            )
    return out


def address_decisions(ctx: NamingContext, addresses: Sequence[Address]) -> list[NameDecision]:
    """Accumulate address -> reply evidence into one name per label.

    Evidence for (label, person) adds up over events; a label that itself says
    the name counts against it; evidence for other people (or the same person
    on other labels) dilutes it. One event ~0.7, two ~0.85, three+ ~0.92.
    """
    score: dict[tuple[str, str], float] = {}
    people: dict[str, Person] = {}
    said: dict[tuple[str, str], float] = {}
    notes: dict[tuple[str, str], list[str]] = {}
    for a in addresses:
        key = (a.to, a.person.email or fold(a.person.name))
        score[key] = score.get(key, 0.0) + a.weight
        people[key[1]] = a.person
        said[(a.by, key[1])] = said.get((a.by, key[1]), 0.0) + a.weight
        verb = "thanked" if a.weight < 1.0 and a.to != a.by else "addressed"
        notes.setdefault(key, []).append(f"{a.by} {verb} as {a.spoken!r} ({a.evidence.strip()})")
    by_label: dict[str, float] = {}
    by_person: dict[str, float] = {}
    for (label, pid), w in score.items():
        by_label[label] = by_label.get(label, 0.0) + w
        by_person[pid] = by_person.get(pid, 0.0) + w
    out: list[NameDecision] = []
    for (label, pid), w in score.items():
        net = w - said.get((label, pid), 0.0)
        if net <= 0:
            continue
        base = 0.55 + 0.42 * (1 - 0.4**net)  # 1 strong event ~0.80, 2 ~0.90, 3 ~0.94
        share = min(w / by_label[label], w / by_person[pid])
        conf = round(min(0.95, base * share), 3)
        person = people[pid]
        out.append(
            NameDecision(
                label=label,
                name=person.name,
                email=person.email or None,
                confidence=conf,
                evidence=("; ".join(notes[(label, pid)]))[:200],
                source="inferred",
            )
        )
    best: dict[str, NameDecision] = {}
    for d in sorted(out, key=lambda d: d.confidence, reverse=True):
        best.setdefault(d.label, d)
    return list(best.values())


# ---- echo merges ---------------------------------------------------------------------------


def _words(fragments: Sequence[tuple[int, str, float, float, str]], label: str):
    out = []
    for _seq, lab, start, end, text in fragments:
        if lab != label:
            continue
        toks = [t for t in fold(text).split() if t]
        if not toks:
            continue
        step = (end - start) / len(toks) if end > start else 0.0
        out.extend((t, start + i * step) for i, t in enumerate(toks))
    return out


def echo_merges(ctx: NamingContext) -> list[MergeDecision]:
    """Short labels whose words mostly repeat another label's words at the same time."""
    active = ctx.active()
    out: list[MergeDecision] = []
    raw_labels = {lab for _s, lab, _a, _b, _t in ctx.fragments}
    words = {lab: _words(ctx.fragments, lab) for lab in raw_labels}
    for sp in active:
        if sp.locked or sp.named:
            continue
        mine = [w for lab in raw_labels if ctx.resolve(lab) == sp.label for w in words[lab]]
        if not mine or len(mine) > ECHO_MAX_WORDS:
            continue
        best: tuple[float, str] | None = None
        for other in active:
            if other.label == sp.label:
                continue
            theirs = [
                w for lab in raw_labels if ctx.resolve(lab) == other.label for w in words[lab]
            ]
            if not theirs:
                continue
            hits = sum(
                1
                for tok, t in mine
                if any(
                    abs(t - t2) <= ECHO_TOLERANCE_S and token_similarity(tok, tok2) >= 0.85
                    for tok2, t2 in theirs
                )
            )
            ratio = hits / len(mine)
            if ratio >= ECHO_MIN_RATIO and (best is None or ratio > best[0]):
                best = (ratio, other.label)
        if best is not None:
            out.append(
                MergeDecision(
                    label=sp.label,
                    into=best[1],
                    confidence=round(0.7 + 0.3 * best[0], 3),
                    reason=f"{round(best[0] * 100)}% of its words echo {best[1]} at once",
                )
            )
    return out


def intro_merges(
    cues: Sequence[Cue], decided: Sequence[NameDecision], ctx: NamingContext
) -> list[MergeDecision]:
    """Two labels that introduce themselves with the same name are one person."""
    by_spoken: dict[str, set[str]] = {}
    for cue in cues:
        if cue.kind == "intro":
            by_spoken.setdefault(fold(cue.spoken).split()[0], set()).add(cue.label)
    strong = {d.label for d in decided}
    out: list[MergeDecision] = []
    for spoken, labels in by_spoken.items():
        anchors = sorted(labels & strong) or sorted(labels)
        anchor = anchors[0]
        for label in sorted(labels - {anchor}):
            out.append(
                MergeDecision(
                    label=label,
                    into=anchor,
                    confidence=0.8,
                    reason=f'both introduced themselves as "{spoken.capitalize()}"',
                )
            )
    return out


# ---- LLM pass ---------------------------------------------------------------------------------

NAMING_TOOL: dict[str, Any] = {
    "name": "identify_speakers",
    "description": "Record who each diarized speaker label is, and labels that are one person.",
    "input_schema": {
        "type": "object",
        "properties": {
            "speakers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "name": {"type": ["string", "null"]},
                        "email": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["label", "name", "confidence", "evidence"],
                },
            },
            "same_person": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "same_as": {"type": "string"},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["label", "same_as", "confidence"],
                },
            },
        },
        "required": ["speakers", "same_person"],
    },
}

NAMING_SYSTEM = """\
You identify the people behind diarized speaker labels (S1, S2, ...) in a live \
speech-recognition transcript. For example, names \
may be misheard ("Alis" for Alice Example, "Karol" for Carol Demo), one sentence \
is often split over several lines, and the \
diarizer sometimes splits one person into two labels or leaks a few words of one \
person into a short extra label.

Evidence, strongest first:
1. Self-introduction: "I'm X", "my name is X", "X here" - even when the name is garbled.
2. Introduced by someone else ("I'm here with Alice", "this is Carol from the demo team"): \
the introduced person is another label, usually the next new voice.
3. Addressed by name: when a speaker says a name to someone ("thanks Alice", "Alice, can \
you...", "what do you think, Carol?"), the speaker who answers next is probably that \
person, and the speaker who said the name is NOT that person.
4. Third-person mentions ("Alice said...") mean that person is not the one talking and \
may not be present.
5. Context: the recorder (who started this transcription) is almost always present \
and is usually the one who explains the tool or runs the meeting; elimination among \
the people known to be present (the title often names them). When CONSTRAINTS rule \
a label out of a name and exactly one present person is left for it, that \
elimination is good evidence (0.75).

Turn boundaries are imprecise: the diarizer often gives the first words of a turn to \
the previous speaker. When a line ends mid-sentence and the next label's line finishes \
that sentence ("I'm Alice and I" then S2 "wrote the demo docs."), the whole \
sentence is the NEXT label's - including any self-introduction in \
it. A label that introduces itself twice as two different people has one borrowed \
introduction; give it the one that is not continued by another label.

Rules:
- Map a heard name to the CANDIDATES list when it plausibly sounds like one of them \
(same or similar first name, nickname, similar sound); then return that candidate's \
exact name and email. Use a name that is not a candidate only when the transcript \
clearly gives it (email null).
- Never give two labels the same person unless you also list them in same_person.
- confidence is your calibrated probability that the name is right: 0.9+ for a clear \
self-introduction or an unambiguous address-and-reply; 0.6-0.8 for good but indirect \
evidence; below 0.5 for a guess. name null when there is no evidence at all.
- same_person: a label that is clearly the same human as another (both introduce \
themselves with the same name, or a short label that only repeats another label's \
words at the same moment). Only with real evidence.
- Return every label in SPEAKERS exactly once in `speakers`.\
"""


def _clock(seconds: float) -> str:
    total = int(max(0.0, seconds))
    m, s = divmod(total, 60)
    return f"{m:02d}:{s:02d}"


def render_transcript(utterances: Sequence[Utterance], cue_seqs: set[int]) -> str:
    """ "[mm:ss] S2: text" lines; when too long, name-cue lines plus the recent tail."""
    lines = [f"[{_clock(u.start)}] {u.speaker}: {u.text}" for u in utterances]
    if sum(len(line) + 1 for line in lines) <= MAX_TRANSCRIPT_CHARS:
        return "\n".join(lines)
    keep = {i for i, u in enumerate(utterances) if set(u.seqs) & cue_seqs}
    budget = MAX_TRANSCRIPT_CHARS - sum(len(lines[i]) + 1 for i in keep)
    for i in range(len(lines) - 1, -1, -1):
        if i in keep:
            continue
        if budget - len(lines[i]) - 1 < 0:
            break
        budget -= len(lines[i]) + 1
        keep.add(i)
    out: list[str] = []
    prev = -1
    for i in sorted(keep):
        if i != prev + 1:
            out.append("(...)")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


def candidate_people(ctx: NamingContext, cues: Sequence[Cue]) -> list[tuple[Person, str]]:
    """People the LLM may choose from, with why each is listed."""
    out: dict[str, tuple[Person, str]] = {}

    def add(p: Person, why: str) -> None:
        key = p.email or fold(p.name)
        if key not in out:
            out[key] = (p, why)

    if ctx.owner is not None:
        add(ctx.owner, "the recorder")
    for p in ctx.attendees:
        add(p, "invited")
    for cue in cues:
        for c in cue.candidates[:3]:
            add(c.person, f'sounds like "{cue.spoken}"')
    for p in ctx.colleagues:
        add(p, "works with the recorder")
    return list(out.values())[:MAX_CANDIDATES]


def build_prompt(
    ctx: NamingContext,
    cues: Sequence[Cue],
    *,
    mixed: dict[str, str] | None = None,
    ruled_out: dict[str, dict[str, str]] | None = None,
) -> str:
    parts: list[str] = []
    if ctx.owner is not None:
        parts.append(f"RECORDER: {ctx.owner.describe()} <{ctx.owner.email}>")
    kind = "in-person conversation" if ctx.kind == "adhoc" else "call"
    parts.append(f"CONVERSATION: {ctx.title or '(untitled)'} ({kind})")
    if ctx.expected_speakers:
        parts.append(f"EXPECTED NUMBER OF PEOPLE TALKING: {ctx.expected_speakers}")
    parts.append("SPEAKERS (label - current name - seconds of speech):")
    for sp in sorted(ctx.active(), key=lambda s: s.label):
        if sp.named:
            how = "set by a person, keep it" if sp.locked else sp.source.replace("_", "-")
            conf = f", {sp.confidence:.2f}" if sp.confidence is not None and not sp.locked else ""
            current = f"{sp.display_name} ({how}{conf})"
        else:
            current = "unnamed"
        parts.append(f"- {sp.label} - {current} - {sp.speech_seconds:.0f} s")
    parts.append("CANDIDATES (name <email> - role - why listed):")
    for p, why in candidate_people(ctx, cues):
        email = f" <{p.email}>" if p.email else ""
        parts.append(f"- {p.describe()}{email} - {why}")
    if cues:
        parts.append("NAME CUES (found automatically; may be wrong):")
        for cue in cues[-30:]:
            sounds = ", ".join(c.person.name for c in cue.candidates[:3]) or "nobody known"
            if cue.kind == "intro":
                what = f'introduced themself as "{cue.spoken}"'
            else:
                what = f'said the name "{cue.spoken}"'
            parts.append(f"- [{_clock(cue.start)}] {cue.label} {what} (sounds like: {sounds})")
    facts: list[str] = []
    for label, why in sorted((mixed or {}).items()):
        facts.append(
            f"- {label} carries two voices ({why}): its name is only a suggestion. Still "
            "name its main voice if the transcript shows who that is (who runs the "
            "meeting, who is addressed, who does what), and count that person as "
            "accounted for when eliminating"
        )
    for label, said in sorted((ruled_out or {}).items()):
        for first, line in said.items():
            name = first.title()
            facts.append(
                f"- {label} is NOT {name} (said: {line.strip()!r}): never name it {name}; "
                "name it by elimination if one present person is left"
            )
    if facts:
        parts.append("CONSTRAINTS (found automatically):")
        parts.extend(facts)
    cue_seqs = {c.seq for c in cues[-30:]}
    parts.append("TRANSCRIPT:")
    parts.append(render_transcript(ctx.utterances, cue_seqs))
    return "\n".join(parts)


class NamingLLMError(RuntimeError):
    pass


def call_gateway(
    prompt: str,
    *,
    model: str,
    timeout_s: float,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """One forced tool call on the platform LLM gateway (Anthropic Messages)."""
    from app.live.answer import gateway_config

    cfg = gateway_config()
    if cfg is None:
        raise NamingLLMError("LLM gateway not configured")
    base, key = cfg
    try:
        with httpx.Client(timeout=timeout_s, transport=transport) as client:
            resp = client.post(
                f"{base}/v1/messages",
                headers={
                    "x-api-key": key,
                    "authorization": f"Bearer {key}",
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 1200,
                    "system": NAMING_SYSTEM,
                    "messages": [{"role": "user", "content": prompt}],
                    "tools": [NAMING_TOOL],
                    "tool_choice": {"type": "tool", "name": NAMING_TOOL["name"]},
                },
            )
    except httpx.HTTPError as exc:
        raise NamingLLMError(f"gateway call failed: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise NamingLLMError(f"gateway HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    for part in data.get("content") or []:
        if part.get("type") == "tool_use" and isinstance(part.get("input"), dict):
            return part["input"]
    text = "".join(p.get("text", "") for p in data.get("content") or [] if p.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise NamingLLMError("no tool call in the reply")
    try:
        return json.loads(m.group(0))
    except ValueError as exc:
        raise NamingLLMError("unparseable reply") from exc


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _person_for(name: str, email: str | None, ctx: NamingContext) -> Person | None:
    people = ctx.directory
    if email:
        key = email.strip().lower()
        hit = next((p for p in people if p.email and p.email.lower() == key), None)
        if hit is not None:
            return hit
    exact = [p for p in people if fold(p.name) == fold(name)]
    if len(exact) == 1:
        return exact[0]
    cands = match_name(name, people, ctx.priors, limit=3)
    if (
        cands
        and cands[0].similarity >= 0.95
        and (len(cands) == 1 or cands[0].score - cands[1].score >= 0.1)
    ):
        return cands[0].person
    return None


def parse_llm(
    raw: dict[str, Any], ctx: NamingContext, *, intro_labels: set[str] = frozenset()
) -> tuple[list[NameDecision], list[MergeDecision]]:
    """LLM tool output -> validated decisions (unknown labels/people dropped)."""
    active = {sp.label for sp in ctx.active()}
    names: list[NameDecision] = []
    for item in raw.get("speakers") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        name = " ".join(str(item.get("name") or "").split())[:120]
        if label not in active or not name or name.lower() in ("null", "none", "unknown"):
            continue
        email = item.get("email") if isinstance(item.get("email"), str) else None
        person = _person_for(name, email, ctx)
        final = person.name if person else display_name(name)
        names.append(
            NameDecision(
                label=label,
                name=final,
                email=(person.email or None) if person else None,
                confidence=round(_clamp(item.get("confidence")), 3),
                evidence=" ".join(str(item.get("evidence") or "").split())[:160],
                source="self_intro" if label in intro_labels else "inferred",
            )
        )
    merges: list[MergeDecision] = []
    for item in raw.get("same_person") or []:
        if not isinstance(item, dict):
            continue
        a = str(item.get("label") or "").strip()
        b = str(item.get("same_as") or "").strip()
        if a in active and b in active and a != b:
            merges.append(
                MergeDecision(
                    label=a,
                    into=b,
                    confidence=round(_clamp(item.get("confidence")), 3),
                    reason=" ".join(str(item.get("reason") or "").split())[:160]
                    or "the same person",
                )
            )
    return names, merges


# ---- combining ----------------------------------------------------------------------------------


@dataclass
class NamingResult:
    names: list[NameDecision]
    merges: list[MergeDecision]
    cues: list[Cue]
    used_llm: bool = False
    llm_error: str | None = None
    # Labels that carry two voices -> why (never pinned to a name).
    mixed: dict[str, str] = field(default_factory=dict)


def _first(name: str) -> str:
    words = fold(name).split()
    return words[0] if words else ""


def mixed_labels(
    ctx: NamingContext, cues: Sequence[Cue], addresses: Sequence[Address]
) -> dict[str, str]:
    """Labels the transcript gives two different identities.

    Evidence per label: its self-introductions (1.0, weak 0.7) and address ->
    reply events (their weight), grouped by first name (Alice ~ Alicia). Mixed
    when the runner-up identity has >= 0.7 and >= half the leader's weight:
    "I am Alice" + "I am Bob" + "I'm Carol" on one label is three people on
    one voiceprint, whatever its single best name.
    """
    ev: dict[str, list[tuple[str, float]]] = {}
    for c in cues:
        if c.kind != "intro" or _INTRODUCES_OTHER.search(c.evidence):
            continue  # "this is Bob" may introduce someone else
        pick = confident_pick(list(c.candidates))
        name = pick.person.name if pick is not None else c.spoken
        ev.setdefault(c.label, []).append((name, 1.0 if c.strong else 0.7))
    for a in addresses:
        ev.setdefault(a.to, []).append((a.person.name, a.weight))
    out: dict[str, str] = {}
    for label, items in ev.items():
        groups: list[list[Any]] = []  # [first name, display, weight]
        for name, w in items:
            first = _first(name)
            if not first:
                continue
            for g in groups:
                if token_similarity(first, g[0]) >= 0.8:
                    g[2] += w
                    break
            else:
                groups.append([first, name, w])
        groups.sort(key=lambda g: g[2], reverse=True)
        if len(groups) >= 2:
            top, second = groups[0][2], groups[1][2]
            if second >= MIXED_MIN_WEIGHT and second >= MIXED_RATIO * top:
                who = " / ".join(str(g[1]) for g in groups[:3])
                out[label] = f"the transcript says it is {who}"
    return out


_NOT_ME = (
    re.compile(r"\b(?P<name>[A-Z][a-z'\-]{2,20}) and I\b"),
    re.compile(r"\b(?i:me and) (?P<name>[A-Z][a-z'\-]{2,20})\b"),
    re.compile(
        r"\b(?P<name>[A-Z][a-z'\-]{2,20}) (?i:said|says|told me|mentioned|thinks|was saying)\b"
    ),
)


def not_names(ctx: NamingContext) -> dict[str, dict[str, str]]:
    """label -> {first name: line}: names a speaker uses for someone else.

    "Alice and I wrote the demo..." means the speaker is not Alice.
    """
    firsts = known_first_names(ctx)
    out: dict[str, dict[str, str]] = {}
    for u in ctx.utterances:
        text = capitalize_names(u.text, firsts)
        for pattern in _NOT_ME:
            for m in pattern.finditer(text):
                name = m.group("name")
                if is_common_word(name):
                    continue
                out.setdefault(u.speaker, {}).setdefault(fold(name), _snippet(text, name, 40))
    return out


def _contradicts(name: str, said: dict[str, str]) -> str | None:
    first = _first(name)
    for other, line in said.items():
        if first and token_similarity(first, other) >= 0.8:
            return line
    return None


def swap_ruled_out(
    names: Sequence[NameDecision],
    said: dict[str, dict[str, str]],
    mixed: dict[str, str],
) -> list[NameDecision]:
    """Fix an LLM pair that breaks a NOT constraint by swapping the two names.

    S6 says "Alice and I" yet the LLM calls S6 Alice and S4 Carol: S6 is Carol and
    S4 Alice. Only when exactly one other single-voice label has an inferred name
    and the swap breaks no constraint; otherwise the drop in `run` handles it.
    """
    out = list(names)
    for i, d in enumerate(out):
        if d.source != "inferred" or _contradicts(d.name, said.get(d.label, {})) is None:
            continue
        others = [
            j
            for j, o in enumerate(out)
            if o.label != d.label
            and o.source == "inferred"
            and o.label not in mixed
            and _first(o.name) != _first(d.name)
        ]
        if len(others) != 1:
            continue
        j = others[0]
        o = out[j]
        if _contradicts(o.name, said.get(d.label, {})) or _contradicts(
            d.name, said.get(o.label, {})
        ):
            continue
        conf = round(min(d.confidence, o.confidence), 3)
        why = f"swapped: {d.label} is not {_first(d.name).title()}"
        out[i] = dataclasses.replace(
            o, label=d.label, confidence=conf, evidence=f"{why}; {o.evidence}"[:160]
        )
        out[j] = dataclasses.replace(
            d, label=o.label, confidence=conf, evidence=f"{why}; {d.evidence}"[:160]
        )
        log.info("naming.swapped", a=d.label, b=o.label)
    return out


def _cap_mixed(d: NameDecision, mixed: dict[str, str]) -> NameDecision:
    if d.label not in mixed:
        return d
    return dataclasses.replace(
        d,
        confidence=min(d.confidence, MIXED_MAX_CONFIDENCE),
        suggest_only=True,
        evidence=f"two voices on this label ({mixed[d.label]}); {d.evidence}"[:200],
    )


def _same_person(a: NameDecision, b: NameDecision) -> bool:
    if a.email and b.email:
        return a.email.lower() == b.email.lower()
    return fold(a.name) == fold(b.name)


def orient_merge(m: MergeDecision, ctx: NamingContext) -> MergeDecision | None:
    """Merge into the speaker that should survive (manual name, then more speech)."""
    views = ctx.by_label
    a, b = views.get(m.label), views.get(m.into)
    if a is None or b is None or a.merged_into or b.merged_into:
        return None
    if a.locked and b.locked:
        return None
    if a.locked or (not b.locked and a.speech_seconds > b.speech_seconds):
        a, b = b, a
    return MergeDecision(label=a.label, into=b.label, confidence=m.confidence, reason=m.reason)


def combine(
    ctx: NamingContext,
    deterministic: Sequence[NameDecision],
    llm_names: Sequence[NameDecision],
    merges: Sequence[MergeDecision],
    *,
    min_confidence: float,
    suggest_confidence: float,
) -> tuple[list[NameDecision], list[MergeDecision]]:
    """One decision per label; same-person collisions become merges."""
    views = ctx.by_label
    best: dict[str, NameDecision] = {}
    for d in [*deterministic, *llm_names]:
        sp = views.get(d.label)
        if sp is None or sp.locked or sp.merged_into:
            continue
        cur = best.get(d.label)
        if cur is None:
            best[d.label] = d
        elif _same_person(cur, d):
            # Agreement: keep the directory identity and the higher confidence.
            winner = cur if (cur.email and not d.email) else d
            best[d.label] = NameDecision(
                label=d.label,
                name=winner.name,
                email=winner.email or cur.email or d.email,
                confidence=round(min(0.99, max(cur.confidence, d.confidence) + 0.03), 3),
                evidence=cur.evidence if cur.source == "self_intro" else d.evidence,
                source="self_intro" if "self_intro" in (cur.source, d.source) else "inferred",
            )
        elif d.confidence > cur.confidence:
            best[d.label] = d
    # Locked/named speakers' identities, for collision checks.
    held: dict[str, NameDecision] = {}
    for sp in ctx.active():
        if sp.named and sp.label not in best:
            held[sp.label] = NameDecision(
                label=sp.label,
                name=sp.display_name,
                email=sp.person_email,
                confidence=1.0 if sp.locked else (sp.confidence or 0.8),
                evidence="",
                source=sp.source,
            )
    out_merges = [m for m in merges if m.confidence >= MERGE_MIN_CONFIDENCE]
    merged_labels = {m.label for m in out_merges} | {m.into for m in out_merges}
    decisions = sorted(best.values(), key=lambda d: d.confidence, reverse=True)
    final: list[NameDecision] = []
    taken: dict[str, NameDecision] = {}
    for d in [*held.values(), *decisions]:
        key = (d.email or fold(d.name)).lower()
        other = taken.get(key)
        if other is not None and other.label != d.label:
            pair = {d.label, other.label}
            if (
                d.confidence >= SAME_PERSON_MERGE_CONFIDENCE
                and other.confidence >= SAME_PERSON_MERGE_CONFIDENCE
                and not (pair & merged_labels)
            ):
                out_merges.append(
                    MergeDecision(
                        label=d.label,
                        into=other.label,
                        confidence=round(min(d.confidence, other.confidence), 3),
                        reason=f"both identified as {d.name}",
                    )
                )
                merged_labels |= pair
            continue  # never two labels with one name
        taken[key] = d
        if d.label in held:
            continue
        if d.confidence >= min_confidence:
            final.append(d)
        elif d.confidence >= suggest_confidence:
            final.append(
                NameDecision(
                    label=d.label,
                    name=d.name,
                    email=d.email,
                    confidence=d.confidence,
                    evidence=d.evidence,
                    source=d.source,
                    suggest_only=True,
                )
            )
    oriented: list[MergeDecision] = []
    seen: set[str] = set()
    for m in out_merges:
        o = orient_merge(m, ctx)
        if o is None or o.label in seen or o.into in seen:
            continue
        seen |= {o.label}
        oriented.append(o)
    # A label merged away needs no name; its survivor inherits one if unnamed.
    gone = {m.label: m.into for m in oriented}
    named_after = {d.label for d in final if not d.suggest_only} | {
        sp.label for sp in ctx.active() if sp.named
    }
    adjusted: list[NameDecision] = []
    for d in final:
        if d.label in gone:
            into = gone[d.label]
            if not d.suggest_only and into not in named_after and not views[into].locked:
                adjusted.append(
                    NameDecision(
                        label=into,
                        name=d.name,
                        email=d.email,
                        confidence=d.confidence,
                        evidence=d.evidence,
                        source=d.source,
                    )
                )
                named_after.add(into)
            continue
        adjusted.append(d)
    return adjusted, oriented


def run(
    ctx: NamingContext,
    *,
    llm: Callable[[str], dict[str, Any]] | None = None,
    min_confidence: float = 0.7,
    suggest_confidence: float = 0.4,
) -> NamingResult:
    """Cues -> deterministic intros + echo merges (+ LLM pass) -> one decision set."""
    cues = find_cues(ctx)
    deterministic = [d for d in (intro_decision(c, ctx) for c in cues) if d is not None]
    # One label introducing two different people means the ASR gave it someone
    # else's line: leave that label to the LLM (it sees both lines in context).
    people_by_label: dict[str, set[str]] = {}
    for d in deterministic:
        people_by_label.setdefault(d.label, set()).add((d.email or fold(d.name)).lower())
    conflicted = {lab for lab, who in people_by_label.items() if len(who) > 1}
    by_label: dict[str, NameDecision] = {}
    for d in deterministic:
        if d.label in conflicted:
            continue
        prev = by_label.get(d.label)
        if prev is None or d.confidence >= prev.confidence:
            by_label[d.label] = d
    deterministic = list(by_label.values())
    merges = [*echo_merges(ctx), *intro_merges(cues, deterministic, ctx)]
    # Address -> reply: a name said to someone pins the person who answers. Two or
    # more events for another person mean the label's intro was probably borrowed
    # (or misheard): that intro loses 0.1.
    addresses = find_addresses(ctx)
    addressed = address_decisions(ctx, addresses)
    mixed = mixed_labels(ctx, cues, addresses)
    strong_addr = {d.label: d for d in addressed if d.confidence >= ADDRESS_OVERRIDE_CONFIDENCE}
    deterministic = [
        dataclasses.replace(d, confidence=round(d.confidence - 0.1, 3))
        if d.label in strong_addr and not _same_person(d, strong_addr[d.label])
        else d
        for d in deterministic
    ] + addressed
    llm_names: list[NameDecision] = []
    used_llm, llm_error = False, None
    needs_llm = llm is not None and any(
        not sp.locked and sp.speech_seconds >= 2.0 for sp in ctx.active()
    )
    if needs_llm:
        try:
            raw = llm(  # type: ignore[misc]
                build_prompt(ctx, cues, mixed=mixed, ruled_out=not_names(ctx))
            )
            intro_labels = {c.label for c in cues if c.kind == "intro"}
            llm_names, llm_merges = parse_llm(raw, ctx, intro_labels=intro_labels)
            llm_names = swap_ruled_out(llm_names, not_names(ctx), mixed)
            merges = [*merges, *llm_merges]
            used_llm = True
        except Exception as exc:  # noqa: BLE001 — the deterministic result still stands
            llm_error = str(exc)[:200]
            log.info("naming.llm_failed", error=llm_error)
    names, merges = combine(
        ctx,
        deterministic,
        llm_names,
        merges,
        min_confidence=min_confidence,
        suggest_confidence=suggest_confidence,
    )
    names = [_cap_mixed(d, mixed) for d in names]
    # A speaker who says "Alice and I" is not Alice: drop inferred names that say so,
    # and unpin an automatic name already showing it.
    said = not_names(ctx)
    kept: list[NameDecision] = []
    for d in names:
        line = _contradicts(d.name, said.get(d.label, {}))
        if line is not None and d.source != "self_intro":
            log.info("naming.contradicted", label=d.label, name=d.name, line=line)
            continue
        kept.append(d)
    names = kept
    for sp in ctx.active():
        line = _contradicts(sp.display_name, said.get(sp.label, {}))
        if (
            line is not None
            and sp.source == "inferred"
            and not sp.locked
            and not any(d.label == sp.label and not d.suggest_only for d in names)
        ):
            mixed.setdefault(sp.label, f"says {line.strip()!r} about someone else")
    return NamingResult(names, merges, cues, used_llm=used_llm, llm_error=llm_error, mixed=mixed)


def should_apply(decision: NameDecision, sp: SpeakerView) -> bool:
    """Whether a decision changes what the speaker shows (never over a manual name)."""
    if sp.locked or sp.merged_into:
        return False
    if decision.suggest_only:
        return not sp.named or (sp.confidence or 0.0) < decision.confidence
    if not sp.named:
        return True
    same = fold(sp.display_name) == fold(decision.name)
    if same:
        # Same name: only a directory identity or a clearly higher confidence is news.
        return bool(decision.email and decision.email != sp.person_email) or (
            decision.confidence >= (sp.confidence or 0.0) + 0.05
        )
    # A different name: an upgrade of the same person ("Alis" -> "Alice Example"),
    # or a better-supported name.
    if decision.email and not sp.person_email:
        heard = Person(email=decision.email, name=decision.name)
        if name_similarity(sp.display_name, heard) >= 0.8:
            return True
    return decision.confidence > (sp.confidence or 0.0) + 0.05
