"""Local user directory and ASR-robust name matching.

The default directory reads locally signed-in users only. Operators can
supply an independent adapter using `set_directory()`.
* `match_name` scores every person against the spoken name with exact,
  first-name-only, nickname, phonetic (Metaphone / NYSIIS / Soundex),
  Jaro-Winkler and "glued consonant" signals, then adds priors for the
  meeting's attendees, the recorder and the recorder's colleagues (same team,
  same manager, same department, people named in their recent sessions).
  `Candidate.score` is comparable across people; callers decide with the top
  score and its margin over the runner-up, and ask the LLM to pick when it is
  ambiguous (`app.live.naming`).

Pure functions apart from `Directory.refresh`; unit-tested in
`tests/test_live_people.py`.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import jellyfish
import structlog

log = structlog.get_logger(__name__)

MAX_PAGES = 50
PAGE_SIZE = 100
FAILURE_BACKOFF_S = 60.0

# Prior bonuses added to the name-similarity score (0..1).
PRIOR_ATTENDEE = 0.15
PRIOR_OWNER = 0.10
PRIOR_TEAM = 0.10
PRIOR_MANAGER_LINK = 0.06  # same manager, or manager <-> report
PRIOR_RECENT = 0.06  # named in one of the recorder's recent sessions
PRIOR_DEPARTMENT = 0.03

# A pick is "confident" when the top score clears this and leads the next
# person by at least `CONFIDENT_MARGIN`.
CONFIDENT_SCORE = 0.85
CONFIDENT_MARGIN = 0.1
# Below this, the spoken word is not treated as that person's name at all.
MIN_CANDIDATE_SCORE = 0.55

# Generic nickname examples only; no employer-specific roster is bundled.
_NICKNAME_GROUPS: tuple[frozenset[str], ...] = tuple(
    frozenset(g.split())
    for g in (
        "alice alicia",
        "bob robert rob",
        "carol caroline",
        "dan daniel",
        "sam samuel samantha",
    )
)
_NICKNAMES: dict[str, frozenset[str]] = {}
for _group in _NICKNAME_GROUPS:
    for _name in _group:
        _NICKNAMES[_name] = _NICKNAMES.get(_name, frozenset()) | _group

# Words that are never a person's name on their own (cue scanning only).
_COMMON_WORDS = frozenset(
    """
    will bill mark may june rich chip sean grant buck green gil lang lee scott hall
    turner potter walker smith brown king young price cash rose summer page bush
    art drew chase sky river reed don ray jack frank bob pat sue joy hope faith
    """.split()
)
_HONORIFICS = frozenset("mr mrs ms miss dr doctor sir madam mister".split())


def is_common_word(word: str) -> bool:
    """A word that is never a first name on its own ("Like", "Will", "Thanks")."""
    f = fold(word)
    return f in _NOT_NAME_WORDS or f in _COMMON_WORDS


def fold(text: str) -> str:
    """Lower-case ASCII with only letters, spaces and hyphens ("Zoë" -> "zoe")."""
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    plain = plain.replace("’", "'").replace("'", "")
    plain = re.sub(r"[^A-Za-z\- ]+", " ", plain).replace("-", " ")
    return " ".join(plain.lower().split())


def display_name(raw: str) -> str:
    """Tidy a directory name for display."""
    out = []
    for tok in (raw or "").split():
        if re.fullmatch(r"[A-Z]{2}[a-z]{2,}", tok):
            tok = tok[0] + tok[1:].lower()
        out.append(tok)
    return " ".join(out)


@dataclass(frozen=True)
class Person:
    email: str
    name: str
    title: str | None = None
    department: str | None = None
    team: str | None = None
    manager_email: str | None = None
    source: str = "local"  # local | attendee

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(fold(self.name).split())

    @property
    def first(self) -> str:
        toks = self.tokens
        return toks[0] if toks else ""

    @property
    def last(self) -> str:
        toks = self.tokens
        return toks[-1] if len(toks) > 1 else ""

    def describe(self) -> str:
        bits = [b for b in (self.title, self.team or self.department) if b]
        return f"{self.name} — {', '.join(bits)}" if bits else self.name

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "email": self.email,
            "title": self.title,
            "department": self.department,
            "team": self.team,
        }


@dataclass(frozen=True)
class Candidate:
    person: Person
    score: float  # similarity + priors; comparable across people
    similarity: float  # name similarity alone (0..1)
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Priors:
    """Who is likely in this conversation (all emails lower-cased)."""

    owner_email: str | None = None
    attendee_emails: frozenset[str] = frozenset()
    recent_emails: frozenset[str] = frozenset()


# ---- similarity ------------------------------------------------------------------


def _jw(a: str, b: str) -> float:
    return float(jellyfish.jaro_winkler_similarity(a, b)) if a and b else 0.0


def token_similarity(spoken: str, actual: str) -> float:
    """How likely `spoken` (as transcribed) is `actual` (folded tokens), 0..1."""
    if not spoken or not actual:
        return 0.0
    if spoken == actual:
        return 1.0
    if actual in _NICKNAMES.get(spoken, ()):
        return 0.95
    jw = _jw(spoken, actual)
    score = jw if jw >= 0.8 else jw * 0.75
    if len(spoken) >= 2 and len(actual) >= 2:
        mp_s, mp_a = jellyfish.metaphone(spoken), jellyfish.metaphone(actual)
        # Equal codes on short, otherwise unlike words ("cat"/"cut") prove little.
        alike = jw >= 0.7 or min(len(spoken), len(actual)) >= 5
        if mp_s and mp_s == mp_a:
            score = max(score, 0.9 if alike else 0.8)
        elif jellyfish.nysiis(spoken) == jellyfish.nysiis(actual):
            score = max(score, 0.88 if alike else 0.78)
        elif (
            mp_s
            and mp_a
            and min(len(mp_s), len(mp_a)) >= 2
            and jellyfish.levenshtein_distance(mp_s, mp_a) == 1
        ):
            score = max(score, 0.72 + 0.1 * (jw >= 0.75))
        elif jellyfish.soundex(spoken) == jellyfish.soundex(actual):
            score = max(score, 0.7)
    # A consonant glued on (or dropped) at a word boundary: "dana" ~ "ana",
    # "belle" ~ "elle".
    if len(spoken) >= 3 and len(actual) >= 3:
        if spoken[1:] == actual or spoken == actual[1:]:
            score = max(score, 0.88)
        else:
            glued = max(_jw(spoken[1:], actual), _jw(spoken, actual[1:]))
            if glued >= 0.85:
                score = max(score, 0.8)
    return min(score, 1.0)


def first_name_similarity(spoken: str, person: Person) -> float:
    """A spoken (folded) token against the person's first name or its nicknames.

    Fuzzy matching runs against the real first name only; a nickname has to be
    heard (nearly) exactly, so "carlin" never reaches Chip via "charlie".
    """
    first = person.first
    if not first or not spoken:
        return 0.0
    best = token_similarity(spoken, first)
    for variant in _NICKNAMES.get(first, frozenset()) - {first}:
        if spoken == variant:
            best = max(best, 0.95)
        else:
            near = _jw(spoken, variant)
            if near >= 0.92:
                best = max(best, 0.85 * near)
    return best


def name_similarity(spoken: str, person: Person) -> float:
    """Similarity of a spoken name (1-3 words) to one person's name."""
    said = [t for t in fold(spoken).split() if t not in _HONORIFICS]
    if not said or not person.first:
        return 0.0
    said = said[:3]
    first_sim = first_name_similarity(said[0], person)
    if len(said) == 1:
        last_only = token_similarity(said[0], person.last) * 0.85 if person.last else 0.0
        return max(first_sim, last_only)
    last_sim = token_similarity(said[-1], person.last) if person.last else 0.0
    if not person.last:
        return first_sim * 0.9
    both = 0.55 * first_sim + 0.45 * last_sim
    whole = _jw(" ".join(said), " ".join(person.tokens))
    # A clear first name with a garbled surname still says a lot.
    return max(both, 0.85 * first_sim, whole if whole >= 0.9 else 0.0)


def _prior(person: Person, priors: Priors, owner: Person | None) -> tuple[float, list[str]]:
    email = person.email.lower()
    bonus, why = 0.0, []
    if email in priors.attendee_emails:
        bonus += PRIOR_ATTENDEE
        why.append("attendee")
    if owner is not None:
        if email == owner.email.lower():
            bonus += PRIOR_OWNER
            why.append("recorder")
        elif owner.team and person.team == owner.team:
            bonus += PRIOR_TEAM
            why.append("same team")
        elif (
            (owner.manager_email and person.manager_email == owner.manager_email)
            or (owner.manager_email and email == owner.manager_email.lower())
            or (person.manager_email and person.manager_email.lower() == owner.email.lower())
        ):
            bonus += PRIOR_MANAGER_LINK
            why.append("same manager")
        elif owner.department and person.department == owner.department:
            bonus += PRIOR_DEPARTMENT
            why.append("same department")
    if email in priors.recent_emails and email != (priors.owner_email or ""):
        bonus += PRIOR_RECENT
        why.append("recent sessions")
    return bonus, why


def match_name(
    spoken: str,
    people: Sequence[Person],
    priors: Priors | None = None,
    *,
    limit: int = 5,
    min_similarity: float = MIN_CANDIDATE_SCORE,
) -> list[Candidate]:
    """Rank `people` for a spoken name, best first (empty when nothing is close)."""
    priors = priors or Priors()
    owner = _by_email(people, priors.owner_email)
    out: list[Candidate] = []
    for person in people:
        sim = name_similarity(spoken, person)
        if sim < min_similarity:
            continue
        bonus, why = _prior(person, priors, owner)
        out.append(Candidate(person, round(sim + bonus, 4), round(sim, 4), tuple(why)))
    out.sort(key=lambda c: (c.score, c.similarity), reverse=True)
    return out[:limit]


def confident_pick(candidates: Sequence[Candidate]) -> Candidate | None:
    """The top candidate when it clearly wins, else None (ask the LLM)."""
    if not candidates:
        return None
    top = candidates[0]
    runner = candidates[1].score if len(candidates) > 1 else 0.0
    if (
        top.similarity >= 0.8
        and top.score >= CONFIDENT_SCORE
        and top.score - runner >= CONFIDENT_MARGIN
    ):
        return top
    return None


def _by_email(people: Iterable[Person], email: str | None) -> Person | None:
    if not email:
        return None
    key = email.lower()
    return next((p for p in people if p.email.lower() == key), None)


# ---- name mentions in free text ---------------------------------------------------

# A capitalised word, peeking (without consuming) at the word after it.
_CAP_TOKEN = re.compile(
    r"\b([A-Z][a-zA-Z'’\-]{1,20})\b(?=(?:[,.]?\s+([A-Za-z][a-zA-Z'’\-]{1,20}))?)"
)


def name_mentions(
    text: str,
    people: Sequence[Person],
    priors: Priors | None = None,
    *,
    min_similarity: float = 0.85,
    sentence_start_similarity: float = 0.95,
    limit: int = 3,
) -> list[tuple[str, list[Candidate]]]:
    """Capitalised words in `text` that sound like someone's name.

    Returns (spoken words, best candidates). Used to notice "thanks Alice",
    "Carol, can you…", "I'm here with Alice Example" without an LLM call; the
    naming pass then decides who is who. Common words ("Will", "Mark") only
    count with a matching surname next to them.
    """
    found: list[tuple[str, list[Candidate]]] = []
    seen: set[str] = set()
    for m in _CAP_TOKEN.finditer(text or ""):
        first, nxt = m.group(1), m.group(2)
        f = fold(first)
        if len(f) < 3 or f in _NOT_NAME_WORDS:
            continue
        # A capital at the start of a sentence says nothing: demand a near-exact name.
        before = (text or "")[: m.start()].rstrip()
        floor = sentence_start_similarity if not before or before[-1] in ".!?" else min_similarity
        spoken = first
        cands: list[Candidate] = []
        if nxt and fold(nxt) not in _NOT_NAME_WORDS:
            pair = f"{first} {nxt}"
            cands = [
                c
                for c in match_name(pair, people, priors, limit=limit)
                if c.similarity >= max(min_similarity, floor - 0.05)
                and c.person.last
                and token_similarity(fold(nxt), c.person.last) >= 0.8
            ]
            if cands:
                spoken = pair
        if not cands:
            if f in _COMMON_WORDS:
                continue
            cands = [
                c for c in match_name(first, people, priors, limit=limit) if c.similarity >= floor
            ]
        if cands and spoken.casefold() not in seen:
            seen.add(spoken.casefold())
            found.append((spoken, cands))
    return found


# Capitalised words that start sentences or are jargon, never a first name.
_NOT_NAME_WORDS = frozenset(
    """
    the and but so yes yeah yep okay well what when where why how who which this that
    these those there here then than they them their our you your his her she him its
    can could would should will shall may might must did does done not now just also
    hello thanks thank sorry please great good nice cool right sure maybe actually
    let lets let's like look see wait hmm uh um oh hey hi bye one two three four five
    monday tuesday wednesday thursday friday saturday sunday january february march april
    june july august september october november december google zoom slack
    nemotron openai jev rgs api llm ai speaker
    although instead have has had is are was were do does it in on at for with from by to
    of as if once after before because since until while every each all some any no
    never always sometimes also still even only really very much more most less first
    next last again later today tomorrow yesterday meanwhile however therefore otherwise
    besides anyway basically honestly literally obviously apparently hopefully probably
    maybe perhaps sure fine true false wow whoa huh nah nope yup boom interesting exactly
    definitely totally absolutely yesterday tonight morning afternoon evening night
    """.split()
)


# ---- the directory -------------------------------------------------------------------


def _str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _ref_name(value: Any) -> str | None:
    return _str(value.get("name")) if isinstance(value, dict) else _str(value)


def parse_people(payload: Any) -> list[Person]:
    """Generic directory response (list or `{people|items|data|results: [...]}`)."""
    rows: Any = payload
    if isinstance(payload, dict):
        for key in ("people", "items", "data", "results"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list):
        return []
    out: list[Person] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        email, name = _str(row.get("email")), _str(row.get("name"))
        status = (_str(row.get("status")) or "active").lower()
        if not email or not name or status not in ("active", "onboarding"):
            continue
        manager = row.get("manager") if isinstance(row.get("manager"), dict) else {}
        out.append(
            Person(
                email=email.lower(),
                name=display_name(name),
                title=_str(row.get("title")),
                department=_ref_name(row.get("department")),
                team=_ref_name(row.get("team")),
                manager_email=(_str(manager.get("email")) or "").lower() or None,
            )
        )
    return out


@dataclass
class Directory:
    """Cached roster. `people()` blocks on the first load (call it off the event loop)."""

    base_url: str = ""
    api_key: str = ""
    ttl_s: float = 3600.0
    timeout_s: float = 10.0
    fallback: Callable[[], list[Person]] | None = None
    clock: Callable[[], float] = time.monotonic
    _people: list[Person] = field(default_factory=list)
    _loaded_at: float | None = None
    _failed_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip() and self.base_url.strip())

    def people(self) -> list[Person]:
        now = self.clock()
        fresh = self._loaded_at is not None and now - self._loaded_at < self.ttl_s
        backing_off = self._failed_at is not None and now - self._failed_at < FAILURE_BACKOFF_S
        if fresh or backing_off or not self.configured:
            return self._people or self._fallback()
        with self._lock:
            if self._loaded_at is not None and self.clock() - self._loaded_at < self.ttl_s:
                return self._people
            try:
                people = self.fetch()
            except Exception as exc:  # noqa: BLE001 — keep the stale roster
                self._failed_at = self.clock()
                log.warning(
                    "people.directory_failed", error=str(exc)[:200], stale=len(self._people)
                )
                return self._people or self._fallback()
            if people:
                self._people, self._loaded_at, self._failed_at = people, self.clock(), None
                log.info("people.directory_loaded", count=len(people))
            else:
                self._failed_at = self.clock()
        return self._people or self._fallback()

    def _fallback(self) -> list[Person]:
        if self.fallback is None:
            return []
        try:
            return self.fallback()
        except Exception as exc:  # noqa: BLE001
            log.info("people.fallback_failed", error=str(exc)[:200])
            return []

    def fetch(self) -> list[Person]:
        """No company-directory integration in the standalone core.

        A deployment may subclass Directory and inject its own fetch method.
        """
        return self._fallback()

    def by_email(self, email: str | None) -> Person | None:
        return _by_email(self.people(), email)


def rgs_users_fallback() -> list[Person]:
    """People who have signed in locally; no upstream directory needed."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models.user import User

    with SessionLocal() as db:
        rows = db.execute(select(User.email, User.name)).all()
    return [
        Person(email=email.lower(), name=display_name(name), source="rgs")
        for email, name in rows
        if email and name and " " in name.strip()
    ]


_directory: Directory | None = None
_directory_lock = threading.Lock()


def get_directory() -> Directory:
    global _directory
    if _directory is None:
        with _directory_lock:
            if _directory is None:
                _directory = Directory(fallback=rgs_users_fallback)
    return _directory


def set_directory(directory: Directory | None) -> None:
    """Tests: swap the process-wide directory."""
    global _directory
    _directory = directory


def colleagues(people: Sequence[Person], owner: Person | None, *, limit: int = 25) -> list[Person]:
    """The recorder's likely co-speakers: team first, then manager links, then department."""
    if owner is None:
        return []
    ranked: list[tuple[int, str, Person]] = []
    for p in people:
        if p.email == owner.email:
            continue
        if owner.team and p.team == owner.team:
            tier = 0
        elif owner.manager_email and (
            p.manager_email == owner.manager_email
            or p.email == owner.manager_email
            or p.manager_email == owner.email
        ):
            tier = 1
        elif owner.department and p.department == owner.department:
            tier = 2
        else:
            continue
        ranked.append((tier, p.name, p))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [p for _t, _n, p in ranked[:limit]]


def attendee_people(attendees: Sequence[dict[str, Any]], people: Sequence[Person]) -> list[Person]:
    """Session attendees as people (directory entry when known, else as given)."""
    out: list[Person] = []
    for a in attendees:
        email = (a.get("email") or "").strip().lower() or None
        name = (a.get("name") or "").strip() or None
        hit = _by_email(people, email) if email else None
        if hit is None and name:
            exact = [p for p in people if fold(p.name) == fold(name)]
            hit = exact[0] if len(exact) == 1 else None
        if hit is not None:
            out.append(hit)
        elif name or email:
            out.append(
                Person(
                    email=email or "",
                    name=name or (email or "").split("@")[0],
                    source="attendee",
                )
            )
    return out
