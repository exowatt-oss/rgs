"""Session-global speaker identity across transcription windows (design notes).

Each Nemotron window diarizes on its own, so its tags (`0`, `1`, ...) mean
nothing outside that window. `SpeakerTracker` maps window-local tags onto
stable session labels (`S1`, `S2`, ...) with online clustering:

* **Embeddings.** For every local tag the runtime asks the endpoint's
  `speaker_embed` task for vectors of its longest clean spans and averages
  them (weighted by duration). Each session speaker keeps a running,
  L2-normalised centroid (weight capped so it can drift with the voice).
* **Overlap votes.** Consecutive windows overlap (left context + the still
  interim tail), so words of the new window that coincide in time with
  already-labelled words "vote" for that label. Strong agreement is a prior
  that survives a missing/noisy embedding.
* **Assignment.** Greedy one-to-one matching by score (two local tags in one
  window are different people). A match needs `score >= match_threshold`.
  An unmatched tag becomes a new speaker when its best similarity is below
  `new_threshold` and it has `min_new_s` of speech (or there is no speaker
  yet); otherwise it joins its best free candidate (ambiguous band).
* **Merging.** Two speakers whose centroids converge (cosine ≥
  `merge_threshold`, both with ≥ `merge_min_s` of evidence) are merged into
  the older one; the runtime emits `speaker.update` with `merged_into`.
  With a speaker-count hint (`expected_speakers`), while there are more
  active speakers than expected the closest pair is merged at the lower
  `hint_merge_threshold`.
* **No voiceprint, no new speaker.** A window-local tag without an
  embedding (too little clean speech, or the embed call failed) joins an
  existing speaker; only the very first speaker is created blind. Short
  blind tags were the main source of phantom 1-2 s speakers.
* **Speech time** is counted by the runtime from *committed* words
  (`add_speech`), not per window: windows overlap, so per-window counting
  reported more speech than the session lasted.

Pure numpy, no I/O; unit-tested in `tests/test_live_speakers.py`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np

CENTROID_WEIGHT_CAP_S = 120.0


@dataclass
class SpeakerState:
    label: str
    color_index: int
    centroid: np.ndarray | None = None
    weight: float = 0.0
    speech_seconds: float = 0.0
    last_seen_s: float = 0.0
    merged_into: str | None = None


@dataclass(frozen=True)
class LocalSpeaker:
    tag: str
    speech_s: float
    embedding: np.ndarray | None
    last_end_s: float = 0.0
    first_start_s: float = 0.0


@dataclass(frozen=True)
class Assignment:
    mapping: dict[str, str]
    created: tuple[str, ...]
    updated: tuple[str, ...]
    merged: tuple[tuple[str, str], ...]  # (merged_label, into_label)


def _unit(vec: np.ndarray | list[float] | None) -> np.ndarray | None:
    if vec is None:
        return None
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.size == 0 or not np.isfinite(arr).all():
        return None
    norm = float(np.linalg.norm(arr))
    if norm < 1e-8:
        return None
    return arr / norm


def average_embeddings(
    vectors: list[tuple[np.ndarray | list[float] | None, float]],
) -> np.ndarray | None:
    acc: np.ndarray | None = None
    for vec, weight in vectors:
        unit = _unit(vec)
        if unit is None or weight <= 0:
            continue
        if acc is not None and acc.shape != unit.shape:
            continue
        acc = unit * weight if acc is None else acc + unit * weight
    return _unit(acc)


def cooccurrence(
    spans: Iterable[tuple[str, float, float]], *, window_s: float, step_s: float
) -> tuple[Counter[frozenset[str]], Counter[str]]:
    """(windows two labels both spoke >= 1 s in, windows each label did).

    Replays the live loop's windows (`window_s` long, every `step_s`) over
    segment times; the same evidence `SpeakerTracker._note_windows` counts live.
    """
    together: Counter[frozenset[str]] = Counter()
    present: Counter[str] = Counter()
    rows = [(lab, max(0.0, a), max(0.0, b)) for lab, a, b in spans if b > a]
    if not rows:
        return together, present
    n = int(max(b for _l, _a, b in rows)) + 2
    per_label: dict[str, np.ndarray] = {}
    for lab, a, b in rows:
        arr = per_label.setdefault(lab, np.zeros(n, dtype=np.float64))
        i, j = int(a), int(b)
        if i == j:
            arr[i] += b - a
        else:
            arr[i] += i + 1 - a
            arr[i + 1 : j] += 1.0
            arr[j] += b - j
    cums = {lab: np.concatenate([[0.0], np.cumsum(arr)]) for lab, arr in per_label.items()}
    w, step = max(1, round(window_s)), max(step_s, 0.5)
    end = float(n)
    t = min(float(w), end)
    while True:
        hi = int(t)
        lo = max(0, hi - w)
        talkers = sorted(lab for lab, c in cums.items() if c[hi] - c[lo] >= 1.0)
        for lab in talkers:
            present[lab] += 1
        for i, a in enumerate(talkers):
            for b in talkers[i + 1 :]:
                together[frozenset((a, b))] += 1
        if t >= end:
            break
        t = min(t + step, end)
    return together, present


@dataclass
class SpeakerTracker:
    match_threshold: float = 0.55
    # A window tag opens a new speaker only when its voiceprint is below this
    # cosine to every existing speaker AND it has >= min_new_s of speech.
    # Distinct voices can still overlap in embedding space; use a conservative threshold.
    new_threshold: float = 0.30
    min_new_s: float = 2.5
    max_speakers: int = 12
    # Re-clustering on every window: merge the closest pair of speakers while
    # their centroids are >= merge_threshold (average linkage on centroids).
    merge_threshold: float = 0.65
    merge_min_s: float = 3.0
    # Fragments: a speaker idle for small_idle_s with < small_s of committed
    # speech (and < small_share of all speech) joins the nearest bigger speaker
    # (always when < tiny_s, else when cosine >= small_merge_threshold).
    small_s: float = 12.0
    small_share: float = 0.1
    small_idle_s: float = 15.0
    tiny_s: float = 2.5
    small_merge_threshold: float = 0.30
    # Speaker-count hint (None = unknown): never more active speakers than this;
    # extra ones join their nearest speaker.
    expected_speakers: int | None = None
    hint_merge_threshold: float = -1.0
    hint_new_threshold: float = 0.2
    hint_min_new_s: float = 4.0
    strong_vote_min_words: int = 3
    strong_vote_fraction: float = 0.7
    speakers: dict[str, SpeakerState] = field(default_factory=dict)
    now_s: float = 0.0  # latest audio time seen (for idleness)
    # Labels never merged away automatically (manually named); may absorb others.
    is_locked: Callable[[str], bool] = field(default=lambda _label: False, repr=False)
    # Cannot-link evidence: windows where Nemotron kept two labels apart as
    # separate tags (people who talk to each other), and windows per label.
    together: Counter[frozenset[str]] = field(default_factory=Counter)
    present: Counter[str] = field(default_factory=Counter)
    cannot_link_min: int = 3
    cannot_link_share: float = 0.3

    # ---- helpers ---------------------------------------------------------

    def active(self) -> list[SpeakerState]:
        return [s for s in self.speakers.values() if s.merged_into is None]

    def resolve(self, label: str) -> str:
        seen = set()
        while label in self.speakers and self.speakers[label].merged_into and label not in seen:
            seen.add(label)
            label = self.speakers[label].merged_into or label
        return label

    def _new_label(self) -> str:
        n = len(self.speakers) + 1
        while f"S{n}" in self.speakers:
            n += 1
        return f"S{n}"

    def _create(self, local: LocalSpeaker) -> SpeakerState:
        label = self._new_label()
        state = SpeakerState(label=label, color_index=len(self.speakers))
        self.speakers[label] = state
        self._absorb(state, local)
        return state

    def add_speech(self, label: str, seconds: float) -> None:
        """Count committed speech for `label` (resolved through merges)."""
        state = self.speakers.get(self.resolve(label))
        if state is not None and seconds > 0:
            state.speech_seconds += seconds

    def merge(self, label: str, into: str) -> bool:
        """Merge `label` into `into` (naming pass / manual). False if not possible."""
        src, dst_label = self.speakers.get(label), self.resolve(into)
        dst = self.speakers.get(dst_label)
        if src is None or dst is None or self.resolve(label) == dst_label:
            return False
        src.merged_into = dst_label
        dst.speech_seconds += src.speech_seconds
        if src.centroid is not None and dst.centroid is not None:
            mixed = _unit(dst.centroid * dst.weight + src.centroid * src.weight)
            if mixed is not None:
                dst.centroid = mixed
            dst.weight = min(dst.weight + src.weight, CENTROID_WEIGHT_CAP_S)
        elif dst.centroid is None and src.centroid is not None:
            dst.centroid, dst.weight = src.centroid, src.weight
        for other in self.speakers.values():
            if other.merged_into == label:
                other.merged_into = dst_label
        for pair in [pr for pr in self.together if label in pr]:
            n = self.together.pop(pair)
            rest = pair - {label}
            if rest and dst_label not in rest:
                self.together[frozenset((dst_label, *rest))] += n
        self.present[dst_label] += self.present.pop(label, 0)
        return True

    # Public hooks for `app.live.diarizer` (there the diarizer decides who is new).
    def create(self, local: LocalSpeaker) -> SpeakerState:
        return self._create(local)

    def absorb(
        self, state: SpeakerState, local: LocalSpeaker, *, update_centroid: bool = True
    ) -> None:
        self._absorb(state, local, update_centroid=update_centroid)

    def note_windows(self, mapping: dict[str, str], by_tag: dict[str, LocalSpeaker]) -> None:
        self._note_windows(mapping, by_tag)

    def unmerge(self, label: str) -> bool:
        state = self.speakers.get(label)
        if state is None or state.merged_into is None:
            return False
        dst = self.speakers.get(self.resolve(label))
        state.merged_into = None
        if dst is not None and dst is not state:
            dst.speech_seconds = max(0.0, dst.speech_seconds - state.speech_seconds)
        return True

    def _absorb(
        self, state: SpeakerState, local: LocalSpeaker, *, update_centroid: bool = True
    ) -> None:
        state.last_seen_s = max(state.last_seen_s, local.last_end_s)
        emb = _unit(local.embedding)
        if emb is None or (not update_centroid and state.centroid is not None):
            return
        dw = min(max(local.speech_s, 0.25), 10.0)
        if state.centroid is None or state.centroid.shape != emb.shape:
            state.centroid = emb
            state.weight = dw
            return
        mixed = _unit(state.centroid * state.weight + emb * dw)
        if mixed is not None:
            state.centroid = mixed
        state.weight = min(state.weight + dw, CENTROID_WEIGHT_CAP_S)

    @staticmethod
    def similarity(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
        if a is None or b is None or a.shape != b.shape:
            return None
        return float(a @ b)

    # ---- main entry --------------------------------------------------------

    def assign(
        self,
        locals_: list[LocalSpeaker],
        votes: dict[str, Counter[str]] | None = None,
    ) -> Assignment:
        votes = votes or {}
        for local in locals_:
            self.now_s = max(self.now_s, local.last_end_s)
        active = self.active()
        candidates: list[tuple[float, str, str]] = []  # (score, tag, label)
        best_sim: dict[str, float] = {}
        for local in locals_:
            emb = _unit(local.embedding)
            tag_votes = votes.get(local.tag) or Counter()
            total_votes = sum(tag_votes.values())
            for state in active:
                sim = self.similarity(emb, state.centroid)
                if sim is not None:
                    best_sim[local.tag] = max(best_sim.get(local.tag, -1.0), sim)
                vote_n = tag_votes.get(state.label, 0)
                for merged in self.speakers.values():
                    if merged.merged_into == state.label:
                        vote_n += tag_votes.get(merged.label, 0)
                vote_frac = vote_n / total_votes if total_votes else 0.0
                strong = (
                    vote_n >= self.strong_vote_min_words and vote_frac >= self.strong_vote_fraction
                )
                score = sim if sim is not None else 0.0
                if strong:
                    score = max(score, self.match_threshold) + 0.3 * vote_frac
                elif sim is None and vote_n:
                    # Weak overlap evidence without an embedding.
                    score = self.match_threshold * vote_frac
                else:
                    score += 0.15 * vote_frac
                candidates.append((score, local.tag, state.label))
        candidates.sort(key=lambda c: c[0], reverse=True)

        mapping: dict[str, str] = {}
        taken: set[str] = set()
        for score, tag, label in candidates:
            if tag in mapping or label in taken:
                continue
            if score >= self.match_threshold:
                mapping[tag] = label
                taken.add(label)

        created: list[str] = []
        # New clusters are numbered in order of first speech in the window.
        for local in sorted(locals_, key=lambda loc: (loc.first_start_s, -loc.speech_s)):
            if local.tag in mapping:
                continue
            sim = best_sim.get(local.tag)
            n_active = len(self.active())
            has_room = n_active < self.max_speakers
            has_voice = _unit(local.embedding) is not None
            clearly_new = (
                has_voice
                and (sim is None or sim < self.new_threshold)
                and local.speech_s >= self.min_new_s
            )
            if clearly_new and self.expected_speakers and n_active >= self.expected_speakers:
                clearly_new = False  # never more speakers than the owner said
            if has_room and (not self.active() or clearly_new):
                state = self._create(local)
                mapping[local.tag] = state.label
                taken.add(state.label)
                created.append(state.label)
                continue
            # Ambiguous band / too short / full: the best candidate. A window
            # often splits one voice over two tags, so a label already taken in
            # this window is allowed when it is clearly the closest.
            mine = [(score, label) for score, tag, label in candidates if tag == local.tag]
            free = [(score, label) for score, label in mine if label not in taken]
            best_any = max(mine, default=None)
            if (
                best_any is not None
                and best_any[0] >= self.match_threshold
                and (not free or best_any[0] > max(free)[0] + 0.1)
            ):
                mapping[local.tag] = best_any[1]
                continue
            if free:
                # Ties (no voiceprint, no votes) go to whoever spoke most recently.
                label = max(free, key=lambda sl: (sl[0], self.speakers[sl[1]].last_seen_s))[1]
            else:
                pool = [s for s in self.active() if s.label not in taken] or self.active()
                if not pool:
                    state = self._create(local)
                    mapping[local.tag] = state.label
                    created.append(state.label)
                    continue
                label = max(pool, key=lambda s: s.last_seen_s).label
            mapping[local.tag] = label
            taken.add(label)

        updated: set[str] = set()
        by_tag = {loc.tag: loc for loc in locals_}
        for tag, label in mapping.items():
            if label in created:
                continue
            state = self.speakers[label]
            sim = self.similarity(_unit(by_tag[tag].embedding), state.centroid)
            # A weak attachment must not pull the voiceprint toward someone else.
            self._absorb(
                state, by_tag[tag], update_centroid=sim is None or sim >= self.new_threshold
            )
            updated.add(label)

        self._note_windows(mapping, by_tag)
        merged = self.recluster()
        if merged:
            mapping = {tag: self.resolve(label) for tag, label in mapping.items()}
        return Assignment(
            mapping=mapping,
            created=tuple(created),
            updated=tuple(sorted(updated - set(created))),
            merged=tuple(merged),
        )

    def _note_windows(self, mapping: dict[str, str], by_tag: dict[str, LocalSpeaker]) -> None:
        talkers = sorted(
            {self.resolve(lab) for tag, lab in mapping.items() if by_tag[tag].speech_s >= 1.0}
        )
        for lab in talkers:
            self.present[lab] += 1
        for i, a in enumerate(talkers):
            for b in talkers[i + 1 :]:
                self.together[frozenset((a, b))] += 1

    def rebuild_cannot_link(
        self,
        spans: Iterable[tuple[str, float, float]],
        *,
        window_s: float,
        step_s: float,
    ) -> None:
        """Recount `together`/`present` from stored segments after a restart.

        The counts live in memory only; without this a deploy during a call
        forgets that two labels are different people and the next re-cluster
        merges them on a close voiceprint (synthetic restart case).
        Replays the live loop's windows (`window_s`
        long, every `step_s`) over the segments' times, labels as they are now.
        """
        together, present = cooccurrence(
            [(self.resolve(lab), a, b) for lab, a, b in spans], window_s=window_s, step_s=step_s
        )
        self.together, self.present = together, present

    def apart(self, a: str, b: str) -> bool:
        """True when Nemotron repeatedly heard `a` and `b` as different people.

        Windows overlap (30 s every 3 s), so one short blip shares ~10 windows
        with whoever was talking: a label under `tiny_s` of speech is never apart.
        """
        for lab in (a, b):
            st = self.speakers.get(lab)
            if st is None or st.speech_seconds < self.tiny_s:
                return False
        n = self.together.get(frozenset((a, b)), 0)
        base = min(self.present.get(a, 0), self.present.get(b, 0))
        return n >= self.cannot_link_min and n >= self.cannot_link_share * max(base, 1)

    def recluster(self) -> list[tuple[str, str]]:
        """Merge split speakers; runs after every window over the whole session.

        1. Closest pair of established speakers while cosine >= merge_threshold.
        2. Fragment speakers (tiny, or small and idle) join their nearest speaker.
        3. Over the count hint: the smallest speaker joins its nearest one.
        The survivor is the speaker with more speech (ties: the older label).
        """
        merged: list[tuple[str, str]] = []
        while True:
            pair = self._closest_pair(min_weight=self.merge_min_s)
            if pair is None or pair[0] < self.merge_threshold:
                break
            merged.append(self._merge_pair(pair[1], pair[2]))
        for state in sorted(self.active(), key=lambda st: st.speech_seconds):
            if state.merged_into is not None or len(self.active()) <= 1:
                continue
            idle = self.now_s - state.last_seen_s >= self.small_idle_s
            total = sum(st.speech_seconds for st in self.active())
            small = min(self.small_s, self.small_share * total)
            tiny = state.speech_seconds < self.tiny_s
            if (
                not idle
                or self.is_locked(state.label)
                or not (tiny or state.speech_seconds < small)
            ):
                continue
            target = self._nearest(state, bigger_only=True, linkable=True)
            if target is None:
                continue
            sim, other = target
            if tiny or sim is None or sim >= self.small_merge_threshold:
                merged.append(self._merge_into(state, other))
        while self.expected_speakers and len(self.active()) > self.expected_speakers:
            movable = [st for st in self.active() if not self.is_locked(st.label)]
            if not movable:
                break
            smallest = min(movable, key=lambda st: (st.speech_seconds, -st.color_index))
            target = self._nearest(smallest, bigger_only=False, linkable=True) or self._nearest(
                smallest, bigger_only=False
            )
            if target is None:
                break
            merged.append(self._merge_into(smallest, target[1]))
        return merged

    def _closest_pair(self, *, min_weight: float):
        best = None
        active = sorted(self.active(), key=lambda st: st.color_index)
        for i, a in enumerate(active):
            if a.weight < min_weight:
                continue
            for b in active[i + 1 :]:
                if b.weight < min_weight:
                    continue
                if (self.is_locked(a.label) and self.is_locked(b.label)) or self.apart(
                    a.label, b.label
                ):
                    continue
                sim = self.similarity(a.centroid, b.centroid)
                if sim is not None and (best is None or sim > best[0]):
                    best = (sim, a, b)
        return best

    def _nearest(self, state: SpeakerState, *, bigger_only: bool, linkable: bool = False):
        """(cosine or None, speaker) closest to `state` among the other active ones."""
        others = [
            o
            for o in self.active()
            if o.label != state.label
            and (not bigger_only or o.speech_seconds > state.speech_seconds)
            and not (linkable and self.apart(state.label, o.label))
        ]
        if not others:
            return None
        scored = [(self.similarity(state.centroid, o.centroid), o) for o in others]
        with_sim = [(sim, o) for sim, o in scored if sim is not None]
        if with_sim:
            return max(with_sim, key=lambda so: so[0])
        # No voiceprint to compare: the speaker with the most speech.
        return (None, max(others, key=lambda o: o.speech_seconds))

    def _merge_pair(self, a: SpeakerState, b: SpeakerState) -> tuple[str, str]:
        """Merge two speakers; the one with more speech (ties: older) survives."""
        a_wins = (a.speech_seconds, -a.color_index) >= (b.speech_seconds, -b.color_index)
        keep, gone = (a, b) if a_wins else (b, a)
        if self.is_locked(gone.label):
            keep, gone = gone, keep
        return self._merge_into(gone, keep)

    def _merge_into(self, gone: SpeakerState, keep: SpeakerState) -> tuple[str, str]:
        self.merge(gone.label, keep.label)
        keep.last_seen_s = max(keep.last_seen_s, gone.last_seen_s)
        return (gone.label, keep.label)
