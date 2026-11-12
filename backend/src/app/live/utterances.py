"""Join transcript fragments back into utterances.

Live windows commit a few words at a time, and overlapping speech arrives
word-interleaved ("S1: example / S2: Search / S1: docs / S2: ready"),
so one sentence is often spread over several segments of the same speaker
with other speakers' fragments in between. Everything that reads the
transcript as language — self-introduction detection, the decision gate, the
naming pass, the rendered final transcript — first stitches each speaker's
fragments back together here.

A fragment continues its speaker's open utterance when it starts within
`gap_s` of that utterance's end (measured on the speaker's own timeline, so
another person's short interjection does not cut it). With `sentences=True`
an utterance also closes at sentence-final punctuation unless it is still a
short fragment, so two separate questions stay two lines for the gate.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

GAP_S = 1.5
MAX_CHARS = 600
_SENTENCE_END = (".", "?", "!")


@dataclass
class Utterance:
    speaker: str
    start: float
    end: float
    text: str
    seqs: list[int] = field(default_factory=list)
    frag_texts: list[str] = field(default_factory=list, repr=False)
    frag_times: list[tuple[float, float]] = field(default_factory=list, repr=False)

    @property
    def seq(self) -> int:
        """The newest fragment's seq (the utterance's id for watermarks)."""
        return self.seqs[-1] if self.seqs else 0


def _closes(text: str) -> bool:
    words = text.split()
    return bool(words) and len(words) > 3 and text.rstrip().endswith(_SENTENCE_END)


def coalesce(
    fragments: Iterable[tuple[int, str, float, float, str]],
    *,
    resolve: Callable[[str], str] = lambda label: label,
    gap_s: float = GAP_S,
    sentences: bool = False,
    max_chars: int = MAX_CHARS,
) -> list[Utterance]:
    """(seq, speaker, start_s, end_s, text) fragments in seq order -> utterances by start."""
    open_: dict[str, Utterance] = {}
    out: list[Utterance] = []
    for seq, label, start, end, text in fragments:
        text = " ".join((text or "").split())
        if not text:
            continue
        speaker = resolve(label)
        cur = open_.get(speaker)
        if (
            cur is not None
            and start - cur.end <= gap_s
            and len(cur.text) + len(text) < max_chars
            and not (sentences and _closes(cur.text))
        ):
            cur.text = f"{cur.text} {text}"
            cur.end = max(cur.end, end)
            cur.seqs.append(seq)
            cur.frag_texts.append(text)
            cur.frag_times.append((start, end))
            continue
        cur = Utterance(
            speaker=speaker,
            start=start,
            end=end,
            text=text,
            seqs=[seq],
            frag_texts=[text],
            frag_times=[(start, end)],
        )
        open_[speaker] = cur
        out.append(cur)
    out.sort(key=lambda u: (u.start, u.seq))
    return out


_SENTENCE = re.compile(r"[^.!?]+(?:[.!?]+|$)")


def sentence_lines(
    fragments: Iterable[tuple[int, str, float, float, str]],
    *,
    resolve: Callable[[str], str] = lambda label: label,
    gap_s: float = GAP_S,
) -> list[Utterance]:
    """Utterances re-split at sentence ends, each keyed by a real fragment seq.

    The decision gate judges one line per seq. Fragments cut sentences at
    arbitrary points (fictional example: "Where is the sample" / "tutorial?").
    One utterance can hold two separate requests. Each
    sentence becomes a line whose seq is the fragment where it ends; when a
    later sentence ends in the same fragment, the earlier one takes the
    latest free seq among the fragments it spans, or joins the next line
    when it has none. Seqs stay real segment seqs (trigger anchors point at
    actual lines) and increase with time.
    """
    out: list[Utterance] = []
    for u in coalesce(fragments, resolve=resolve, gap_s=gap_s, max_chars=100_000):
        out.extend(_split_sentences(u))
    out.sort(key=lambda x: (x.start, x.seq))
    return out


def _split_sentences(u: Utterance) -> list[Utterance]:
    # Rebuild the char offsets of each fragment inside the joined text.
    texts = u.frag_texts or [u.text]
    seqs = u.seqs if len(u.seqs) == len(texts) else [u.seq] * len(texts)
    times = u.frag_times if len(u.frag_times) == len(texts) else [(u.start, u.end)] * len(texts)
    bounds: list[tuple[int, int]] = []
    pos = 0
    for t in texts:
        bounds.append((pos, pos + len(t)))
        pos += len(t) + 1
    joined = " ".join(texts)

    def frag_at(offset: int) -> int:
        for i, (a, b) in enumerate(bounds):
            if offset < b + 1:
                return i
        return len(bounds) - 1

    spans = [(m.start(), m.end()) for m in _SENTENCE.finditer(joined) if m.group(0).strip()]
    if len(spans) <= 1:
        return [u]
    sentences = []
    for a, b in spans:
        text = joined[a:b].strip()
        lead = len(joined[a:b]) - len(joined[a:b].lstrip())
        sentences.append([text, frag_at(a + lead), frag_at(max(a, b - 1))])
    # Assign seqs from the last sentence backwards.
    used: set[int] = set()
    assigned: list[tuple[str, int, int, int]] = []  # text, first frag, last frag, seq index
    carry = ""
    for text, first, last in reversed(sentences):
        free = [i for i in range(last, first - 1, -1) if i not in used]
        if not free:
            # No fragment of its own: prepend to the line after it.
            if assigned:
                t, f, l_, k = assigned[-1]
                assigned[-1] = (f"{text} {t}", min(first, f), l_, k)
            else:
                carry = f"{text} {carry}".strip()
            continue
        k = free[0]
        used.add(k)
        assigned.append((f"{text} {carry}".strip() if carry else text, first, last, k))
        carry = ""
    assigned.reverse()
    lines = []
    for text, first, _last, k in assigned:
        lines.append(
            Utterance(
                speaker=u.speaker,
                start=times[first][0],
                end=times[k][1],
                text=text,
                seqs=[seqs[k]],
            )
        )
    return lines
