"""Nemotron client request/response shapes (design notes; docs/providers/nemotron.md)."""

from __future__ import annotations

import base64
import io
import wave

import pytest

from app.live.nemotron import NemotronClient, NemotronError, parse_transcription
from app.live.settings import LiveSettings


class RecordingTransport:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.bodies: list[dict] = []

    def invoke(self, body: dict, *, timeout_s: float) -> dict:
        self.bodies.append(body)
        assert 0 < timeout_s <= 60
        return self.response


def _chat(content: str, ctc: dict | None) -> dict:
    out = {"choices": [{"message": {"content": content}}]}
    if ctc is not None:
        out["ctc_timestamps"] = ctc
    return out


def test_transcribe_body_matches_contract_and_parses_words() -> None:
    transport = RecordingTransport(
        _chat(
            "<spk:0> Hello there. <spk:1> Hi!",
            {
                "words": [
                    {"word": "Hello", "start": 0.1, "end": 0.4, "speaker": "spk:0"},
                    {"word": "there.", "start": 0.5, "end": 0.9, "speaker": 0},
                    {"word": "Hi!", "start": 1.2, "end": 1.5, "speaker": "<spk:1>"},
                ],
                "diarization": [
                    {"start": 0.0, "end": 1.0, "speaker": 0},
                    {"start": 1.1, "end": 1.6, "speaker": 1},
                ],
                "speaker_tag_to_diarization_speaker": {"0": 0, "1": 1},
                "error": None,
            },
        )
    )
    client = NemotronClient(transport=transport, settings=LiveSettings(live_record_enabled=True))
    pcm = b"\x00\x00" * 16000
    result = client.transcribe(pcm, context_terms=["sample"])

    body = transport.bodies[0]
    assert body["stream"] is False and body["n"] == 1
    assert body["skip_special_tokens"] is False
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["mm_processor_kwargs"] == {"capture_ctc_timestamps": True}
    content = body["messages"][0]["content"]
    assert "<spk:0>" in content[0]["text"] and "Context terms: sample" in content[0]["text"]
    url = content[1]["audio_url"]["url"]
    assert url.startswith("data:audio/wav;base64,")
    with wave.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getnframes()) == (16000, 1, 16000)

    assert result.aligned and result.text == "Hello there. Hi!"
    assert [(x.text, x.speaker) for x in result.words] == [
        ("Hello", "0"),
        ("there.", "0"),
        ("Hi!", "1"),
    ]
    assert result.diarization[1] == (1.1, 1.6, "1")


def test_alignment_error_falls_back_to_estimated_times() -> None:
    result = parse_transcription(
        _chat("<spk:0> one two <spk:1> three", {"words": [], "error": "alignment failed"}), 3.0
    )
    assert not result.aligned and result.error == "alignment failed"
    assert [(x.text, x.speaker) for x in result.words] == [
        ("one", "0"),
        ("two", "0"),
        ("three", "1"),
    ]
    assert result.words[-1].end == pytest.approx(3.0)


def test_malformed_response_raises() -> None:
    with pytest.raises(NemotronError):
        parse_transcription({"error": "model not loaded"}, 1.0)


def test_speaker_embed_shape() -> None:
    transport = RecordingTransport({"model": "titanet", "dim": 3, "embeddings": [[1, 0, 0], None]})
    client = NemotronClient(transport=transport, settings=LiveSettings())
    out = client.speaker_embed(b"\x00\x00" * 32000, [(0.0, 1.0), (1.0, 1.2)])
    assert out == [[1.0, 0.0, 0.0], None]
    assert transport.bodies[0]["task"] == "speaker_embed"
    assert transport.bodies[0]["segments"] == [[0.0, 1.0], [1.0, 1.2]]

    bad = NemotronClient(
        transport=RecordingTransport({"embeddings": [[1.0]]}), settings=LiveSettings()
    )
    with pytest.raises(NemotronError):
        bad.speaker_embed(b"\x00\x00" * 100, [(0.0, 0.1), (0.1, 0.2)])


def test_untagged_multi_speaker_window_is_retranscribed_per_turn() -> None:
    import base64
    import struct

    from app.live.nemotron import NemotronClient, turn_clips
    from app.live.settings import LiveSettings

    def response(content: str, words: list[tuple[str, float, float]], diar: list) -> dict:
        return {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "ctc_timestamps": {
                "words": [
                    {"word": w, "start": a, "end": b, "speaker": "spk:0"} for w, a, b in words
                ],
                "diarization": diar,
                "error": None,
            },
        }

    diar = [[0.0, 1.9, 0], [2.5, 3.4, 1], [3.6, 5.5, 1], [6.2, 7.5, 0]]
    whole = response(
        "<spk:0> Hi, I'm Alice. Thanks Alice, I'm Bob. Dana owns it.",
        # The router squashes Bob's words into speaker 0's regions.
        [
            ("Hi,", 0.0, 0.4),
            ("I'm", 0.5, 0.8),
            ("Alice.", 0.8, 1.6),
            ("Thanks", 1.7, 1.75),
            ("Alice,", 1.75, 1.8),
            ("I'm", 1.8, 1.85),
            ("Bob.", 1.85, 1.9),
            ("Dana", 6.2, 6.6),
            ("owns", 6.6, 7.0),
            ("it.", 7.0, 7.4),
        ],
        diar,
    )
    # One response per turn clip, keyed by the clip's first sample value.
    per_clip = {
        1: response("Hi, I'm Alice.", [("Hi,", 0.0, 0.4), ("Alice.", 0.8, 1.6)], [[0, 1.9, 0]]),
        2: response("Thanks Alice, I'm Bob.", [("Thanks", 0.4, 0.8), ("Bob.", 1.9, 2.6)], []),
        3: response("Dana owns it.", [("Dana", 0.4, 0.8), ("it.", 1.2, 1.6)], [[0.3, 1.6, 0]]),
    }
    calls: list[float] = []

    class Fake:
        def invoke(self, body: dict, *, timeout_s: float) -> dict:
            url = body["messages"][0]["content"][1]["audio_url"]["url"]
            wav = base64.b64decode(url.split(",", 1)[1])
            dur = round((len(wav) - 44) / 32000, 2)
            calls.append(dur)
            if dur >= 8:
                return whole
            return per_clip[struct.unpack("<h", wav[44:46])[0]]

    assert turn_clips(tuple((a, b, str(t)) for a, b, t in diar), 8.0) == [
        (0.0, 2.15, "0"),
        (2.25, 5.75, "1"),
        (5.95, 8.0, "0"),
    ]
    pcm = b"".join(
        struct.pack("<h", 1 if i < 2.2 * 16000 else 2 if i < 5.8 * 16000 else 3)
        for i in range(8 * 16000)
    )
    client = NemotronClient(transport=Fake(), settings=LiveSettings())
    result = client.transcribe(pcm)
    assert sorted(calls) == [2.05, 2.15, 3.5, 8.0]
    assert [(w.text, w.start, w.speaker) for w in result.words] == [
        ("Hi,", 0.0, "0"),
        ("Alice.", 0.8, "0"),
        ("Thanks", 2.65, "1"),
        ("Bob.", 4.15, "1"),
        ("Dana", 6.35, "0"),
        ("it.", 7.15, "0"),
    ]
    assert result.aligned and result.diarization == whole_diar(diar)

    # Tagged output, or a single diarization speaker, is used as-is.
    calls.clear()
    whole = response("<spk:0> Hi, I'm Alice.", [("Hi,", 0.0, 0.4)], [[0.0, 1.9, 0], [2, 3, 0]])
    single = client.transcribe(pcm)
    assert calls == [8.0] and [w.text for w in single.words] == ["Hi,"]
    # The feature flag turns it off.
    whole = response(whole_content := "<spk:0> a b", [("a", 0, 1), ("b", 3, 4)], diar)
    calls.clear()
    off = NemotronClient(transport=Fake(), settings=LiveSettings(live_turn_split_enabled=False))
    assert [w.text for w in off.transcribe(pcm).words] == ["a", "b"] and calls == [8.0]
    assert whole_content


def whole_diar(diar: list) -> tuple:
    return tuple((float(a), float(b), str(t)) for a, b, t in diar)
