"""Per-session live runtime and process-wide manager.

One `LiveRuntime` per live session in this process, created when the owner's
audio connects (or on stop, to flush). It owns:

* the in-memory PCM tail (the left context of the transcription window) and
  the durable audio ledger (RGS keeps no audio, only the transcript);
* the transcription loop (rolling window → `Committer` → segments);
* the speaker tracker (window tags → stable session labels);
* the optional gate loop (`app.live.gate`) and the built-in LLM answerer
  (`app.live.answer`);
* speaker naming (`app.live.naming` via `app.live.name_pass`): self-
  introductions, names said to someone ("thanks Alice") and a periodic LLM pass
  turn labels into optionally configured local people, merge labels that are one
  person, and run once more at stop (never over a manual name).

Blocking work (Nemotron calls, DB writes, LLM calls) is always offloaded
to worker threads so the event loop (and `/api/healthz`) never stalls.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import anyio
import numpy as np
import structlog

from app.db import SessionLocal
from app.live import intro, name_pass, service
from app.live.broker import LiveBroker
from app.live.broker import broker as default_broker
from app.live.diarizer import DiarLinker, assign_window, load_diarizer, relabel_window, tune_tracker
from app.live.gate import (
    GateSegment,
    GateSessionContext,
    GateTick,
    GateTrigger,
    LiveGate,
    load_gate,
)
from app.live.nemotron import NemotronClient, NemotronError, WindowTranscript
from app.live.people import Person, Priors, get_directory, name_mentions
from app.live.settings import LiveSettings, get_live_settings
from app.live.speakers import LocalSpeaker, SpeakerTracker, average_embeddings
from app.live.utterances import coalesce
from app.live.windowing import (
    Committer,
    TimedWord,
    join_words,
    normalise,
    plan_window,
    segment_words,
)

log = structlog.get_logger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
# Untranscribed audio kept in memory before the oldest is skipped (and the
# stop path falls back to batch ASR for full coverage).
MAX_BACKLOG_S = 600.0
ACK_INTERVAL_S = 1.0
VOTE_TOLERANCE_S = 0.35
# Naming: how far back a cue check looks, and the minimum gap after an intro cue.
NAMING_CUE_LOOKBACK = 6
NAMING_INTRO_GAP_S = 2.0


class AudioGap(Exception):
    """The client must resend from `received` (gap or out-of-range offset)."""

    def __init__(self, received: int) -> None:
        super().__init__(f"expected offset {received}")
        self.received = received


class NotLive(Exception):
    """The session no longer accepts audio (stopping / finalized / closed)."""


@dataclass
class _Label:
    """A word on the timeline with its global label (for overlap votes)."""

    word: TimedWord
    norm: str


class LiveRuntime:
    def __init__(
        self,
        *,
        manager: LiveManager,
        session_id: UUID,
        starter_id: UUID,
        title: str,
        attendees: list[str],
        audio_samples: int,
        chunk_count: int,
        committed_until: float,
        last_final_seq: int,
        tracker: SpeakerTracker,
        tail_pcm: bytes = b"",
        tail_start_sample: int | None = None,
        gate_provider: str | None = None,
        expected_speakers: int | None = None,
    ) -> None:
        self.manager = manager
        self.settings = manager.settings
        self.session_id = session_id
        self.meeting_id: UUID | None = None  # gate context compatibility
        self.starter_id = starter_id
        self.title = title
        self.attendees = attendees
        self.gate_provider = gate_provider
        self.expected_speakers = expected_speakers
        self.chunk_samples = int(self.settings.live_chunk_seconds * SAMPLE_RATE)
        # Audio buffer covers samples [buf_start, received).
        self.persisted = audio_samples
        self.chunk_count = chunk_count
        self.buf_start = tail_start_sample if tail_start_sample is not None else audio_samples
        self.buf = bytearray(tail_pcm)
        self.received = self.buf_start + len(self.buf) // BYTES_PER_SAMPLE
        if self.received < audio_samples:  # tail restore failed/short
            self.buf_start = audio_samples
            self.buf = bytearray()
            self.received = audio_samples
        self.committer = Committer(
            stable_horizon_s=self.settings.live_stable_horizon_s,
            force_horizon_s=self.settings.live_force_horizon_s,
            committed_until=max(committed_until, self.buf_start / SAMPLE_RATE),
        )
        self.skipped_s = max(0.0, self.buf_start / SAMPLE_RATE - committed_until)
        self.tracker = tracker
        self.diar_linker = DiarLinker()
        self.diar_sent: int | None = None  # samples streamed to the diarizer
        self.last_final_seq = last_final_seq
        self.labelled: deque[_Label] = deque(maxlen=600)
        self.recent_segments: deque[GateSegment] = deque(
            maxlen=self.settings.live_gate_context_segments
        )
        self.gate_seen_seq = last_final_seq
        self.interim_text = ""
        self.asr_failures = 0
        self.asr_state = "ok"
        self.flushing = False
        self.closed = False
        self.incomplete = False
        self.audio_conn: object | None = None
        self.audio_close: Callable[[int, str], Any] | None = None
        self.last_audio_monotonic = time.monotonic()
        self._asr_wake = asyncio.Event()
        self._persist_wake = asyncio.Event()
        self._gate_wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()
        self._asr_done = asyncio.Event()
        self._persist_lock = asyncio.Lock()
        # Naming state.
        self._naming_wake = asyncio.Event()
        self._naming_cue = False  # a name cue arrived: run soon, LLM due
        self.named_through_seq = -1  # deterministic pass ran over segments up to here
        self.llm_named_through_seq = -1
        self.last_llm_at: float | None = None
        self.last_naming_at: float | None = None
        self.people: list[Person] | None = None

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._asr_loop(), name=f"live-asr-{self.session_id}"),
            asyncio.create_task(self._persist_loop(), name=f"live-persist-{self.session_id}"),
        ]
        if self.manager.gate is not None:
            self._tasks.append(
                asyncio.create_task(self._gate_loop(), name=f"live-gate-{self.session_id}")
            )
        if self.settings.live_naming_enabled:
            self._tasks.append(
                asyncio.create_task(self._naming_loop(), name=f"live-naming-{self.session_id}")
            )

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def close(self) -> None:
        self.closed = True
        self._asr_wake.set()
        self._persist_wake.set()
        self._gate_wake.set()
        self._naming_wake.set()
        for task in [*self._tasks, *self._bg]:
            if not task.done():
                task.cancel()
        for task in [*self._tasks, *self._bg]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ---- audio -------------------------------------------------------------

    def attach_audio(self, conn: object, close: Callable[[int, str], Any]) -> None:
        old_close = self.audio_close
        self.audio_conn = conn
        self.audio_close = close
        if old_close is not None:
            self.spawn(_maybe_await(old_close(4410, "superseded by a newer audio connection")))

    def detach_audio(self, conn: object) -> None:
        if self.audio_conn is conn:
            self.audio_conn = None
            self.audio_close = None

    def append_audio(self, data: bytes, offset: int | None = None) -> int:
        """Append contiguous PCM. Returns total received samples."""
        if self.flushing or self.closed:
            raise NotLive(str(self.session_id))
        if len(data) % BYTES_PER_SAMPLE:
            raise ValueError("PCM payload must have an even number of bytes")
        n = len(data) // BYTES_PER_SAMPLE
        if offset is not None:
            if offset > self.received or offset < 0:
                raise AudioGap(self.received)
            if offset + n <= self.received:
                return self.received
            data = data[(self.received - offset) * BYTES_PER_SAMPLE :]
            n = len(data) // BYTES_PER_SAMPLE
        if not n:
            return self.received
        self.buf.extend(data)
        self.received += n
        self.last_audio_monotonic = time.monotonic()
        if self.received - self.persisted >= self.chunk_samples:
            self._persist_wake.set()
        self._enforce_backlog()
        return self.received

    @property
    def audio_end_s(self) -> float:
        return self.received / SAMPLE_RATE

    def slice_pcm(self, start_s: float, end_s: float) -> bytes:
        a = max(int(round(start_s * SAMPLE_RATE)), self.buf_start)
        b = min(int(round(end_s * SAMPLE_RATE)), self.received)
        if b <= a:
            return b""
        return bytes(
            self.buf[
                (a - self.buf_start) * BYTES_PER_SAMPLE : (b - self.buf_start) * BYTES_PER_SAMPLE
            ]
        )

    def _trim(self) -> None:
        # The diarizer re-reads `live_diarizer_context_s` of audio per window.
        diar_s = self.settings.live_diarizer_context_s if self.manager.diarizer else 0.0
        keep_s = self.settings.live_left_context_s + max(20.0, diar_s)
        keep_from = min(
            self.persisted,
            int(max(0.0, self.committer.committed_until - keep_s) * SAMPLE_RATE),
        )
        drop = keep_from - self.buf_start
        if drop > 0:
            del self.buf[: drop * BYTES_PER_SAMPLE]
            self.buf_start = keep_from

    def _enforce_backlog(self) -> None:
        backlog = self.audio_end_s - self.committer.committed_until
        if backlog > MAX_BACKLOG_S:
            new_commit = self.audio_end_s - MAX_BACKLOG_S
            self.skipped_s += new_commit - self.committer.committed_until
            self.committer.committed_until = new_commit
            self.incomplete = True
            self._trim()

    # ---- persistence -------------------------------------------------------

    async def _persist_loop(self) -> None:
        while not self.closed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._persist_wake.wait(), timeout=2.0)
            self._persist_wake.clear()
            try:
                await self.persist_ready(final=False)
            except Exception as exc:  # noqa: BLE001 — retried next round
                log.warning("live.persist_failed", session_id=str(self.session_id), error=str(exc))
                await asyncio.sleep(2.0)

    async def persist_ready(self, *, final: bool) -> None:
        async with self._persist_lock:
            while self.received - self.persisted >= self.chunk_samples or (
                final and self.received > self.persisted
            ):
                n = min(self.chunk_samples, self.received - self.persisted)
                a = (self.persisted - self.buf_start) * BYTES_PER_SAMPLE
                pcm = bytes(self.buf[a : a + n * BYTES_PER_SAMPLE])
                seq, start = self.chunk_count, self.persisted
                await db_call(service.persist_audio_chunk, self.session_id, seq, start, pcm)
                self.persisted = start + n
                self.chunk_count = seq + 1
                self._trim()

    # ---- transcription -------------------------------------------------------

    async def _asr_loop(self) -> None:
        settings = self.settings
        backoff = 0.0
        try:
            while not self.closed:
                if not self.flushing:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._asr_wake.wait(), timeout=settings.live_step_s + backoff
                        )
                    self._asr_wake.clear()
                head = self.audio_end_s
                committed = self.committer.committed_until
                if self.flushing and head - committed <= 0.05:
                    break
                if not self.flushing and head - committed < settings.live_min_new_audio_s:
                    continue
                plan = plan_window(
                    committed,
                    head,
                    left_context_s=settings.live_left_context_s,
                    max_window_s=settings.live_max_window_s,
                )
                flush = self.flushing and not plan.catching_up
                try:
                    await self._run_window(plan.start, plan.end, plan.catching_up, flush)
                    backoff = 0.0
                    if self.asr_failures:
                        self.asr_failures = 0
                        await self._set_asr_state("ok")
                except (NemotronError, TimeoutError) as exc:
                    self.asr_failures += 1
                    log.warning(
                        "live.asr_window_failed",
                        session_id=str(self.session_id),
                        failures=self.asr_failures,
                        error=str(exc)[:300],
                    )
                    await self._set_asr_state(
                        "unavailable" if self.asr_failures >= 3 else "degraded"
                    )
                    backoff = min(30.0, 2.0 * self.asr_failures)
                    if self.flushing:
                        if self.asr_failures >= 3:
                            self.incomplete = True
                            break
                        await asyncio.sleep(min(backoff, 5.0))
                except Exception:  # noqa: BLE001 — keep the session alive
                    log.exception("live.asr_window_crashed", session_id=str(self.session_id))
                    self.asr_failures += 1
                    backoff = min(30.0, 2.0 * self.asr_failures)
                    if self.flushing and self.asr_failures >= 3:
                        self.incomplete = True
                        break
        finally:
            self._asr_done.set()

    async def _set_asr_state(self, state: str) -> None:
        if state == self.asr_state:
            return
        self.asr_state = state
        event = await db_call(service.set_status, self.session_id, None, asr_state=state)
        self.publish(event)

    def _context_terms(self) -> list[str]:
        terms = [self.title] if self.title and not self.title.startswith("Live — ") else []
        # Names already known in this conversation, so the ASR spells them right.
        names = self.manager._speaker_names.get(self.session_id, {})
        sources = self.manager._speaker_sources.get(self.session_id, {})
        for label, name in names.items():
            if sources.get(label, "default") != "default" and name not in terms:
                terms.append(name)
        for name in self.attendees:
            if name and name not in terms:
                terms.append(name)
        return terms[:40]

    @property
    def speaker_tags(self) -> int:
        """Diarization tags asked of Nemotron (the speaker-count hint, 2-4)."""
        if self.expected_speakers:
            return max(2, min(4, self.expected_speakers))
        return 4

    async def _run_window(self, start: float, end: float, catching_up: bool, flush: bool) -> None:
        pcm = self.slice_pcm(start, end)
        if not pcm:
            self.committer.committed_until = max(self.committer.committed_until, end)
            return
        client = self.manager.client
        timeout = self.settings.live_nemotron_timeout_s + 5
        terms = self._context_terms()
        tags = self.speaker_tags
        # LIVE_DIARIZER: stream this window's new audio alongside the ASR call.
        diar_task = (
            asyncio.ensure_future(self._diarize_window(end, flush))
            if self.manager.diarizer is not None
            else None
        )
        result: WindowTranscript = await asyncio.wait_for(
            anyio.to_thread.run_sync(
                lambda: client.transcribe(pcm, context_terms=terms, speaker_tags=tags),
                limiter=self.manager.limiter,
            ),
            timeout=timeout,
        )
        words = list(result.words)
        diarization = result.diarization
        diar_turns = await diar_task if diar_task is not None else None
        if diar_turns is not None and not words:
            diar_turns = None
        if diar_turns is not None:  # LIVE_DIARIZER: speakers by time overlap
            words, diarization = relabel_window(words, diar_turns, start, end)
        # ---- local speakers + embeddings
        by_tag: dict[str, list] = {}
        for w in words:
            by_tag.setdefault(w.speaker, []).append(w)
        spans = _speaker_spans(words, diarization)
        embeddings: dict[str, Any] = {}
        if self.settings.live_embed_enabled and spans:
            flat = [(tag, s, e) for tag, items in spans.items() for s, e in items]
            try:
                vectors = await asyncio.wait_for(
                    anyio.to_thread.run_sync(
                        lambda: client.speaker_embed(pcm, [(s, e) for _t, s, e in flat]),
                        limiter=self.manager.limiter,
                    ),
                    timeout=timeout,
                )
                grouped: dict[str, list] = {}
                for (tag, s, e), vec in zip(flat, vectors, strict=False):
                    grouped.setdefault(tag, []).append((vec, e - s))
                embeddings = {tag: average_embeddings(items) for tag, items in grouped.items()}
            except Exception as exc:  # noqa: BLE001 — embeddings are best-effort
                log.info("live.embed_failed", session_id=str(self.session_id), error=str(exc)[:200])
        locals_ = [
            LocalSpeaker(
                tag=tag,
                speech_s=sum(max(0.0, w.end - w.start) for w in items),
                embedding=embeddings.get(tag),
                last_end_s=start + max(w.end for w in items),
                first_start_s=start + min(w.start for w in items),
            )
            for tag, items in by_tag.items()
        ]
        if diar_turns is not None:
            assignment = assign_window(
                self.tracker, self.diar_linker, diar_turns, locals_, head_s=end, flush=flush
            )
        else:
            votes = self._overlap_votes(words, start)
            assignment = self.tracker.assign(locals_, votes) if locals_ else None
        mapping = assignment.mapping if assignment else {}
        timed = [
            TimedWord(
                text=w.text,
                start=round(start + w.start, 3),
                end=round(start + w.end, 3),
                speaker=mapping.get(w.speaker, "S1"),
            )
            for w in words
        ]
        commit = self.committer.update(timed, head=end, catching_up=catching_up, flush=flush)
        segments = segment_words(commit.final)
        # Speech time counts committed words only (windows overlap).
        for w in commit.final:
            self.tracker.add_speech(w.speaker, max(0.0, w.end - w.start))
        announce: set[str] = set()
        if assignment is not None:
            announce |= set(assignment.created)
            for merged, into in assignment.merged:
                announce |= {merged, into}
        changed = (
            set(mapping.values())
            | announce
            | {self.tracker.resolve(w.speaker) for w in commit.final}
        )
        snapshots = [
            service.SpeakerSnapshot(
                label=st.label,
                color_index=st.color_index,
                speech_seconds=st.speech_seconds,
                centroid=[float(x) for x in st.centroid] if st.centroid is not None else None,
                weight=st.weight,
                merged_into=st.merged_into,
            )
            for st in self.tracker.speakers.values()
            if st.label in changed
        ]
        events: list[dict] = []
        if segments or snapshots or commit.committed_until:
            events = await db_call(
                service.record_window,
                self.session_id,
                segments=[(sg.speaker, sg.start, sg.end, sg.text) for sg in segments],
                committed_until=commit.committed_until,
                speakers=snapshots,
                announce=announce,
            )
        for ev in events:
            self.publish(ev)
            if ev["type"] == "segment.final":
                d = ev["data"]
                self.last_final_seq = ev["seq"]
                self.recent_segments.append(
                    GateSegment(
                        seq=ev["seq"],
                        speaker=d["speaker"],
                        speaker_name=self.manager.speaker_name(self.session_id, d["speaker"]),
                        start_s=d["start_s"],
                        end_s=d["end_s"],
                        text=d["text"],
                    )
                )
        for ev in events:
            if ev["type"] == "speaker.update":
                self.manager.remember_speaker(self.session_id, ev["data"])
        if any(ev["type"] == "segment.final" for ev in events):
            self._maybe_name_cue()
        # Remember labelled words for the next window's overlap votes.
        for w in [*commit.final, *commit.interim]:
            self.labelled.append(_Label(word=w, norm=normalise(w.text)))
        self._publish_interim(commit.interim)
        if segments:
            self._gate_wake.set()
        self._trim()

    async def _diarize_window(self, end: float, flush: bool) -> list | None:
        """Stream audio up to `end` to the diarizer; its turns (session seconds,
        labels `D<slot>`) over the recent context, or None (no diarizer, or it
        failed: this window uses Nemotron's tags)."""
        diarizer = self.manager.diarizer
        if diarizer is None:
            return None
        end_sample = min(int(round(end * SAMPLE_RATE)), self.received)
        sent = self.diar_sent
        if sent is None or sent < self.buf_start or sent > end_sample:
            # First call (or resumed session): start with some context.
            ctx = int(self.settings.live_diarizer_context_s * SAMPLE_RATE)
            sent = max(self.buf_start, end_sample - ctx)
        pcm = self.slice_pcm(sent / SAMPLE_RATE, end_sample / SAMPLE_RATE)
        if not pcm and not flush:
            return None
        stream_id = str(self.session_id)
        try:
            turns, _until, reset = await asyncio.wait_for(
                anyio.to_thread.run_sync(
                    lambda: diarizer.stream(stream_id, sent / SAMPLE_RATE, pcm, flush=flush)
                ),
                timeout=self.settings.live_diarizer_timeout_s + 2,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to Nemotron's tags
            self.diar_sent = None  # unknown stream state: restart with context
            log.info("live.diarize_failed", session_id=str(self.session_id), error=str(exc)[:200])
            return None
        self.diar_sent = None if flush else end_sample
        if reset:  # new stream: its slots are not the old ones
            self.diar_linker.reset()
            if sent > self.buf_start:
                log.info(
                    "live.diarize_reset", session_id=str(self.session_id), at_s=sent / SAMPLE_RATE
                )
        return [(s, e, f"D{tag}") for s, e, tag in turns]

    def _overlap_votes(self, words, offset: float) -> dict[str, Counter[str]]:
        votes: dict[str, Counter[str]] = {}
        if not self.labelled:
            return votes
        known = list(self.labelled)
        for w in words:
            norm = normalise(w.text)
            if not norm:
                continue
            abs_start = offset + w.start
            for lab in reversed(known):
                if lab.word.end < abs_start - 2.0:
                    break
                if lab.norm == norm and abs(lab.word.start - abs_start) <= VOTE_TOLERANCE_S:
                    votes.setdefault(w.speaker, Counter())[
                        self.tracker.resolve(lab.word.speaker)
                    ] += 1
                    break
        return votes

    def _publish_interim(self, interim: tuple[TimedWord, ...]) -> None:
        parts: list[dict[str, str]] = []
        for w in interim:
            if parts and parts[-1]["speaker"] == w.speaker:
                parts[-1]["_words"].append(w)  # type: ignore[union-attr]
            else:
                parts.append({"speaker": w.speaker, "_words": [w]})  # type: ignore[dict-item]
        clean_parts = [{"speaker": p["speaker"], "text": join_words(p["_words"])} for p in parts]  # type: ignore[arg-type]
        text = join_words(interim)
        self.interim_text = text
        data = {
            "after_seq": self.last_final_seq,
            "speaker": interim[0].speaker if interim else None,
            "start_s": round(interim[0].start, 3) if interim else None,
            "end_s": round(interim[-1].end, 3) if interim else None,
            "text": text,
            "parts": clean_parts,
        }
        self.publish(service.envelope(None, "segment.interim", data))

    def publish(self, event: dict | None) -> None:
        if event is not None:
            self.manager.broker.publish(self.session_id, event)

    # ---- naming ------------------------------------------------------------------

    def _maybe_name_cue(self) -> None:
        """Cheap check of the newest utterances for a name cue (intro / address).

        Runs on coalesced utterances, so split phrases across fragments are
        one sentence again. A cue wakes the naming loop right away.
        """
        recent = list(self.recent_segments)[-NAMING_CUE_LOOKBACK:]
        if not recent:
            return
        frags = [(g.seq, g.speaker, g.start_s, g.end_s, g.text) for g in recent]
        cue = False
        priors = Priors()
        for u in coalesce(frags, resolve=self.tracker.resolve):
            if self.manager.speaker_source(self.session_id, u.speaker) == "manual":
                continue
            if intro.detect_intro(u.text) is not None:
                cue = True
                break
            if self.people and name_mentions(u.text, self.people, priors):
                cue = True
                break
        if cue:
            self._naming_cue = True
            self._naming_wake.set()

    async def _naming_loop(self) -> None:
        interval = self.settings.live_naming_interval_s
        while not self.closed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._naming_wake.wait(), timeout=interval)
            self._naming_wake.clear()
            if self.closed or self.flushing:
                return
            try:
                await self.naming_pass()
            except Exception as exc:  # noqa: BLE001 — naming never breaks the session
                log.info("live.naming_failed", session_id=str(self.session_id), error=str(exc))

    async def final_recluster(self) -> None:
        """At stop every speaker is idle: fold leftover fragments into their speaker."""
        tracker = self.tracker
        tracker.now_s = max(tracker.now_s, self.committer.committed_until) + tracker.small_idle_s
        merged = tracker.recluster()
        if not merged:
            return
        changed = {label for pair in merged for label in pair}
        snapshots = [
            service.SpeakerSnapshot(
                label=st.label,
                color_index=st.color_index,
                speech_seconds=st.speech_seconds,
                centroid=[float(x) for x in st.centroid] if st.centroid is not None else None,
                weight=st.weight,
                merged_into=st.merged_into,
            )
            for st in tracker.speakers.values()
            if st.label in changed
        ]
        events = await db_call(
            service.record_window,
            self.session_id,
            segments=[],
            committed_until=self.committer.committed_until,
            speakers=snapshots,
            announce=changed,
        )
        for ev in events:
            self.publish(ev)
            if ev["type"] == "speaker.update":
                self.manager.remember_speaker(self.session_id, ev["data"])
        log.info("live.final_recluster", session_id=str(self.session_id), merged=merged)

    async def naming_pass(self, *, final: bool = False) -> None:
        """Deterministic naming now; the LLM pass when due (cue, interval, or stop)."""
        if self.people is None:
            self.people = await anyio.to_thread.run_sync(lambda: get_directory().people())
        now = time.monotonic()
        cue, self._naming_cue = self._naming_cue, False
        last_seq = self.last_final_seq
        if not final and last_seq <= self.named_through_seq and not cue:
            return
        if (
            not final
            and not cue
            and self.last_naming_at is not None
            and now - self.last_naming_at < NAMING_INTRO_GAP_S
        ):
            return
        llm_gap = self.settings.live_naming_min_gap_s
        llm_due = final or (
            last_seq > self.llm_named_through_seq
            and (self.last_llm_at is None or now - self.last_llm_at >= llm_gap)
            and (
                cue
                or self.last_llm_at is None
                or now - self.last_llm_at >= self.settings.live_naming_interval_s
            )
        )
        if llm_due and not final:
            ctx_speakers = await db_call(_speaker_views, self.session_id)
            llm_due = name_pass.needs_llm(ctx_speakers)
        self.last_naming_at = now
        self.named_through_seq = last_seq
        people = self.people
        if llm_due:
            self.last_llm_at = now
            self.llm_named_through_seq = last_seq
        timeout = self.settings.live_naming_timeout_s + 15
        outcome = await asyncio.wait_for(
            anyio.to_thread.run_sync(
                functools.partial(
                    name_pass.run_pass,
                    self.session_id,
                    use_llm=llm_due,
                    settings=self.settings,
                    people=people,
                )
            ),
            timeout=timeout,
        )
        for label, into in outcome.merges:
            self.tracker.merge(label, into)
        for ev in outcome.events:
            self.publish(ev)
            if ev.get("type") == "speaker.update":
                self.manager.remember_speaker(self.session_id, ev["data"])
        if outcome.result is not None and outcome.result.llm_error and not final:
            # Let the next interval retry instead of the next cue hammering a failing gateway.
            self.last_llm_at = now

    # ---- gate ------------------------------------------------------------------

    async def _gate_loop(self) -> None:
        gate = self.manager.gate
        assert gate is not None
        interval = self.settings.live_gate_interval_s
        while not self.closed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._gate_wake.wait(), timeout=interval)
            self._gate_wake.clear()
            if self.closed or self.flushing:
                return
            if not self.recent_segments:
                continue
            await self.observe_gate(gate)

    async def observe_gate(self, gate: LiveGate) -> None:
        published: set[str] = set()

        async def on_trigger(trigger: GateTrigger) -> None:
            if trigger.trigger_id in published:
                return
            published.add(trigger.trigger_id)
            await self.publish_trigger(trigger)

        async def on_tick(tick: GateTick) -> None:
            self.publish(service.envelope(None, "gate.tick", tick.to_payload()))

        ctx = GateSessionContext(
            session_id=self.session_id,
            meeting_id=self.meeting_id,
            title=self.title,
            attendees=tuple(self.attendees),
            segments=tuple(self.recent_segments),
            new_from_seq=self.gate_seen_seq + 1,
            interim_text=self.interim_text,
            last_seq=self.last_final_seq,
            audio_seconds=self.audio_end_s,
            on_trigger=on_trigger,
            owner_user_id=self.starter_id,
            gate_provider=self.gate_provider,
            on_tick=on_tick,
        )
        self.gate_seen_seq = self.last_final_seq
        try:
            returned = await asyncio.wait_for(
                gate.observe(ctx), timeout=self.settings.live_gate_timeout_s
            )
        except Exception as exc:  # noqa: BLE001 — the gate never breaks the session
            log.warning("live.gate_observe_failed", session_id=str(self.session_id), error=str(exc))
            return
        for trigger in returned or ():
            if isinstance(trigger, GateTrigger):
                await on_trigger(trigger)

    async def publish_trigger(self, trigger: GateTrigger) -> None:
        event = await db_call(service.publish_trigger, self.session_id, trigger)
        if event is None:
            return
        self.publish(event)
        delay = self.settings.live_fallback_after_s
        if delay > 0 and self.manager.responder is not None:
            self.spawn(self._fallback(UUID(event["data"]["trigger_id"]), delay))

    async def _fallback(self, trigger_id: UUID, delay: float) -> None:
        await asyncio.sleep(delay)
        responder = self.manager.responder
        if responder is None:
            return
        try:
            events = await anyio.to_thread.run_sync(responder.respond, self.session_id, trigger_id)
            for ev in events or ():
                self.publish(ev)
        except Exception as exc:  # noqa: BLE001
            log.warning("live.fallback_failed", session_id=str(self.session_id), error=str(exc))
            return
        types = {ev.get("type") for ev in events or ()}
        if "gate.claimed" in types and "finding.new" not in types:
            # The built-in responder claimed it but had nothing to cite: the
            # trigger is released as expired, so free the gate's slot now.
            self.manager.trigger_resolved(self.session_id, trigger_id)

    # ---- stop --------------------------------------------------------------------

    async def drain(self, timeout: float) -> None:
        """Stop accepting audio, transcribe the tail, persist all audio."""
        self.flushing = True
        if self.audio_close is not None:
            close, self.audio_close, self.audio_conn = self.audio_close, None, None
            with contextlib.suppress(Exception):
                await _maybe_await(close(1000, "session stopped"))
        self._asr_wake.set()
        try:
            await asyncio.wait_for(self._asr_done.wait(), timeout=timeout)
        except TimeoutError:
            self.incomplete = True
        for _ in range(3):
            try:
                await self.persist_ready(final=True)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("live.final_persist_failed", error=str(exc))
                await asyncio.sleep(1.0)
        if self.received > self.persisted:
            self.incomplete = True
        if self.skipped_s > 1.0:
            self.incomplete = True


def _speaker_spans(words, diarization) -> dict[str, list[tuple[float, float]]]:
    """Up to two longest contiguous spans (≥ 0.6 s) per window-local tag."""
    spans: dict[str, list[tuple[float, float]]] = {}
    if diarization:
        for s, e, tag in diarization:
            if e - s >= 0.6:
                spans.setdefault(tag, []).append((s, e))
    else:
        cur_tag, cur_s, cur_e = None, 0.0, 0.0
        for w in words:
            if cur_tag == w.speaker and w.start - cur_e <= 0.4:
                cur_e = w.end
                continue
            if cur_tag is not None and cur_e - cur_s >= 0.6:
                spans.setdefault(cur_tag, []).append((cur_s, cur_e))
            cur_tag, cur_s, cur_e = w.speaker, w.start, w.end
        if cur_tag is not None and cur_e - cur_s >= 0.6:
            spans.setdefault(cur_tag, []).append((cur_s, cur_e))
    return {
        tag: sorted(items, key=lambda se: se[1] - se[0], reverse=True)[:2]
        for tag, items in spans.items()
    }


async def _maybe_await(value):
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _db_call(fn, *args, **kwargs):
    with SessionLocal() as db:
        return fn(db, *args, **kwargs)


async def db_call(fn, *args, **kwargs):
    """Run `fn(db, *args, **kwargs)` with a fresh DB session on a worker thread."""
    return await anyio.to_thread.run_sync(functools.partial(_db_call, fn, *args, **kwargs))


class LiveManager:
    """Process-wide registry of live runtimes + the idle/stuck sweeper."""

    def __init__(
        self,
        *,
        settings: LiveSettings | None = None,
        client: NemotronClient | None = None,
        broker: LiveBroker | None = None,
        gate: LiveGate | None = None,
        gate_loader: Callable[[], LiveGate | None] | None = None,
        responder=None,
    ) -> None:
        self.settings = settings or get_live_settings()
        self._client = client
        self.broker = broker or default_broker
        self._gate = gate
        self._gate_loaded = gate is not None
        self._gate_loader = gate_loader or load_gate
        self._responder = responder
        self.runtimes: dict[UUID, LiveRuntime] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._speaker_names: dict[UUID, dict[str, str]] = {}
        self._speaker_sources: dict[UUID, dict[str, str]] = {}
        self.limiter = anyio.CapacityLimiter(8)
        self._sweeper: asyncio.Task | None = None
        self._stopping: set[UUID] = set()
        self._discard_after: set[UUID] = set()
        self.broker.add_listener(self._on_event)

    # ---- pluggable collaborators -------------------------------------------

    @property
    def client(self) -> NemotronClient:
        if self._client is None:
            if self.settings.asr_provider == "mock":
                from app.live.mock_provider import MockTranscriber

                self._client = MockTranscriber()
            else:
                self._client = NemotronClient(settings=self.settings)
        return self._client

    @property
    def diarizer(self):
        """The `LIVE_DIARIZER` speaker layer client, or None (Nemotron's tags)."""
        if not hasattr(self, "_diarizer"):
            self._diarizer = load_diarizer(self.settings)
        return self._diarizer

    def set_diarizer(self, diarizer) -> None:
        self._diarizer = diarizer

    @property
    def gate(self) -> LiveGate | None:
        if not self._gate_loaded:
            self._gate_loaded = True
            self._gate = self._gate_loader() if self.settings.jev_gate_enabled else None
            if self._gate is not None and hasattr(self._gate, "warmup"):
                with contextlib.suppress(RuntimeError):  # no running loop: skip
                    asyncio.get_running_loop()
                    task = asyncio.create_task(self._gate_hook_async("warmup"))
                    _BACKGROUND.add(task)
                    task.add_done_callback(_BACKGROUND.discard)
        return self._gate

    @property
    def loaded_gate(self) -> LiveGate | None:
        """The gate if it has been loaded already (never triggers a load)."""
        return self._gate if self._gate_loaded else None

    def set_gate_provider(self, session_id: UUID, provider: str | None) -> None:
        """Switch a running session's gate provider in place (PATCH /sessions/{id})."""
        rt = self.runtimes.get(session_id)
        if rt is not None:
            rt.gate_provider = provider

    def set_expected_speakers(self, session_id: UUID, n: int | None) -> None:
        """Apply a new speaker-count hint to a running session (re-clusters next window)."""
        rt = self.runtimes.get(session_id)
        if rt is not None:
            rt.expected_speakers = n
            rt.tracker.expected_speakers = n
            rt._asr_wake.set()

    def set_gate(self, gate: LiveGate | None) -> None:
        self._gate = gate
        self._gate_loaded = True

    # Optional gate lifecycle hooks (`app.live.gate` docstring). Only an
    # already-loaded gate is called; failures never reach the session.

    def _gate_hook(self, name: str, *args: Any, **kwargs: Any) -> None:
        fn = getattr(self._gate, name, None) if self._gate is not None else None
        if fn is None:
            return
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("live.gate_hook_failed", hook=name, error=str(exc))

    async def _gate_hook_async(self, name: str, timeout: float = 10.0) -> None:
        fn = getattr(self._gate, name, None) if self._gate is not None else None
        if fn is None:
            return
        try:
            await asyncio.wait_for(_maybe_await(fn()), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.warning("live.gate_hook_failed", hook=name, error=str(exc))

    def trigger_resolved(self, session_id: UUID, trigger_id: UUID | str) -> None:
        self._gate_hook("mark_resolved", str(trigger_id), session_id=str(session_id))

    def _on_event(self, session_id: UUID, event: dict[str, Any]) -> None:
        """Broker listener (loop thread): feed finding outcomes back to the gate."""
        if self._gate is None or event.get("type") != "finding.new":
            return
        data = event.get("data") or {}
        if data.get("trigger_id"):
            self.trigger_resolved(session_id, data["trigger_id"])
        elif data.get("title"):
            self._gate_hook("note_surfaced", str(session_id), str(data["title"]))

    @property
    def responder(self):
        if self._responder is None:
            from app.live.answer import LLMAnswerer

            self._responder = LLMAnswerer(tail=self._interim_tail)
        return self._responder

    def _interim_tail(self, session_id: UUID) -> str:
        rt = self.runtimes.get(session_id)
        return getattr(rt, "interim_text", "") if rt is not None else ""

    def speaker_name(self, session_id: UUID, label: str) -> str:
        return self._speaker_names.get(session_id, {}).get(label, f"Speaker {label.lstrip('S')}")

    def remember_speaker(self, session_id: UUID, data: dict) -> None:
        self._speaker_names.setdefault(session_id, {})[data["label"]] = data["display_name"]
        if data.get("source"):
            self._speaker_sources.setdefault(session_id, {})[data["label"]] = data["source"]

    def speaker_source(self, session_id: UUID, label: str) -> str:
        """`default` | `manual` | `self_intro` (how the label's name was set)."""
        return self._speaker_sources.get(session_id, {}).get(label, "default")

    def _lock(self, session_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = self._locks[session_id] = asyncio.Lock()
        return lock

    # ---- runtimes ----------------------------------------------------------------

    async def ensure_runtime(self, session_id: UUID) -> LiveRuntime:
        self.broker.bind_loop()
        async with self._lock(session_id):
            rt = self.runtimes.get(session_id)
            if rt is not None and not rt.closed:
                return rt
            if session_id in self._stopping:
                raise NotLive(str(session_id))
            state = await anyio.to_thread.run_sync(_load_state, session_id, self.settings)
            if state["status"] != "live":
                raise NotLive(str(session_id))
            tracker = SpeakerTracker(
                match_threshold=self.settings.live_speaker_match_threshold,
                new_threshold=self.settings.live_speaker_new_threshold,
                min_new_s=self.settings.live_speaker_min_new_s,
                merge_threshold=self.settings.live_speaker_merge_threshold,
                max_speakers=self.settings.live_max_speakers,
                expected_speakers=state["expected_speakers"],
                is_locked=lambda label, sid=session_id: self.speaker_source(sid, label) == "manual",
            )
            if self.diarizer is not None:
                tune_tracker(tracker, self.settings)
            for sp in state["speakers"]:
                from app.live.speakers import SpeakerState

                tracker.speakers[sp["label"]] = SpeakerState(
                    label=sp["label"],
                    color_index=sp["color_index"],
                    centroid=np.asarray(sp["centroid"], dtype=np.float32)
                    if sp["centroid"]
                    else None,
                    weight=sp["weight"],
                    speech_seconds=sp["speech_seconds"],
                    merged_into=sp["merged_into"],
                    last_seen_s=state["committed_until"],
                )
            # Cannot-link evidence is in memory only: recount it from the segments.
            tracker.rebuild_cannot_link(
                state["spans"],
                window_s=self.settings.live_max_window_s,
                step_s=self.settings.live_step_s,
            )
            self._speaker_names[session_id] = {
                sp["label"]: sp["display_name"] for sp in state["speakers"]
            }
            self._speaker_sources[session_id] = {
                sp["label"]: sp["name_source"] or "default" for sp in state["speakers"]
            }
            rt = LiveRuntime(
                manager=self,
                session_id=session_id,
                starter_id=state["starter_id"],
                title=state["title"],
                attendees=state["attendees"],
                audio_samples=state["audio_samples"],
                chunk_count=state["chunk_count"],
                committed_until=state["committed_until"],
                last_final_seq=state["last_final_seq"],
                tracker=tracker,
                tail_pcm=state["tail_pcm"],
                tail_start_sample=state["tail_start_sample"],
                gate_provider=state["gate_provider"],
                expected_speakers=state["expected_speakers"],
            )
            self.runtimes[session_id] = rt
            rt.start()
            self.ensure_sweeper()
            return rt

    def get(self, session_id: UUID) -> LiveRuntime | None:
        return self.runtimes.get(session_id)

    async def stop(self, session_id: UUID, *, discard: bool = False) -> None:
        """Drain + finalize (or discard). Safe to call repeatedly."""
        if session_id in self._stopping:
            if discard:  # owner discarded while a normal stop was draining
                self._discard_after.add(session_id)
            return
        self._stopping.add(session_id)
        try:
            rt = self.runtimes.get(session_id)
            incomplete = False
            if rt is not None:
                if discard:
                    rt.flushing = True
                    if rt.audio_close is not None:
                        with contextlib.suppress(Exception):
                            await _maybe_await(rt.audio_close(1000, "session discarded"))
                else:
                    await rt.drain(timeout=max(60.0, self.settings.live_nemotron_timeout_s * 4))
                    incomplete = rt.incomplete
                    try:
                        await rt.final_recluster()
                    except Exception as exc:  # noqa: BLE001 — never blocks finalize
                        log.info(
                            "live.final_recluster_failed",
                            session_id=str(session_id),
                            error=str(exc)[:200],
                        )
                    if self.settings.live_naming_enabled:
                        # Last naming pass over the whole conversation before finalize.
                        try:
                            await asyncio.wait_for(
                                rt.naming_pass(final=True),
                                timeout=self.settings.live_naming_timeout_s + 20,
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.info(
                                "live.final_naming_failed",
                                session_id=str(session_id),
                                error=str(exc)[:200],
                            )
                await rt.close()
                self.runtimes.pop(session_id, None)
            discard = discard or session_id in self._discard_after
            if discard:
                from app.live.finalize import discard_session

                event = await db_call(discard_session, session_id)
            else:
                from app.live.finalize import finalize_session

                try:
                    event = await db_call(finalize_session, session_id, incomplete=incomplete)
                except Exception as exc:
                    log.exception("live.finalize_failed", session_id=str(session_id))
                    event = await db_call(
                        service.set_status, session_id, "failed", error=f"finalize failed: {exc}"
                    )
            if event is not None:
                self.broker.publish(session_id, event)
                if not discard and (event.get("data") or {}).get("status") == "finalized":
                    from app.live import recap

                    recap.schedule(session_id)
            self.broker.clear(session_id)
            self._speaker_names.pop(session_id, None)
            self._speaker_sources.pop(session_id, None)
        finally:
            self._stopping.discard(session_id)
            self._discard_after.discard(session_id)
            self._locks.pop(session_id, None)
            self._gate_hook("end_session", str(session_id))

    def stop_in_background(self, session_id: UUID, *, discard: bool = False) -> asyncio.Task:
        task = asyncio.create_task(self.stop(session_id, discard=discard))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        return task

    # ---- sweeper ---------------------------------------------------------------------

    def ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop(), name="live-sweeper")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(60.0)
            try:
                await self.sweep_once()
            except Exception:  # noqa: BLE001
                log.exception("live.sweep_failed")

    async def sweep_once(self) -> list[UUID]:
        due = await anyio.to_thread.run_sync(_due_sessions, self.settings)
        for session_id in due:
            log.info("live.auto_stop", session_id=str(session_id))
            self.broker.publish(
                session_id, await db_call(service.set_status, session_id, "stopping")
            )
            await self.stop(session_id)
        # Runtimes whose audio went idle for too long.
        idle = self.settings.live_idle_finalize_minutes * 60
        for session_id, rt in list(self.runtimes.items()):
            if rt.audio_conn is None and time.monotonic() - rt.last_audio_monotonic > idle:
                self.broker.publish(
                    session_id, await db_call(service.set_status, session_id, "stopping")
                )
                await self.stop(session_id)
        await self._purge_audio()
        return due

    async def _purge_audio(self) -> None:
        """Drop opt-in raw audio past its retention date (at most every 10 min)."""
        now = time.monotonic()
        if now - getattr(self, "_audio_purged_at", -1e9) < 600:
            return
        self._audio_purged_at = now
        from app.live import audio_store

        try:
            await db_call(audio_store.purge_expired)
        except Exception as exc:  # noqa: BLE001 — retried next sweep
            log.warning("live.audio_purge_failed", error=str(exc)[:200])

    async def shutdown(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sweeper
            self._sweeper = None
        for rt in list(self.runtimes.values()):
            # Persist what we have; clients resume from `ready` after restart.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(rt.persist_ready(final=False), timeout=10)
            await rt.close()
        self.runtimes.clear()
        if self._gate is not None:
            await self._gate_hook_async("aclose")
            # A later lifespan (tests, reload) builds a fresh gate.
            self._gate, self._gate_loaded = None, False


_BACKGROUND: set[asyncio.Task] = set()


def _speaker_views(db, session_id: UUID) -> list:
    """For `db_call` (which passes the session first)."""
    from sqlalchemy import select

    from app.models.live import LiveSpeaker

    rows = list(db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == session_id)))
    return name_pass._views(rows)


def _load_state(session_id: UUID, settings: LiveSettings) -> dict:
    from sqlalchemy import func, select

    from app.models.live import LiveSegment, LiveSession, LiveSpeaker

    with SessionLocal() as db:
        s = db.get(LiveSession, session_id)
        if s is None:
            raise LookupError("live session not found")
        speakers = [
            {
                "label": sp.label,
                "color_index": sp.color_index,
                "centroid": list(sp.centroid) if sp.centroid else None,
                "weight": sp.centroid_weight,
                "speech_seconds": sp.speech_seconds,
                "merged_into": sp.merged_into,
                "display_name": sp.display_name,
                "name_source": sp.name_source,
            }
            for sp in db.scalars(select(LiveSpeaker).where(LiveSpeaker.session_id == session_id))
        ]
        last_final = db.scalar(
            select(func.max(LiveSegment.seq)).where(LiveSegment.session_id == session_id)
        )
        attendees = [a["name"] or a["email"] for a in service.attendees_of(s)]
        committed = float(s.committed_until_s or 0)
        tail_pcm = b""
        tail_start = None
        spans = [
            (lab, float(a), float(b))
            for lab, a, b in db.execute(
                select(
                    LiveSegment.speaker_label, LiveSegment.start_seconds, LiveSegment.end_seconds
                ).where(LiveSegment.session_id == session_id)
            )
        ]
        # After a restart: reload the persisted, untranscribed tail (bounded).
        duration = s.audio_samples / SAMPLE_RATE
        if s.audio_chunk_count and duration - committed > 0.05:
            tail_pcm, tail_start = _restore_tail(s, committed, settings)
        return {
            "starter_id": s.started_by_user_id,
            "status": s.status,
            "title": s.title,
            "attendees": attendees,
            "gate_provider": s.gate_provider,
            "expected_speakers": s.expected_speakers,
            "audio_samples": s.audio_samples,
            "chunk_count": s.audio_chunk_count,
            "committed_until": committed,
            "last_final_seq": last_final or 0,
            "speakers": speakers,
            "spans": spans,
            "tail_pcm": tail_pcm,
            "tail_start_sample": tail_start,
        }


def _restore_tail(s, committed: float, settings: LiveSettings) -> tuple[bytes, int | None]:
    """RGS keeps no audio: after a restart the live window resumes at the head."""
    return b"", None


def _due_sessions(settings: LiveSettings) -> list[UUID]:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import or_, select

    from app.models.live import LiveSession

    now = datetime.now(UTC)
    idle = timedelta(minutes=settings.live_idle_finalize_minutes)
    with SessionLocal() as db:
        rows = db.execute(
            select(LiveSession.id, LiveSession.status).where(
                LiveSession.status.in_(("live", "stopping")),
                or_(
                    LiveSession.started_at < now - timedelta(hours=settings.live_max_session_hours),
                    LiveSession.last_audio_at < now - idle,
                    (LiveSession.last_audio_at.is_(None)) & (LiveSession.started_at < now - idle),
                    LiveSession.status == "stopping",
                ),
            )
        ).all()
    # `LiveManager.stop` ignores sessions it is already stopping.
    return [sid for sid, _status in rows]


manager = LiveManager()
