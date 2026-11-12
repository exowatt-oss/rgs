"""Optional Nemotron-compatible ASR client for Live Record.

Transport contract: see `docs/providers/nemotron.md`. One InvokeEndpoint,
`application/json`, three body shapes routed by the container:

1. chat — an OpenAI chat-completions body (has `messages`); response is the
   vLLM JSON incl. top-level `ctc_timestamps` when requested;
2. `{"task": "speaker_embed", "audio_b64", "segments": [[s, e], ...]}` →
   `{"model", "dim", "embeddings": [[...] | null, ...]}`;
3. `{"task": "health"}` → `{"ok": true, "model_loaded": true}`.

Everything here is synchronous (HTTP or optional boto3); callers use a worker thread.
The transport is injectable so tests never touch AWS.
"""

from __future__ import annotations

import base64
import io
import json
import re
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from app.live.settings import LiveSettings, get_live_settings

log = structlog.get_logger(__name__)

SAMPLE_RATE = 16000


def transcribe_prompt(speaker_tags: int = 4) -> str:
    """Model-card transcription prompt with `speaker_tags` diarization tags.

    Batch ASR asks for 8 tags when more than 4 people are invited;
    Live Record keeps the 4-tag default.
    """
    tags = " ".join(f"<spk:{i}>" for i in range(max(1, speaker_tags)))
    return (
        "Transcribe this audio with punctuation and capitalization. Include all spoken words "
        "and verbal fillers. Write numbers in their standard written form. Put speaker tags "
        f"{tags} on the transcription."
    )


TRANSCRIBE_PROMPT = transcribe_prompt(4)

_SPK_TAG_RE = re.compile(r"<spk:(\d+)>")
_ANY_TAG_RE = re.compile(r"<[^<>\s]{1,32}>")


class NemotronError(RuntimeError):
    """The endpoint call failed (transport, HTTP, model or parse)."""


class NemotronTransport(Protocol):
    def invoke(self, body: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        """POST one JSON body to the endpoint and return the decoded JSON."""
        ...


@dataclass(frozen=True)
class Word:
    """A word with times relative to the submitted window audio."""

    text: str
    start: float
    end: float
    speaker: str  # window-local speaker tag ("0", "1", ...)


@dataclass(frozen=True)
class WindowTranscript:
    text: str
    words: tuple[Word, ...]
    aligned: bool  # False when CTC timestamps were missing/failed (times estimated)
    diarization: tuple[tuple[float, float, str], ...] = field(default_factory=tuple)
    error: str | None = None
    # `choices[0].finish_reason`. "length" means the decoder hit max_tokens —
    # on long audio that is the repetition-loop failure mode.
    finish_reason: str | None = None


class HttpTransport:
    """Direct HTTP transport to a router exposing the SageMaker contract
    (`POST {base}/invocations`)."""

    def __init__(self, settings: LiveSettings | None = None) -> None:
        self.settings = settings or get_live_settings()

    def invoke(self, body: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.settings.nemotron_api_key:
            headers["Authorization"] = f"Bearer {self.settings.nemotron_api_key}"
        url = self.settings.nemotron_base_url.rstrip("/") + "/invocations"
        try:
            response = httpx.post(
                url, json=body, headers=headers, timeout=httpx.Timeout(timeout_s, connect=5.0)
            )
            response.raise_for_status()
            decoded = response.json()
        except Exception as exc:
            raise NemotronError(f"http invoke failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(decoded, dict):
            raise NemotronError("endpoint returned a non-object")
        return decoded


def default_transport(settings: LiveSettings) -> NemotronTransport:
    if settings.nemotron_base_url:
        return HttpTransport(settings)
    return SageMakerTransport(settings)


class SageMakerTransport:
    """boto3 `sagemaker-runtime` InvokeEndpoint transport."""

    def __init__(self, settings: LiveSettings | None = None) -> None:
        self.settings = settings or get_live_settings()
        self._clients: dict[float, Any] = {}

    def _client(self, timeout_s: float) -> Any:
        client = self._clients.get(timeout_s)
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "sagemaker-runtime",
                region_name=self.settings.nemotron_aws_region,
                aws_access_key_id=self.settings.nemotron_aws_access_key_id or None,
                aws_secret_access_key=self.settings.nemotron_aws_secret_access_key or None,
                config=Config(
                    connect_timeout=5,
                    read_timeout=timeout_s,
                    # Live windows are retried by the next tick, never here.
                    retries={"max_attempts": 1, "mode": "standard"},
                    max_pool_connections=16,
                ),
            )
            self._clients[timeout_s] = client
        return client

    def invoke(self, body: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        try:
            response = self._client(timeout_s).invoke_endpoint(
                EndpointName=self.settings.nemotron_endpoint_name,
                ContentType="application/json",
                Accept="application/json",
                Body=json.dumps(body).encode(),
            )
            raw = response["Body"].read()
        except Exception as exc:  # botocore raises many shapes; all mean "call failed"
            raise NemotronError(f"invoke_endpoint failed: {type(exc).__name__}: {exc}") from exc
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise NemotronError("endpoint returned non-JSON") from exc
        if not isinstance(decoded, dict):
            raise NemotronError("endpoint returned a non-object")
        return decoded


def pcm16_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


class NemotronClient:
    def __init__(
        self,
        transport: NemotronTransport | None = None,
        settings: LiveSettings | None = None,
    ) -> None:
        self.settings = settings or get_live_settings()
        self.transport = transport or default_transport(self.settings)

    # ---- chat transcription -------------------------------------------

    def build_transcribe_body(
        self,
        wav_bytes: bytes,
        *,
        context_terms: list[str] | None = None,
        mime: str = "audio/wav",
        speaker_tags: int = 4,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        prompt = transcribe_prompt(speaker_tags)
        terms = [t.strip() for t in (context_terms or []) if t and t.strip()]
        if terms:
            # Model-card "Domain vocabulary" contextual prompt.
            prompt = f"{prompt} Context terms: {', '.join(terms[:60])}."
        audio = base64.b64encode(wav_bytes).decode()
        return {
            "model": self.settings.nemotron_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "audio_url",
                            "audio_url": {"url": f"data:{mime};base64,{audio}"},
                        },
                    ],
                }
            ],
            "temperature": 0,
            "top_p": 1.0,
            "seed": 0,
            "max_tokens": max_tokens or self.settings.live_nemotron_max_tokens,
            "stream": False,
            "n": 1,
            "skip_special_tokens": False,
            "spaces_between_special_tokens": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "mm_processor_kwargs": {"capture_ctc_timestamps": True},
        }

    def transcribe(
        self,
        pcm: bytes,
        *,
        sample_rate: int = SAMPLE_RATE,
        context_terms: list[str] | None = None,
        speaker_tags: int = 4,
    ) -> WindowTranscript:
        duration = len(pcm) / 2 / sample_rate
        body = self.build_transcribe_body(
            pcm16_to_wav(pcm, sample_rate), context_terms=context_terms, speaker_tags=speaker_tags
        )
        response = self.transport.invoke(body, timeout_s=self.settings.live_nemotron_timeout_s)
        result = parse_transcription(response, duration)
        if not self.settings.live_turn_split_enabled or not needs_turn_split(result):
            return result
        try:
            return self._transcribe_turns(pcm, result, sample_rate, context_terms)
        except Exception as exc:  # noqa: BLE001 — keep the single-pass result
            log.info("nemotron.turn_split_failed", error=str(exc)[:200])
            return result

    def _transcribe_turns(
        self,
        pcm: bytes,
        whole: WindowTranscript,
        sample_rate: int,
        context_terms: list[str] | None,
    ) -> WindowTranscript:
        """Re-transcribe each diarization turn on its own (see `needs_turn_split`).

        Each turn is one speaker, so the router's CTC alignment is reliable and
        the words take the turn's diarization speaker. Turn audio is padded by
        up to `_TURN_PAD_S` (never past the midpoint to a neighbouring turn).
        """
        duration = len(pcm) / 2 / sample_rate
        clips = turn_clips(whole.diarization, duration)

        def run(clip: tuple[float, float, str]) -> list[Word]:
            start, end, tag = clip
            a = int(start * sample_rate) * 2
            b = int(end * sample_rate) * 2
            body = self.build_transcribe_body(
                pcm16_to_wav(pcm[a:b], sample_rate), context_terms=context_terms
            )
            response = self.transport.invoke(body, timeout_s=self.settings.live_nemotron_timeout_s)
            part = parse_transcription(response, (b - a) / 2 / sample_rate)
            return [
                Word(
                    text=w.text,
                    start=round(start + w.start, 3),
                    end=round(start + w.end, 3),
                    speaker=tag,
                )
                for w in part.words
            ]

        with ThreadPoolExecutor(max_workers=min(4, len(clips))) as pool:
            parts = list(pool.map(run, clips))
        words = [w for part in parts for w in part]
        words.sort(key=lambda w: (w.start, w.end))
        return WindowTranscript(
            text=" ".join(w.text for w in words),
            words=tuple(words),
            aligned=True,
            diarization=whole.diarization,
            finish_reason=whole.finish_reason,
        )

    def transcribe_encoded(
        self,
        audio: bytes,
        *,
        duration: float,
        mime: str,
        timeout_s: float,
        max_tokens: int,
        speaker_tags: int = 4,
        context_terms: list[str] | None = None,
    ) -> WindowTranscript:
        """Batch variant: pre-encoded audio (e.g. FLAC, half of
        WAV) and caller-chosen time/token budgets for long windows."""
        body = self.build_transcribe_body(
            audio,
            context_terms=context_terms,
            mime=mime,
            speaker_tags=speaker_tags,
            max_tokens=max_tokens,
        )
        response = self.transport.invoke(body, timeout_s=timeout_s)
        return parse_transcription(response, duration)

    # ---- speaker embeddings -------------------------------------------

    def speaker_embed(
        self,
        pcm: bytes,
        segments: list[tuple[float, float]],
        *,
        sample_rate: int = SAMPLE_RATE,
        encoded_audio: bytes | None = None,
        timeout_s: float | None = None,
    ) -> list[list[float] | None]:
        """TitaNet vectors for `segments` (seconds into the audio).

        `encoded_audio` (any container the router decodes, e.g. FLAC)
        replaces `pcm` when given — the batch path sends its window FLAC.
        """
        if not segments:
            return []
        audio = encoded_audio if encoded_audio is not None else pcm16_to_wav(pcm, sample_rate)
        body = {
            "task": "speaker_embed",
            "audio_b64": base64.b64encode(audio).decode(),
            "segments": [[round(s, 3), round(e, 3)] for s, e in segments],
        }
        response = self.transport.invoke(
            body, timeout_s=timeout_s or self.settings.live_nemotron_timeout_s
        )
        embeddings = response.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(segments):
            raise NemotronError("speaker_embed returned a mismatched embeddings list")
        out: list[list[float] | None] = []
        for vec in embeddings:
            if isinstance(vec, list) and vec and all(isinstance(x, int | float) for x in vec):
                out.append([float(x) for x in vec])
            else:
                out.append(None)
        return out

    def health(self) -> dict[str, Any]:
        return self.transport.invoke({"task": "health"}, timeout_s=10.0)


# ---- response parsing (pure) ------------------------------------------


def _speaker_tag(raw: Any) -> str:
    if raw is None:
        return "0"
    if isinstance(raw, int):
        return str(raw)
    text = str(raw).strip()
    match = re.search(r"(\d+)", text)
    return match.group(1) if match else (text or "0")


def strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", _ANY_TAG_RE.sub(" ", text)).strip()


def _words_from_text(text: str, duration: float) -> list[Word]:
    """Fallback when alignment failed: spread tagged words evenly over the window.

    Times are estimates; the caller marks the window `aligned=False` so the
    committer can be more conservative.
    """
    tokens: list[tuple[str, str]] = []
    speaker = "0"
    for piece in re.split(r"(<spk:\d+>)", text):
        tag = _SPK_TAG_RE.fullmatch(piece)
        if tag:
            speaker = tag.group(1)
            continue
        for token in strip_tags(piece).split():
            tokens.append((token, speaker))
    if not tokens:
        return []
    step = max(duration, 0.1) / len(tokens)
    return [
        Word(text=tok, start=i * step, end=(i + 1) * step, speaker=spk)
        for i, (tok, spk) in enumerate(tokens)
    ]


_MIN_TURN_S = 0.3
_TURN_PAD_S = 0.25
_MIN_SPLIT_TURN_S = 0.4


def _diarization_turns(
    diarization: tuple[tuple[float, float, str], ...] | list[tuple[float, float, str]],
) -> list[tuple[str, list[tuple[float, float]]]]:
    """Consecutive same-speaker diarization segments merged into turns.

    Turns shorter than `_MIN_TURN_S` are folded into the previous turn (a
    diarization blip is not a speaker change).
    """
    turns: list[tuple[str, list[tuple[float, float]]]] = []
    for start, end, tag in sorted(diarization, key=lambda d: (d[0], d[1])):
        if end <= start:
            continue
        if turns and (turns[-1][0] == tag or end - start < _MIN_TURN_S):
            turns[-1][1].append((start, end))
        else:
            turns.append((tag, [(start, end)]))
    return turns


def needs_turn_split(result: WindowTranscript) -> bool:
    """True when the model wrote no speaker tags but diarization heard >= 2 speakers.

    The router force-aligns words inside the diarization regions of the
    *tagged* speaker, so an untagged multi-speaker hypothesis gets everyone's
    words squashed into speaker 0's regions (in some diarization outputs, a second
    speaker's sentence aligned into 0.7 s, 8 s gaps over real speech) and the
    window collapses to one speaker.
    """
    if not result.words or len({w.speaker for w in result.words}) != 1:
        return False
    turns = _diarization_turns(result.diarization)
    return len({tag for tag, _ in turns}) >= 2


def turn_clips(
    diarization: tuple[tuple[float, float, str], ...], duration: float
) -> list[tuple[float, float, str]]:
    """`(start, end, speaker)` audio clips, one per diarization turn, padded."""
    turns = [(tag, segs[0][0], segs[-1][1]) for tag, segs in _diarization_turns(diarization)]
    # Turns too short to transcribe on their own join the previous turn.
    merged: list[list] = []
    for tag, start, end in turns:
        if merged and end - start < _MIN_SPLIT_TURN_S:
            merged[-1][2] = max(merged[-1][2], end)
        else:
            merged.append([tag, start, end])
    clips: list[tuple[float, float, str]] = []
    for k, (tag, start, end) in enumerate(merged):
        lo = 0.0 if k == 0 else (merged[k - 1][2] + start) / 2
        hi = duration if k == len(merged) - 1 else (end + merged[k + 1][1]) / 2
        # The window edges cut speech mid-word: the first/last clip runs to them.
        clip_start = 0.0 if k == 0 else max(lo, start - _TURN_PAD_S, 0.0)
        clip_end = duration if k == len(merged) - 1 else min(hi, end + _TURN_PAD_S, duration)
        if clip_end - clip_start > 0.05:
            clips.append((round(clip_start, 3), round(clip_end, 3), tag))
    return clips


def parse_transcription(response: dict[str, Any], duration: float) -> WindowTranscript:
    try:
        content = response["choices"][0]["message"]["content"] or ""
        finish_reason = response["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        if response.get("error"):
            raise NemotronError(f"endpoint error: {response['error']}") from exc
        raise NemotronError("chat response has no choices[0].message.content") from exc
    if not isinstance(content, str):
        raise NemotronError("chat content is not a string")
    plain = strip_tags(content)
    ctc = response.get("ctc_timestamps")
    diarization: list[tuple[float, float, str]] = []
    if isinstance(ctc, dict):
        for seg in ctc.get("diarization") or []:
            try:
                if isinstance(seg, dict):
                    diarization.append(
                        (float(seg["start"]), float(seg["end"]), _speaker_tag(seg.get("speaker")))
                    )
                elif isinstance(seg, list | tuple) and len(seg) >= 3:
                    diarization.append((float(seg[0]), float(seg[1]), _speaker_tag(seg[2])))
            except (KeyError, TypeError, ValueError):
                continue
    error = ctc.get("error") if isinstance(ctc, dict) else "no ctc_timestamps in response"
    raw_words = ctc.get("words") if isinstance(ctc, dict) else None
    words: list[Word] = []
    if not error and isinstance(raw_words, list):
        for item in raw_words:
            if not isinstance(item, dict):
                continue
            text = strip_tags(str(item.get("word") or ""))
            if not text:
                continue
            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end < start:
                start, end = end, start
            words.append(
                Word(
                    text=text,
                    start=max(0.0, start),
                    end=min(max(end, start), max(duration, end)),
                    speaker=_speaker_tag(item.get("speaker")),
                )
            )
        words.sort(key=lambda w: (w.start, w.end))
        return WindowTranscript(
            text=plain,
            words=tuple(words),
            aligned=True,
            diarization=tuple(diarization),
            finish_reason=str(finish_reason) if finish_reason else None,
        )
    return WindowTranscript(
        text=plain,
        words=tuple(_words_from_text(content, duration)),
        aligned=False,
        diarization=tuple(diarization),
        error=str(error) if error else None,
        finish_reason=str(finish_reason) if finish_reason else None,
    )
