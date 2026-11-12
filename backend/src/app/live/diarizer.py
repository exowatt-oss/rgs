"""Pluggable speaker layer for live windows (`LIVE_DIARIZER`).

`nemotron` keeps the original behaviour: each window's speakers are the
tags Nemotron wrote, mapped to session labels by `SpeakerTracker.assign`.

`sortformer` is an opt-in diarizer transport (`POST {url}/diarize/stream`)
for an operator-supplied model server. It gets back who spoke when over the
last `live_diarizer_context_s`. Nemotron still produces the words; each word
takes the diarizer speaker it overlaps most in time. The model's speaker
cache keeps slots stable across calls. The default `nemotron` mode requires
no separate diarizer server.

Diarizer labels (slots) become session labels in `DiarLinker`. A slot linked
with real evidence is pinned to its session speaker until the stream restarts
(the sidecar keeps a person in one slot), so an ambiguous voiceprint or a
quiet window cannot swap people. Unpinned slots link by:

1. **Overlap.** Each call returns ~60 s that mostly overlap audio already
   linked, so a label that covers the same time as session speaker X's
   linked turns is X (greedy one-to-one by overlapped seconds). This also
   relinks a restarted stream (sidecar restart, session resume).
2. **Voiceprint.** A label with no overlap evidence joins the closest active
   speaker when its TitaNet cosine reaches the match threshold.
3. **New.** Otherwise it is a new speaker once it has `min_new_s` of speech
   in audio not linked yet. Similar voices (cosine 0.5-0.65 between
   different people on a laptop mic) therefore stay apart: the diarizer, not
   a cosine threshold, decides that they are two people.

Turns are linked only up to `lag_s` behind the newest audio, where the
diarizer has seen some right context. Any diarizer failure falls back to the
Nemotron path for that window; nothing here raises into the session loop.
"""

from __future__ import annotations

import base64
import dataclasses
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.live.settings import LiveSettings
from app.live.speakers import Assignment, LocalSpeaker, SpeakerTracker

log = structlog.get_logger(__name__)

PROVIDERS = ("nemotron", "sortformer")
# A word that overlaps no diarizer turn takes the nearest one this close.
NEAR_S = 1.0

Turn = tuple[float, float, str]


class DiarizerError(RuntimeError):
    pass


class SortformerClient:
    """`POST {base}/diarize/stream` on the GPU box (same bearer token as Nemotron)."""

    def __init__(self, settings: LiveSettings) -> None:
        self.url = (settings.live_diarizer_url or settings.nemotron_base_url).rstrip("/")
        self.api_key = settings.live_diarizer_api_key or settings.nemotron_api_key
        self.timeout_s = settings.live_diarizer_timeout_s
        self.context_s = settings.live_diarizer_context_s

    def stream(
        self, stream_id: str, offset_s: float, pcm: bytes, *, flush: bool = False
    ) -> tuple[list[Turn], float, bool]:
        """Append `pcm` (starting at session time `offset_s`) to the stream.

        Returns (turns in session seconds over the last `context_s`, diarized
        up to, reset). A gap or an unknown stream restarts it at `offset_s`.
        """
        body = {
            "stream": stream_id,
            "offset_s": round(offset_s, 4),
            "audio_b64": base64.b64encode(pcm).decode(),
            "context_s": self.context_s,
            "flush": flush,
        }
        data = self._post("/diarize/stream", body)
        return _turns(data), float(data.get("until_s") or 0.0), bool(data.get("reset"))

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = httpx.post(
                self.url + path,
                json=body,
                headers=headers,
                timeout=httpx.Timeout(self.timeout_s, connect=5.0),
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DiarizerError(f"diarize failed: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise DiarizerError("diarize returned a non-object")
        return data


def _turns(data: dict[str, Any]) -> list[Turn]:
    turns = data.get("turns")
    if not isinstance(turns, list):
        raise DiarizerError("diarize returned no turns")
    out: list[Turn] = []
    for t in turns:
        if isinstance(t, list | tuple) and len(t) >= 3:
            s, e = float(t[0]), float(t[1])
            if e > s:
                out.append((s, e, str(t[2])))
    return out


def load_diarizer(settings: LiveSettings) -> SortformerClient | None:
    if settings.live_diarizer == "sortformer" and (
        settings.live_diarizer_url or settings.nemotron_base_url
    ):
        return SortformerClient(settings)
    return None


def word_tags(words: Sequence[Any], turns: Sequence[Turn], near_s: float = NEAR_S) -> list[str]:
    """The diarizer label of each word (most overlap; else nearest turn within
    `near_s`; else the previous word's, else the next word's)."""
    out: list[str | None] = []
    for w in words:
        best, best_ov = None, 0.0
        for s, e, tag in turns:
            ov = min(e, w.end) - max(s, w.start)
            if ov > best_ov:
                best, best_ov = tag, ov
        if best is None:
            mid = (w.start + w.end) / 2
            dist = [(min(abs(mid - s), abs(mid - e)), tag) for s, e, tag in turns]
            near = min(dist, default=None)
            if near is not None and near[0] <= near_s:
                best = near[1]
        out.append(best)
    for i in range(len(out)):
        if out[i] is None and i > 0:
            out[i] = out[i - 1]
    for i in range(len(out) - 2, -1, -1):
        if out[i] is None:
            out[i] = out[i + 1]
    return [t or "?" for t in out]


@dataclass
class DiarLinker:
    """Diarizer slots -> session labels.

    A streaming diarizer keeps a person in the same slot for the whole stream,
    so a slot, once linked with real evidence, is **pinned** to its session
    label until the stream restarts (`reset`). Unpinned slots link by
    (1) overlap with already-linked turns, (2) voiceprint, (3) as a new
    speaker; a slot with too little evidence for any of these is labelled for
    this window only (best free voiceprint, else the most recent free speaker)
    and is neither pinned nor remembered.
    """

    min_overlap_s: float = 0.4
    min_new_s: float = 1.0
    lag_s: float = 1.5
    keep_s: float = 180.0
    linked_until: float = 0.0
    history: list[Turn] = field(default_factory=list)  # (start, end, session label)
    pinned: dict[str, str] = field(default_factory=dict)  # slot -> session label

    def reset(self) -> None:
        """The diarizer restarted its stream: slots mean nothing any more."""
        self.pinned.clear()

    def link(
        self,
        tracker: SpeakerTracker,
        turns: Sequence[Turn],
        locals_: Sequence[LocalSpeaker],
        *,
        head_s: float,
        flush: bool = False,
    ) -> tuple[dict[str, str], list[str]]:
        """Map slots (`turns` in session seconds) to session labels.

        Returns (mapping, created labels). Creates/absorbs speakers in `tracker`.
        """
        by_tag = {loc.tag: loc for loc in locals_}
        tags = sorted({t for _s, _e, t in turns} | set(by_tag))
        mapping: dict[str, str] = {}
        for tag in tags:
            if tag in self.pinned:
                mapping[tag] = tracker.resolve(self.pinned[tag])
        taken: set[str] = set(mapping.values())
        # 1. overlap with already-linked turns
        ov: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for s, e, tag in turns:
            if s >= self.linked_until or tag in mapping:
                continue
            e = min(e, self.linked_until)
            for hs, he, lab in self.history:
                o = min(e, he) - max(s, hs)
                if o > 0:
                    ov[tag][tracker.resolve(lab)] += o
        pairs = sorted(
            ((o, tag, lab) for tag, d in ov.items() for lab, o in d.items()), reverse=True
        )
        for o, tag, lab in pairs:
            if o < self.min_overlap_s:
                break
            if tag in mapping or lab in taken:
                continue
            mapping[tag] = self.pinned[tag] = lab
            taken.add(lab)
        # speech in audio not linked yet, per slot
        fresh: dict[str, float] = defaultdict(float)
        first: dict[str, float] = {}
        for s, e, tag in turns:
            fresh[tag] += max(0.0, e - max(s, self.linked_until))
            first[tag] = min(first.get(tag, s), s)
        created: list[str] = []
        weak: set[str] = set()
        # New speakers are numbered in order of first speech.
        for tag in sorted(tags, key=lambda t: (first.get(t, float("inf")), -fresh[t])):
            if tag in mapping:
                continue
            local = by_tag.get(tag)
            if local is None:
                continue  # no words, no evidence: leave it for a later window
            emb = _unit(local.embedding)
            scored = []
            for st in tracker.active():
                sim = tracker.similarity(emb, st.centroid)
                if sim is not None:
                    scored.append((sim, st.label))
            scored.sort(reverse=True)
            free = [(sim, lab) for sim, lab in scored if lab not in taken]
            # 2. voiceprint (pinned only with enough speech; a short clear match
            # is still that person for this window, never a new speaker)
            voice_match = bool(free) and free[0][0] >= tracker.match_threshold
            if voice_match and local.speech_s >= self.min_new_s:
                mapping[tag] = self.pinned[tag] = free[0][1]
                taken.add(free[0][1])
                continue
            n_active = len(tracker.active())
            room = n_active < tracker.max_speakers and not (
                tracker.expected_speakers and n_active >= tracker.expected_speakers
            )
            # 3. the diarizer says this slot is someone not linked yet: with
            # enough fresh speech that is a new person (no voiceprint needed).
            if (not voice_match and fresh[tag] >= self.min_new_s and room) or not tracker.active():
                state = tracker.create(local)
                mapping[tag] = self.pinned[tag] = state.label
                taken.add(state.label)
                created.append(state.label)
                continue
            # 4. weak: this window only
            free_active = [st for st in tracker.active() if st.label not in taken]
            if free:
                lab = free[0][1]
            elif free_active:
                lab = max(free_active, key=lambda st: st.last_seen_s).label
            elif scored:
                lab = scored[0][1]
            else:
                lab = max(tracker.active(), key=lambda st: st.last_seen_s).label
            mapping[tag] = lab
            taken.add(lab)
            weak.add(tag)
        # absorb voiceprints of linked slots (created ones already have theirs)
        for tag, lab in mapping.items():
            loc = by_tag.get(tag)
            if loc is None or lab in created or tag in weak:
                continue
            state = tracker.speakers.get(tracker.resolve(lab))
            if state is None:
                continue
            sim = tracker.similarity(_unit(loc.embedding), state.centroid)
            tracker.absorb(
                state, loc, update_centroid=sim is None or sim >= tracker.match_threshold
            )
        # remember linked turns up to the lag (weak ones are not evidence)
        until = head_s if flush else max(self.linked_until, head_s - self.lag_s)
        for s, e, tag in turns:
            lab = mapping.get(tag)
            if lab is None or tag in weak:
                continue
            a, b = max(s, self.linked_until), min(e, until)
            if b > a:
                self.history.append((a, b, lab))
        self.linked_until = max(self.linked_until, until)
        cutoff = self.linked_until - self.keep_s
        if self.history and self.history[0][1] < cutoff:
            self.history = [h for h in self.history if h[1] >= cutoff]
        return mapping, created


def relabel_window(
    words: Sequence[Any], turns_abs: Sequence[Turn], start: float, end: float
) -> tuple[list[Any], tuple[Turn, ...]]:
    """Window words re-tagged with diarizer labels + the diarizer's spans
    clipped to the window (both relative to `start`, like Nemotron's)."""
    rel = [(s - start, e - start, t) for s, e, t in turns_abs]
    tags = word_tags(words, rel)
    relabelled = [dataclasses.replace(w, speaker=tag) for w, tag in zip(words, tags, strict=True)]
    span = end - start
    spans = tuple((max(0.0, s), min(span, e), t) for s, e, t in rel if e > 0 and s < span)
    return relabelled, spans


def assign_window(
    tracker: SpeakerTracker,
    linker: DiarLinker,
    turns_abs: Sequence[Turn],
    locals_: Sequence[LocalSpeaker],
    *,
    head_s: float,
    flush: bool,
) -> Assignment:
    """`SpeakerTracker.assign` for diarizer windows: the linker decides who
    is who/new; the tracker keeps the speakers, cannot-link evidence and merges."""
    mapping, created = linker.link(tracker, turns_abs, locals_, head_s=head_s, flush=flush)
    by_tag = {loc.tag: loc for loc in locals_}
    tracker.note_windows({t: lab for t, lab in mapping.items() if t in by_tag}, by_tag)
    merged = tracker.recluster()
    mapping = {tag: tracker.resolve(lab) for tag, lab in mapping.items()}
    updated = sorted(set(mapping.values()) - set(created))
    return Assignment(
        mapping=mapping, created=tuple(created), updated=tuple(updated), merged=tuple(merged)
    )


def tune_tracker(tracker: SpeakerTracker, settings: LiveSettings) -> None:
    """With a diarizer deciding who is new, voiceprint merges must be rarer:
    similar voices legitimately reach 0.5-0.65 cosine."""
    tracker.merge_threshold = settings.live_diarizer_merge_threshold
    # Quiet speakers join a bigger one only on a clear voiceprint match
    # (similar voices on one microphone can overlap in embedding space).
    tracker.small_merge_threshold = max(tracker.small_merge_threshold, 0.7)
    tracker.tiny_s = min(tracker.tiny_s, 1.0)


def _unit(vec):
    from app.live.speakers import _unit as unit

    return unit(vec)
