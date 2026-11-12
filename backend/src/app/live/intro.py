"""Self-introduction detection for Live Record (v2).

When someone says "Hi, I'm Alice" or "This is Bob from the demo team", their speaker
label ("Speaker 2") should become their name while the meeting is still
going. Two stages, provider-agnostic:

1. `detect_intro(text)` — a cheap regex pre-filter over each final segment.
   It only looks at introduction phrasings at the start of a sentence (after
   optional greetings/fillers): "my name is X", "I'm X", "I am X",
   "this is X", "it's X here", "X here". The name must look like a name
   (capitalised, not a common word). Phrasings that are unambiguous on their
   own ("my name is", a greeting before "I'm", "this is X from Y",
   "X here") are *strong*; the rest are *weak*.
2. Strong candidates are accepted deterministically (confidence 0.9, 0.95
   when the name matches a meeting attendee). Weak ones are confirmed with ONE
   yes/no question on the session's gate provider (`gate.confirm_intro`) and
   accepted at P(yes) >= 0.8. A weak candidate whose first name matches
   exactly one attendee counts as strong.

`resolve_name(spoken, attendees)` prefers the attendee's full name when the
spoken first name (or full name) matches exactly one attendee; otherwise the
spoken name is used as said. Manual renames always win: the runtime only
considers labels still showing their default name, and
`service.apply_self_intro` re-checks under a row lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INTRO_MIN_CONFIDENCE = 0.8
STRONG_CONFIDENCE = 0.9
ATTENDEE_CONFIDENCE = 0.95

_NAME_TOKEN = r"[A-Z][a-z]+(?:[-'’][A-Z]?[a-z]+)?|[A-Z][a-z]*[A-Z][a-z]+"
_NAME = rf"(?P<name>(?:{_NAME_TOKEN})(?:\s+(?:{_NAME_TOKEN})){{0,2}})"
# Case-insensitive pieces are scoped with (?i:...): the NAME must stay capitalised
# (ASR output is cased), which is what tells "I'm Alice" from "I'm going".
_GREETING = (
    r"(?i:hi|hello|hey|hiya|good\s+(?:morning|afternoon|evening))"
    r"(?i:\s+(?:everyone|everybody|all|guys|folks|team|there|y'all))?"
)
_FILLER = r"(?i:so|um+|uh+|okay|ok|yeah|yes|well|and|alright|sorry|oh|right)"
# Clause start: beginning of text or after . ! ? ; or a comma ("Thanks, so I'm Alice").
_START = r"(?:^|(?<=[.!?;,])\s+)"
# "Alice here" only as a whole clause ("Alice here, can you hear me?"), never
# "Alice here has the numbers".
_CLAUSE_END = r"(?=\s*(?:[.,!?;]|$))"
_PREFIX = rf"(?P<greet>{_GREETING}[\s,.!-]+)?(?:{_FILLER}[\s,]+){{0,2}}"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # "my name is Alice" / "my name's Alice Example" (anywhere in a sentence).
    (re.compile(rf"\b(?i:my\s+name(?:'s|’s|\s+is))\s+{_NAME}"), "name_is"),
    # "it's Alice here" / "Alice here" (start of sentence).
    (
        re.compile(
            rf"{_START}{_PREFIX}(?i:it's\s+|it’s\s+|it\s+is\s+)?{_NAME}\s+here{_CLAUSE_END}"
        ),
        "here",
    ),
    # "Hi, I'm Alice" / "I am Alice from docs" / "This is Bob from the demo team".
    (
        re.compile(
            rf"{_START}{_PREFIX}(?P<verb>(?i:i'm|i’m|i\s+am|this\s+is))\s+{_NAME}"
            rf"(?P<tail>\s+(?i:from|with|on|in|at)\s+[^\s,.!?;]+|\s+here{_CLAUSE_END})?"
        ),
        "intro",
    ),
)

# Capitalised words that follow "I'm" / "this is" but are not names.
_NOT_NAMES = frozenset(
    """
    a an the it its this that these those there here what who where when why how which
    not just going gonna trying still also actually really very so too sorry glad happy
    good great fine okay ok sure back done ready new late early muted unmuted here now
    all everyone everybody nobody someone somebody anyone team guys folks speaker
    in on at from with and but or of for to by up out over about like kind sort maybe
    probably definitely totally literally basically honestly hoping thinking looking
    calling joining dialing talking hearing seeing getting having doing making taking
    monday tuesday wednesday thursday friday saturday sunday today tomorrow yesterday
    january february march april may june july august september october november december
    google meet zoom slack teams microsoft amazon aws openai jev nemotron
    hi hello hey yes yeah no nope well um uh oh right alright thanks thank please
    mister miss mrs mr ms dr doctor sir madam
    i i'm im we you he she they me my our your his her their
    one two three four five six seven eight nine ten first second last next
    """.split()
)


@dataclass(frozen=True)
class IntroMatch:
    name: str  # as spoken, e.g. "Alice" or "Alice Example"
    evidence: str  # the phrase that matched, e.g. "Hi, I'm Alice"
    strong: bool


def _clean_name(raw: str) -> str | None:
    tokens = raw.split()
    out: list[str] = []
    for tok in tokens:
        bare = tok.strip(".,!?;:'’").replace("’", "'")
        if bare.casefold().endswith("'s"):
            # "I'm Alice's manager" is about Alice, not a self-introduction.
            return None
        if not bare or bare.casefold() in _NOT_NAMES:
            break
        out.append(bare)
    if not out or len(out[0]) < 2:
        return None
    return " ".join(out)


def detect_intro(text: str) -> IntroMatch | None:
    """Find a self-introduction in one final segment's text (or None)."""
    flat = " ".join((text or "").split())
    if not flat or len(flat) > 600:
        return None
    for pattern, kind in _PATTERNS:
        for m in pattern.finditer(flat):
            name = _clean_name(m.group("name"))
            if name is None:
                continue
            groups = m.groupdict()
            if kind == "name_is":
                strong = True
            elif kind == "here":
                strong = True
            else:
                verb = (groups.get("verb") or "").casefold()
                strong = bool(groups.get("greet")) or bool(groups.get("tail"))
                if not strong and verb.startswith("i"):
                    # "I'm Alice." as a whole short utterance is unambiguous.
                    rest = flat[m.end() :].strip(" .!,")
                    strong = not rest and len(flat.split()) <= 5
            evidence = flat[m.start() : m.end()].strip(" ,.;")
            return IntroMatch(name=name, evidence=evidence[:160], strong=strong)
    return None


def attendee_full_name(raw: str) -> str:
    """Display name for an attendee given as a name or an email address."""
    value = (raw or "").strip()
    if "@" in value:
        local = value.split("@", 1)[0]
        parts = [p for p in re.split(r"[._\-+]+", local) if p and not p.isdigit()]
        return " ".join(p.capitalize() for p in parts)
    return " ".join(value.split())


@dataclass(frozen=True)
class ResolvedName:
    name: str
    from_attendee: bool


def resolve_name(spoken: str, attendees: list[str] | tuple[str, ...]) -> ResolvedName:
    """Prefer the one attendee whose full (or first) name matches what was said."""
    said = spoken.casefold().split()
    if not said:
        return ResolvedName(spoken, False)
    full_names = [n for n in (attendee_full_name(a) for a in attendees) if n]
    exact = [n for n in full_names if n.casefold().split() == said]
    if len(exact) == 1:
        return ResolvedName(exact[0], True)
    if len(said) == 1:
        first = [n for n in full_names if n.casefold().split()[0] == said[0]]
        unique = {n.casefold(): n for n in first}
        if len(unique) == 1:
            return ResolvedName(next(iter(unique.values())), True)
    else:
        # "Alice E." / "Alice Ex..." said, attendee "Alice Example": first + last initial.
        prefix = [
            n
            for n in full_names
            if len(n.split()) >= 2
            and n.casefold().split()[0] == said[0]
            and n.casefold().split()[-1].startswith(said[-1].rstrip("."))
        ]
        if len(prefix) == 1:
            return ResolvedName(prefix[0], True)
    return ResolvedName(
        " ".join(w if w[:1].isupper() else w.capitalize() for w in spoken.split()), False
    )


def is_default_name(label: str, display_name: str | None) -> bool:
    return (display_name or "") == f"Speaker {label.lstrip('S')}"
