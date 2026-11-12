"""Optional on-device words (Copper browser Phonon-2 only) merged with Nemotron speakers.

Inside the Copper browser, RGS's page hands the same microphone audio it
uploads to Copper's on-device speech-to-text and posts each finished phrase
here: its words, each with a start and end on the session timeline. Nemotron
keeps doing what it is best at here — who spoke when — and its own words stay
the fallback.

The merge is by time and nothing else:

* every on-device word takes the speaker of the Nemotron segment it overlaps
  most; a word that overlaps none takes the nearest one within `NEAR_S`, and
  failing that the word before it's;
* consecutive words of one speaker within one phrase make a segment;
* where no on-device phrase covers the time (the browser wasn't sending yet,
  or dropped some), Nemotron's own words stay in (spread evenly over their
  segment, since segments keep no word times).

Integration: README.md (Configuration / Transcription).
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.live_ondevice import LiveOnDeviceSegment

ENGINE_DEFAULT = "phonon-2"
# A word that overlaps no Nemotron segment takes the nearest one this close.
NEAR_S = 1.5
# Bounds on what one POST may carry.
MAX_SEGMENTS = 200
MAX_WORDS = 400


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Phrase:
    """One on-device phrase (a Phonon final)."""

    seq: int
    start: float
    end: float
    text: str
    words: tuple[Word, ...]


@dataclass(frozen=True)
class Turn:
    """Nemotron's evidence: `speaker` talked from `start` to `end` (with `text`)."""

    speaker: str
    start: float
    end: float
    text: str = ""
    seq: int = 0


@dataclass
class Merged:
    speaker: str | None
    start: float
    end: float
    text: str
    source: str  # "ondevice" | "nemotron"
    words: list[dict[str, Any]] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        return {
            "speaker": self.speaker,
            "start_s": round(self.start, 3),
            "end_s": round(self.end, 3),
            "text": self.text,
            "source": self.source,
        }


# ---- the merge ------------------------------------------------------------------------


class _Index:
    """Turns sorted by start, for overlap lookups near a time."""

    def __init__(self, turns: Iterable[Turn]) -> None:
        self.turns = sorted((t for t in turns if t.end >= t.start), key=lambda t: (t.start, t.end))
        self.starts = [t.start for t in self.turns]
        longest = max((t.end - t.start for t in self.turns), default=0.0)
        self.reach = longest + NEAR_S

    def near(self, start: float, end: float) -> Sequence[Turn]:
        lo = bisect.bisect_left(self.starts, start - self.reach)
        hi = bisect.bisect_right(self.starts, end + NEAR_S)
        return self.turns[lo:hi]


def speaker_of(word: Word, index: _Index, before: str | None) -> str | None:
    """The speaker of the turn `word` overlaps most, else the nearest, else `before`."""
    best: Turn | None = None
    best_overlap = 0.0
    nearest: Turn | None = None
    nearest_gap = NEAR_S
    for turn in index.near(word.start, word.end):
        overlap = min(word.end, turn.end) - max(word.start, turn.start)
        if overlap > best_overlap:
            best, best_overlap = turn, overlap
        gap = max(turn.start - word.end, word.start - turn.end, 0.0)
        if gap <= nearest_gap:
            nearest, nearest_gap = turn, gap
    if best is not None and best_overlap > 0:
        return best.speaker
    if nearest is not None:
        return nearest.speaker
    return before


def attribute(phrase: Phrase, index: _Index, before: str | None = None) -> list[Merged]:
    """One phrase's words split into segments by speaker."""
    words = phrase.words or _spread(phrase)
    out: list[Merged] = []
    speaker = before
    for word in words:
        speaker = speaker_of(word, index, speaker)
        wd = {"text": word.text, "start_s": round(word.start, 3), "end_s": round(word.end, 3)}
        if out and out[-1].speaker == speaker:
            cur = out[-1]
            cur.text = f"{cur.text} {word.text}"
            cur.end = max(cur.end, word.end)
            cur.words.append(wd)
        else:
            out.append(Merged(speaker, word.start, word.end, word.text, "ondevice", [wd]))
    if out:
        # The phrase's own bounds frame its first and last part.
        out[0].start = min(out[0].start, phrase.start)
    return out


def _spread(phrase: Phrase) -> tuple[Word, ...]:
    parts = phrase.text.split()
    if not parts:
        return ()
    step = max(phrase.end - phrase.start, 0.01) / len(parts)
    return tuple(
        Word(p, phrase.start + i * step, phrase.start + (i + 1) * step) for i, p in enumerate(parts)
    )


def _inside(t: float, spans: list[tuple[float, float]], starts: list[float]) -> bool:
    i = bisect.bisect_right(starts, t) - 1
    return i >= 0 and spans[i][0] <= t <= spans[i][1]


def _uncovered(turn: Turn, spans: list[tuple[float, float]], starts: list[float]) -> list[Merged]:
    """A Nemotron segment's words (spread over its time) that no on-device phrase covers."""
    words = _spread(Phrase(turn.seq, turn.start, turn.end, turn.text, ()))
    out: list[Merged] = []
    last_kept = False
    for w in words:
        if _inside((w.start + w.end) / 2, spans, starts):
            last_kept = False
            continue
        if out and last_kept:
            cur = out[-1]
            cur.text = f"{cur.text} {w.text}"
            cur.end = w.end
        else:
            out.append(Merged(turn.speaker, w.start, w.end, w.text, "nemotron"))
        last_kept = True
    return out


def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def merge(phrases: Sequence[Phrase], turns: Sequence[Turn]) -> list[Merged]:
    """On-device words with Nemotron's speakers, plus Nemotron's uncovered segments, by time."""
    index = _Index(turns)
    out: list[Merged] = []
    before: str | None = None
    ordered = sorted(phrases, key=lambda p: (p.start, p.seq))
    for phrase in ordered:
        parts = attribute(phrase, index, before)
        if parts:
            before = parts[-1].speaker
        out.extend(parts)
    spans = _union(sorted((p.start, p.end) for p in ordered))
    starts = [a for a, _ in spans]
    for turn in turns:
        if turn.text.strip():
            out.extend(_uncovered(turn, spans, starts))
    # Words heard before Nemotron said anyone spoke take the first speaker after them.
    first = next((m.speaker for m in sorted(out, key=lambda m: m.start) if m.speaker), None)
    for m in out:
        if m.speaker is None:
            m.speaker = first or "S1"
    out.sort(key=lambda m: (m.start, m.end))
    return out


# ---- input -------------------------------------------------------------------------


def parse_phrases(raw: Sequence[dict[str, Any]], *, offset_s: float = 0.0) -> list[Phrase]:
    """The POST body's segments → phrases on the session timeline (bad words dropped)."""
    out: list[Phrase] = []
    for seg in raw[:MAX_SEGMENTS]:
        try:
            seq = int(seg["seq"])
            start = float(seg["start_s"]) + offset_s
            end = float(seg["end_s"]) + offset_s
        except (KeyError, TypeError, ValueError):
            continue
        text = " ".join(str(seg.get("text") or "").split())
        words: list[Word] = []
        for w in (seg.get("words") or [])[:MAX_WORDS]:
            try:
                wt = " ".join(str(w["text"]).split())
                ws = float(w["start_s"]) + offset_s
                we = float(w["end_s"]) + offset_s
            except (KeyError, TypeError, ValueError):
                continue
            if wt and we >= ws >= 0:
                words.append(Word(wt, ws, we))
        if not text:
            text = " ".join(w.text for w in words)
        if text and end >= start >= 0:
            out.append(Phrase(seq, start, end, text, tuple(words)))
    return out


# ---- storage -----------------------------------------------------------------------


def store(db: Session, session_id: UUID, phrases: Sequence[Phrase], engine: str) -> list[Phrase]:
    """Insert phrases not seen before (by the browser's seq); return those. Caller commits."""
    if not phrases:
        return []
    rows = [
        {
            "session_id": session_id,
            "client_seq": p.seq,
            "engine": engine[:32] or ENGINE_DEFAULT,
            "start_seconds": Decimal(f"{p.start:.3f}"),
            "end_seconds": Decimal(f"{p.end:.3f}"),
            "text": p.text,
            "words": [
                {"text": w.text, "start_s": round(w.start, 3), "end_s": round(w.end, 3)}
                for w in p.words
            ],
        }
        for p in phrases
    ]
    stmt = (
        insert(LiveOnDeviceSegment)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_live_ondevice_segments_client_seq")
        .returning(LiveOnDeviceSegment.client_seq)
    )
    fresh = set(db.scalars(stmt).all())
    return [p for p in phrases if p.seq in fresh]


def load(db: Session, session_id: UUID) -> list[Phrase]:
    rows = db.scalars(
        select(LiveOnDeviceSegment)
        .where(LiveOnDeviceSegment.session_id == session_id)
        .order_by(LiveOnDeviceSegment.start_seconds, LiveOnDeviceSegment.client_seq)
    ).all()
    return [
        Phrase(
            r.client_seq,
            float(r.start_seconds),
            float(r.end_seconds),
            r.text,
            tuple(Word(w["text"], float(w["start_s"]), float(w["end_s"])) for w in (r.words or [])),
        )
        for r in rows
    ]


def engine_of(db: Session, session_id: UUID) -> str | None:
    return db.scalar(
        select(LiveOnDeviceSegment.engine)
        .where(LiveOnDeviceSegment.session_id == session_id)
        .limit(1)
    )


def turns_from_rows(rows: Iterable[Any], resolve: Callable[[str], str] = lambda s: s) -> list[Turn]:
    """`LiveSegment` rows (or anything shaped like them) → Nemotron turns."""
    return [
        Turn(resolve(r.speaker_label), float(r.start_seconds), float(r.end_seconds), r.text, r.seq)
        for r in rows
    ]


@dataclass
class _Row:
    """Shaped like `LiveSegment` for `finalize.merge_segments`."""

    seq: int
    speaker_label: str
    start_seconds: float
    end_seconds: float
    text: str


def hybrid_rows(
    db: Session, session_id: UUID, rows: list[Any], resolve: Callable[[str], str]
) -> list[Any]:
    """The final transcript's fragments: `rows` (Nemotron) as-is when no on-device words came."""
    phrases = load(db, session_id)
    if not phrases:
        return rows
    merged = merge(phrases, turns_from_rows(rows, resolve))
    return [_Row(i + 1, m.speaker or "S1", m.start, m.end, m.text) for i, m in enumerate(merged)]


# ---- live --------------------------------------------------------------------------


def live_turns(
    rows: Iterable[Any], labelled: Iterable[Any], resolve: Callable[[str], str]
) -> list[Turn]:
    """Committed segments plus the runtime's latest labelled words (the uncommitted tail)."""
    turns = turns_from_rows(rows, resolve)
    for lab in labelled:
        w = getattr(lab, "word", lab)
        turns.append(Turn(resolve(w.speaker), float(w.start), float(w.end)))
    return turns


def event_data(phrase: Phrase, parts: list[Merged], engine: str) -> dict[str, Any]:
    """`ondevice.segment` event data (the authenticated OpenAPI schema)."""
    return {
        "seq": None,
        "client_seq": phrase.seq,
        "engine": engine,
        "start_s": round(phrase.start, 3),
        "end_s": round(phrase.end, 3),
        "text": phrase.text,
        "parts": [{**m.out(), "words": m.words} for m in parts],
    }
