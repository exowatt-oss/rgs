"""Pure tests for Live Record's session-global speaker clustering (design notes)."""

from __future__ import annotations

from collections import Counter

import numpy as np

from app.live.speakers import LocalSpeaker, SpeakerTracker, average_embeddings

rng = np.random.default_rng(7)
VOICE_A = rng.normal(size=64)
VOICE_B = rng.normal(size=64)
VOICE_C = rng.normal(size=64)


def noisy(base: np.ndarray, scale: float = 0.25) -> np.ndarray:
    return base + rng.normal(size=base.shape) * scale * np.linalg.norm(base) / 8


def test_stable_labels_across_windows_with_swapped_local_tags() -> None:
    t = SpeakerTracker()
    a1 = t.assign([LocalSpeaker("0", 4.0, noisy(VOICE_A)), LocalSpeaker("1", 3.0, noisy(VOICE_B))])
    assert sorted(a1.created) == ["S1", "S2"]
    a_label, b_label = a1.mapping["0"], a1.mapping["1"]
    # Next window: the model happened to tag B first.
    a2 = t.assign([LocalSpeaker("0", 3.0, noisy(VOICE_B)), LocalSpeaker("1", 3.0, noisy(VOICE_A))])
    assert a2.created == ()
    assert a2.mapping == {"0": b_label, "1": a_label}


def test_new_voice_creates_speaker_and_short_unknown_does_not() -> None:
    t = SpeakerTracker()
    t.assign([LocalSpeaker("0", 4.0, noisy(VOICE_A))])
    short = t.assign([LocalSpeaker("0", 0.5, noisy(VOICE_C))])
    assert short.created == ()  # too little speech to open a cluster
    long = t.assign([LocalSpeaker("0", 3.0, noisy(VOICE_C))])
    assert long.created == ("S2",)


def test_overlap_votes_carry_identity_without_embeddings() -> None:
    t = SpeakerTracker()
    t.assign([LocalSpeaker("0", 4.0, noisy(VOICE_A)), LocalSpeaker("1", 4.0, noisy(VOICE_B))])
    result = t.assign(
        [LocalSpeaker("0", 2.0, None), LocalSpeaker("1", 2.0, None)],
        votes={"0": Counter({"S2": 5}), "1": Counter({"S1": 4})},
    )
    assert result.mapping == {"0": "S2", "1": "S1"}


def test_converged_clusters_merge_into_the_older_label() -> None:
    t = SpeakerTracker(merge_min_s=2.0)
    t.assign([LocalSpeaker("0", 6.0, VOICE_A), LocalSpeaker("1", 6.0, VOICE_B)])
    # Evidence drifts until S2's centroid is indistinguishable from S1's.
    t.speakers["S2"].centroid = t.speakers["S1"].centroid.copy()
    result = t.assign([])
    assert result.merged == (("S2", "S1"),)
    assert t.resolve("S2") == "S1"
    assert [s.label for s in t.active()] == ["S1"]
    # Later windows voting for the merged label land on the survivor.
    again = t.assign([LocalSpeaker("0", 2.0, None)], votes={"0": Counter({"S2": 5})})
    assert again.mapping == {"0": "S1"}


def test_average_embeddings_ignores_missing_and_mismatched() -> None:
    vec = average_embeddings([(None, 1.0), ([1.0, 0.0], 2.0), ([0.0, 1.0, 0.0], 1.0)])
    assert vec is not None and np.allclose(vec, [1.0, 0.0])
    assert average_embeddings([(None, 1.0)]) is None


def test_no_voiceprint_never_opens_a_speaker_and_speech_counts_once() -> None:
    t = SpeakerTracker()
    t.assign([LocalSpeaker("0", 6.0, noisy(VOICE_A))])
    blind = t.assign([LocalSpeaker("0", 6.0, None), LocalSpeaker("1", 5.0, None)])
    assert blind.created == ()  # the S7 case: a tag without an embedding joins S1
    assert set(blind.mapping.values()) == {"S1"}
    assert t.speakers["S1"].speech_seconds == 0.0  # windows overlap: not counted here
    t.add_speech("S1", 2.5)
    assert t.speakers["S1"].speech_seconds == 2.5


def _toward(base: np.ndarray, cos: float, seed: int) -> np.ndarray:
    """A unit vector at cosine `cos` to `base` (random other direction)."""
    b = base / np.linalg.norm(base)
    other = np.random.default_rng(seed).normal(size=b.shape)
    o = other - (other @ b) * b
    o /= np.linalg.norm(o)
    return cos * b + np.sqrt(1 - cos**2) * o


def _state(label: str, i: int, centroid, speech: float, seen: float):
    from app.live.speakers import SpeakerState

    return SpeakerState(
        label=label,
        color_index=i,
        centroid=None if centroid is None else centroid / np.linalg.norm(centroid),
        weight=min(speech, 120.0),
        speech_seconds=speech,
        last_seen_s=seen,
    )


def test_recluster_collapses_a_two_person_call_split_into_six_labels() -> None:
    """Synthetic two-person stress case with six initially split labels."""
    t = SpeakerTracker()
    b = VOICE_B / np.linalg.norm(VOICE_B)
    s6 = _toward(b, 0.74, 1)
    for st in [
        _state("S1", 0, VOICE_A, 490.0, 500.0),
        _state("S2", 1, b, 52.0, 498.0),
        _state("S3", 2, _toward(b, 0.53, 2), 8.1, 300.0),
        _state("S4", 3, _toward(b, 0.42, 3), 8.7, 200.0),
        _state("S5", 4, _toward(VOICE_A, 0.22, 4), 1.6, 100.0),
        _state("S6", 5, s6, 26.3, 450.0),
        _state("S7", 6, VOICE_C, 2.0, 495.0),  # just spoke: not a fragment yet
    ]:
        t.speakers[st.label] = st
    t.now_s = 500.0
    assert t.recluster()
    assert {lab: t.resolve(lab) for lab in ["S3", "S4", "S5", "S6"]} == {
        "S3": "S2",
        "S4": "S2",
        "S5": "S1",
        "S6": "S2",
    }
    assert sorted(s.label for s in t.active()) == ["S1", "S2", "S7"]
    t.now_s = 520.0  # S7 went quiet: a 2 s fragment joins its nearest speaker
    t.recluster()
    assert sorted(s.label for s in t.active()) == ["S1", "S2"]


def test_recluster_respects_the_hint_and_manual_names() -> None:
    t = SpeakerTracker(expected_speakers=2, is_locked=lambda label: label == "S3")
    for st in [
        _state("S1", 0, VOICE_A, 100.0, 99.0),
        _state("S2", 1, VOICE_B, 80.0, 99.0),
        _state("S3", 2, VOICE_C, 20.0, 99.0),  # manually named: never merged away
    ]:
        t.speakers[st.label] = st
    t.now_s = 100.0
    t.recluster()
    assert "S3" in {s.label for s in t.active()} and len(t.active()) == 2
    # A short new voice past the hint never opens a speaker.
    out = t.assign([LocalSpeaker("0", 6.0, noisy(VOICE_C) + VOICE_A * 3, last_end_s=101.0)])
    assert out.created == () and len(t.active()) == 2


def test_speaker_count_hint_merges_the_closest_pair() -> None:
    t = SpeakerTracker(expected_speakers=2, hint_merge_threshold=0.3)
    t.assign([LocalSpeaker("0", 6.0, VOICE_A), LocalSpeaker("1", 6.0, VOICE_B)])
    near_a = VOICE_A + rng.normal(size=64) * 0.9
    created = t.assign([LocalSpeaker("0", 6.0, near_a)])
    # A third voice past the hint needs to be unmistakable; else it joins S1.
    assert created.created == () and created.mapping == {"0": "S1"}
    t.speakers["S3"] = type(t.speakers["S1"])(
        label="S3", color_index=2, centroid=t.speakers["S1"].centroid.copy(), weight=6.0
    )
    result = t.assign([])
    assert ("S3", "S1") in result.merged
    assert [s.label for s in t.active()] == ["S1", "S2"]
    assert t.merge("S2", "S1") and t.resolve("S2") == "S1"
    assert t.unmerge("S2") and t.resolve("S2") == "S2"


def test_people_who_talk_to_each_other_are_not_merged_on_a_close_voiceprint() -> None:
    """Synthetic alternating voices at similar centroid cosine."""
    t = SpeakerTracker()
    b = VOICE_B / np.linalg.norm(VOICE_B)
    for st in [
        _state("S1", 0, VOICE_A, 17.6, 50.0),
        _state("S2", 1, b, 10.4, 40.0),
        _state("S3", 2, _toward(b, 0.75, 5), 23.3, 50.0),
    ]:
        t.speakers[st.label] = st
        t.present[st.label] = 8
    t.together[frozenset(("S2", "S3"))] = 6
    t.now_s = 80.0  # everyone idle, as at stop
    assert t.recluster() == []
    assert len(t.active()) == 3
    # The same voiceprints without that evidence (a split of one person) merge.
    t.together.clear()
    assert t.recluster() == [("S2", "S3")]


def test_cannot_link_survives_a_restart_rebuilt_from_segments() -> None:
    """Synthetic restart case: in-memory counts reset, so without replaying
    saved segments a recluster might merge two different voices."""
    b = VOICE_B / np.linalg.norm(VOICE_B)
    spans = []
    t = 0.0
    while t < 240.0:  # S1 and S2 alternate 3-s turns for four minutes
        spans.append(("S1", t, t + 2.5))
        spans.append(("S2", t + 3.0, t + 5.5))
        t += 6.0
    spans.append(("S3", 123.0, 124.0))  # a 1-s blip: never cannot-linked

    def fresh() -> SpeakerTracker:
        tr = SpeakerTracker()
        tr.speakers["S1"] = _state("S1", 0, _toward(b, 0.87, 3), 53.0, 120.0)
        tr.speakers["S2"] = _state("S2", 1, b, 260.0, 120.0)
        tr.speakers["S3"] = _state("S3", 2, _toward(b, 0.2, 4), 1.0, 124.0)
        tr.now_s = 260.0
        return tr

    restarted = fresh()  # what the old reload did: no evidence -> merged
    assert ("S1", "S2") in restarted.recluster()
    t2 = fresh()
    t2.rebuild_cannot_link(spans, window_s=30.0, step_s=3.0)
    assert t2.apart("S1", "S2")
    assert not t2.apart("S1", "S3") and not t2.apart("S2", "S3")
    # The blip still folds into its nearest speaker; the two people stay apart.
    assert t2.recluster() == [("S3", "S2")]
    assert {st.label for st in t2.active()} == {"S1", "S2"}
