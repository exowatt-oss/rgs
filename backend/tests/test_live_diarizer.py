"""Pure tests for the pluggable speaker layer (`app.live.diarizer`)."""

from __future__ import annotations

import numpy as np

from app.live.diarizer import (
    DiarLinker,
    assign_window,
    load_diarizer,
    relabel_window,
    tune_tracker,
    word_tags,
)
from app.live.nemotron import Word
from app.live.settings import LiveSettings
from app.live.speakers import LocalSpeaker, SpeakerTracker

rng = np.random.default_rng(11)
BASE = rng.normal(size=64)
# Two similar voices (cosine ~0.6): the tracker alone would never split them.
VOICE_A = BASE + rng.normal(size=64) * 0.8
VOICE_B = BASE + rng.normal(size=64) * 0.8


def unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def test_similar_voices_fixture_is_hard() -> None:
    assert 0.4 < float(unit(VOICE_A) @ unit(VOICE_B)) < 0.75


def test_word_tags_overlap_nearest_and_carry() -> None:
    words = [
        Word("hi", 0.0, 0.4, "0"),
        Word("there", 0.5, 0.9, "0"),
        Word("yes", 2.0, 2.3, "0"),
        Word("far", 9.0, 9.2, "0"),
    ]
    turns = [(0.0, 0.8, "A"), (0.7, 1.5, "B"), (2.6, 3.0, "B")]
    # most overlap; nearest turn within 1 s; else the previous word's label
    assert word_tags(words, turns) == ["A", "A", "B", "B"]


def test_relabel_window_is_window_relative() -> None:
    words = [Word("one", 0.2, 0.5, "0"), Word("two", 3.0, 3.4, "0")]
    turns_abs = [(95.0, 100.6, "D0"), (102.5, 104.0, "D1")]
    relabelled, spans = relabel_window(words, turns_abs, start=100.0, end=105.0)
    assert [w.speaker for w in relabelled] == ["D0", "D1"]
    assert [w.text for w in relabelled] == ["one", "two"]
    assert [t for _s, _e, t in spans] == ["D0", "D1"]
    assert [round(x, 3) for s, e, _t in spans for x in (s, e)] == [0.0, 0.6, 2.5, 4.0]


def test_linker_keeps_similar_voices_apart_and_stable() -> None:
    tracker = SpeakerTracker()
    linker = DiarLinker()
    # Window 1 (0-10 s): A talks, then B answers; the diarizer separates them.
    turns = [(0.0, 4.0, "D0"), (4.5, 9.0, "D1")]
    locs = [LocalSpeaker("D0", 4.0, unit(VOICE_A)), LocalSpeaker("D1", 4.5, unit(VOICE_B))]
    a1 = assign_window(tracker, linker, turns, locs, head_s=10.0, flush=False)
    assert sorted(a1.created) == ["S1", "S2"]
    a_lab, b_lab = a1.mapping["D0"], a1.mapping["D1"]
    # Window 2: the stream keeps each person in their slot (pinned), even
    # where the voiceprint alone is ambiguous.
    turns = [(4.5, 9.0, "D1"), (10.0, 12.5, "D0"), (12.6, 14.0, "D1")]
    locs = [
        LocalSpeaker("D0", 2.5, unit(VOICE_B)),  # misleading voiceprint
        LocalSpeaker("D1", 1.4, unit(VOICE_B)),
    ]
    a2 = assign_window(tracker, linker, turns, locs, head_s=15.0, flush=False)
    assert a2.created == ()
    assert a2.mapping == {"D0": a_lab, "D1": b_lab}
    # Stream restarted (sidecar restart): new slot numbers relink by overlap.
    linker.reset()
    turns = [(4.5, 9.0, "D0"), (10.0, 12.5, "D1"), (15.0, 17.0, "D1")]
    locs = [LocalSpeaker("D1", 2.0, unit(VOICE_A + rng.normal(size=64) * 0.3))]
    a3 = assign_window(tracker, linker, turns, locs, head_s=18.0, flush=False)
    assert a3.created == ()
    assert a3.mapping == {"D0": b_lab, "D1": a_lab}
    assert len(tracker.active()) == 2


def test_linker_new_voice_needs_fresh_speech() -> None:
    tracker = SpeakerTracker()
    linker = DiarLinker()
    assign_window(
        tracker,
        linker,
        [(0.0, 8.0, "D0")],
        [LocalSpeaker("D0", 8.0, unit(VOICE_A))],
        head_s=10.0,
        flush=False,
    )
    # A 0.5 s blip in a new label is too short to be someone new.
    blip = assign_window(
        tracker,
        linker,
        [(0.0, 8.0, "D0"), (10.5, 11.0, "D1")],
        [LocalSpeaker("D1", 0.5, unit(VOICE_B))],
        head_s=12.0,
        flush=False,
    )
    assert blip.created == ()
    assert blip.mapping["D1"] == "S1"


def test_linker_short_clear_voiceprint_is_not_new_and_not_pinned() -> None:
    tracker = SpeakerTracker()
    linker = DiarLinker()
    assign_window(
        tracker,
        linker,
        [(0.0, 8.0, "D0")],
        [LocalSpeaker("D0", 8.0, unit(VOICE_A))],
        head_s=10.0,
        flush=False,
    )
    linker.reset()  # restarted stream, no overlap with linked audio
    a = assign_window(
        tracker,
        linker,
        [(20.0, 22.0, "D2")],
        [LocalSpeaker("D2", 0.6, unit(VOICE_A))],
        head_s=23.0,
        flush=False,
    )
    assert a.created == ()
    assert a.mapping == {"D2": "S1"}
    assert "D2" not in linker.pinned


def test_tune_tracker_and_provider_setting() -> None:
    settings = LiveSettings(live_diarizer="sortformer", nemotron_base_url="http://gpu.example")
    tracker = SpeakerTracker()
    tune_tracker(tracker, settings)
    assert tracker.merge_threshold == settings.live_diarizer_merge_threshold
    client = load_diarizer(settings)
    assert client is not None and client.url == "http://gpu.example"
    assert load_diarizer(LiveSettings(live_diarizer="nemotron")) is None
