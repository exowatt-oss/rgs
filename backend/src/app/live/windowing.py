"""Rolling-window commit logic for near-realtime transcription.

Nemotron is an offline model, so live text comes from re-transcribing a
trailing window every few seconds:

    audio:    |---- committed ----|ctx|------ uncommitted -------| head
    window:                       [start ............................ end]

* `plan_window` picks `[start, end]`: `start = committed_until - left_context`,
  capped so the window never exceeds `max_window_s` (a backlog after an
  endpoint outage is worked off oldest-first, "catch-up" mode).
* `Committer.update` takes the window's words (absolute seconds) and returns
  the newly *final* words plus the *interim* tail:
  - the re-sent left context is dropped by *text*: the longest run (>= 2
    words) of the last committed words found in the window marks where new
    speech starts (RGS: word times are estimates when the router's CTC
    alignment is re-derived from diarization, so time alone duplicates or
    drops words at the seam; a context word cut by the window start can even
    be aligned *after* the matched run, so a repeat of the committed words
    just before the run that starts inside committed audio is dropped too);
    without a text match, words whose midpoint is
    at/behind `committed_until` are treated as duplicates and dropped;
  - a prefix of words is committed when it ends at least `stable_horizon_s`
    behind the head **and** the previous hypothesis agreed on it (same
    normalised text, start within `AGREE_TOLERANCE_S`) — LocalAgreement-2;
  - anything at least `force_horizon_s` behind the head is committed even
    without agreement (bounds final-text latency);
  - an agreement-only commit is snapped back to a natural boundary
    (sentence end, pause, speaker change) so finals rarely split a phrase.
* `segment_words` turns committed words into transcript segments.

All functions are pure and unit-tested (`tests/test_live_windowing.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

AGREE_TOLERANCE_S = 0.6
PAUSE_BOUNDARY_S = 0.4
DUPLICATE_EPSILON_S = 0.02
# Text-overlap dedupe: how many committed words to remember, the shortest run
# that counts as a match, and how far past `committed_until` a matched run may
# start (word-time estimation error).
TAIL_WORDS = 12
MIN_OVERLAP_WORDS = 2
OVERLAP_SLACK_S = 4.0
# Committed words right before a matched run that may come back out of order
# (a word cut by the window start, mis-aligned after the run).
RESENT_BEFORE_RUN = 2

_NORM_RE = re.compile(r"[^\w']+", re.UNICODE)


@dataclass(frozen=True)
class TimedWord:
    """A word on the session timeline (absolute seconds)."""

    text: str
    start: float
    end: float
    speaker: str  # global speaker label ("S1") once mapped

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2


@dataclass(frozen=True)
class WindowPlan:
    start: float
    end: float
    catching_up: bool


@dataclass(frozen=True)
class CommitResult:
    final: tuple[TimedWord, ...]
    interim: tuple[TimedWord, ...]
    committed_until: float


@dataclass(frozen=True)
class SegmentDraft:
    speaker: str
    start: float
    end: float
    text: str


def normalise(text: str) -> str:
    return _NORM_RE.sub("", text.lower())


def plan_window(
    committed_until: float,
    audio_end: float,
    *,
    left_context_s: float,
    max_window_s: float,
) -> WindowPlan:
    start = max(0.0, committed_until - left_context_s)
    end = audio_end
    if end - start > max_window_s:
        end = start + max_window_s
    return WindowPlan(start=start, end=end, catching_up=end < audio_end - 1e-6)


def _is_sentence_end(word: TimedWord) -> bool:
    return word.text.rstrip("\"')]}").endswith((".", "?", "!"))


@dataclass
class Committer:
    stable_horizon_s: float = 2.0
    force_horizon_s: float = 6.0
    committed_until: float = 0.0
    _previous: tuple[TimedWord, ...] = field(default_factory=tuple)
    _tail: tuple[str, ...] = field(default_factory=tuple)

    def _text_overlap_end(self, words: list[TimedWord]) -> tuple[int, int] | None:
        """`(index, run length)` of the last re-sent (already committed) word, matched by text.

        Longest run first; among equal runs the one ending nearest
        `committed_until`. None when fewer than `MIN_OVERLAP_WORDS` match.
        """
        tail = [t for t in self._tail if t]
        if len(tail) < MIN_OVERLAP_WORDS or not words:
            return None
        norm = [normalise(w.text) for w in words]
        limit = self.committed_until + OVERLAP_SLACK_S
        for k in range(min(len(tail), TAIL_WORDS), MIN_OVERLAP_WORDS - 1, -1):
            want = tail[-k:]
            best: int | None = None
            for j in range(k - 1, len(words)):
                if words[j - k + 1].start > limit:
                    break
                if norm[j - k + 1 : j + 1] == want:
                    if best is None or abs(words[j].end - self.committed_until) < abs(
                        words[best].end - self.committed_until
                    ):
                        best = j
            if best is not None:
                return best, k
        return None

    def _fresh(self, words: list[TimedWord] | tuple[TimedWord, ...]) -> list[TimedWord]:
        ordered = sorted(words, key=lambda w: (w.start, w.end))
        match = self._text_overlap_end(ordered)
        j = match[0] if match else None
        if (
            j is None
            and ordered
            and self._tail
            and len(self._tail[-1]) >= 3
            and normalise(ordered[0].text) == self._tail[-1]
            and ordered[0].start <= self.committed_until + 1.0
        ):
            # The left context held only the last committed word (cut
            # mid-phrase at the window start): it is a re-send, not new speech.
            j = 0
        if j is None:
            return [w for w in ordered if w.mid > self.committed_until + DUPLICATE_EPSILON_S]
        cu = self.committed_until
        # Committed words just before the matched run were re-sent too (the
        # window starts mid-phrase), but alignment can place such a cut-off
        # word *after* the run (synthetic example: "... done. | Yes. Done.").
        # A word that starts inside committed audio and repeats one of them
        # is that re-send, not new speech.
        run = match[1] if match else 1
        tail = [t for t in self._tail if t]
        resent = list(tail[max(0, len(tail) - run - RESENT_BEFORE_RUN) : len(tail) - run])
        out: list[TimedWord] = []
        for w in ordered[j + 1 :]:
            if w.start < cu:
                norm = normalise(w.text)
                if norm in resent:
                    resent.remove(norm)
                    continue
                w = TimedWord(text=w.text, start=cu, end=max(w.end, cu), speaker=w.speaker)
            out.append(w)
        return out

    def _remember(self, final: tuple[TimedWord, ...]) -> None:
        if final:
            texts = [normalise(w.text) for w in final]
            self._tail = tuple([*self._tail, *(t for t in texts if t)][-TAIL_WORDS:])

    def _agreed_prefix(self, words: list[TimedWord]) -> int:
        n = 0
        for new, old in zip(words, self._previous, strict=False):
            if normalise(new.text) != normalise(old.text):
                break
            if abs(new.start - old.start) > AGREE_TOLERANCE_S:
                break
            n += 1
        return n

    def update(
        self,
        words: list[TimedWord] | tuple[TimedWord, ...],
        *,
        head: float,
        catching_up: bool = False,
        flush: bool = False,
    ) -> CommitResult:
        """Fold one window hypothesis into the committed transcript.

        `head` is the end of the transcribed audio (the window end).
        `catching_up` (window ends before the live head) commits everything
        older than the stable horizon without waiting for agreement.
        `flush` (session stop) commits every remaining word.
        """
        fresh = self._fresh(words)
        if flush:
            final = tuple(fresh)
            self._remember(final)
            if final:
                self.committed_until = max(self.committed_until, final[-1].end)
            self.committed_until = max(self.committed_until, head)
            self._previous = ()
            return CommitResult(final=final, interim=(), committed_until=self.committed_until)

        agreed = self._agreed_prefix(fresh)
        stable_cutoff = head - self.stable_horizon_s
        force_cutoff = head - (self.stable_horizon_s if catching_up else self.force_horizon_s)

        # Longest prefix eligible by agreement + stability.
        agree_n = 0
        for i, w in enumerate(fresh[:agreed]):
            if w.end <= stable_cutoff:
                agree_n = i + 1
            else:
                break
        # Snap an agreement-only commit back to a natural boundary.
        if agree_n:
            snapped = 0
            for i in range(agree_n):
                w = fresh[i]
                nxt = fresh[i + 1] if i + 1 < len(fresh) else None
                if (
                    _is_sentence_end(w)
                    or nxt is None
                    or nxt.start - w.end >= PAUSE_BOUNDARY_S
                    or nxt.speaker != w.speaker
                ):
                    snapped = i + 1
            agree_n = snapped
        # Forced prefix: old enough to commit regardless of agreement.
        force_n = 0
        for i, w in enumerate(fresh):
            if w.end <= force_cutoff:
                force_n = i + 1
            else:
                break
        n = max(agree_n, force_n)
        final = tuple(fresh[:n])
        interim = tuple(fresh[n:])
        self._remember(final)
        if final:
            self.committed_until = max(self.committed_until, final[-1].end)
        # Silence: nothing (left) to commit before the force cutoff → move the
        # commit point so the next window does not keep re-sending silence.
        silent_until = interim[0].start if interim else force_cutoff
        silent_until = min(silent_until, force_cutoff)
        if silent_until > self.committed_until:
            self.committed_until = silent_until
        self._previous = interim
        return CommitResult(final=final, interim=interim, committed_until=self.committed_until)


def join_words(words: list[TimedWord] | tuple[TimedWord, ...]) -> str:
    text = " ".join(w.text for w in words)
    # Tidy spaces the tokenizer may leave before punctuation.
    return re.sub(r"\s+([,.;:?!])", r"\1", text).strip()


def segment_words(
    words: list[TimedWord] | tuple[TimedWord, ...],
    *,
    pause_s: float = 1.2,
    max_words: int = 60,
    max_duration_s: float = 30.0,
) -> list[SegmentDraft]:
    """Group committed words into segments.

    Breaks on speaker change, a pause ≥ `pause_s`, or (after a sentence end)
    a segment that has grown past `max_words`/`max_duration_s`.
    """
    out: list[SegmentDraft] = []
    current: list[TimedWord] = []

    def emit() -> None:
        if current:
            out.append(
                SegmentDraft(
                    speaker=current[0].speaker,
                    start=current[0].start,
                    end=current[-1].end,
                    text=join_words(current),
                )
            )
            current.clear()

    for w in words:
        if current:
            last = current[-1]
            too_long = (
                len(current) >= max_words or w.end - current[0].start >= max_duration_s
            ) and _is_sentence_end(last)
            hard_cap = len(current) >= max_words * 2
            if w.speaker != last.speaker or w.start - last.end >= pause_s or too_long or hard_cap:
                emit()
        current.append(w)
    emit()
    return [s for s in out if s.text]
