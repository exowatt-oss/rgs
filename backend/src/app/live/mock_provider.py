"""Deterministic offline providers. Synthetic audio is *not* transcribed.

Use only for demonstrations and tests; every non-silent window emits the same
invented utterance, independent of microphone content. Production installations
must explicitly configure a real ASR and gate provider.
"""

from __future__ import annotations

from app.live.gate import GateSessionContext, GateTick, GateTrigger
from app.live.nemotron import WindowTranscript, Word

SAMPLE_LINE = "Who owns the sample project?"


class MockTranscriber:
    def transcribe(
        self,
        pcm: bytes,
        *,
        sample_rate: int = 16000,
        context_terms: list[str] | None = None,
        speaker_tags: int = 4,
    ) -> WindowTranscript:
        duration = len(pcm) / (sample_rate * 2)
        if not pcm or not any(pcm):
            return WindowTranscript(text="", words=(), aligned=True)
        tokens = SAMPLE_LINE.split()
        words = tuple(
            Word(
                text=token,
                start=round(i * duration / len(tokens), 3),
                end=round((i + 1) * duration / len(tokens), 3),
                speaker="0",
            )
            for i, token in enumerate(tokens)
        )
        return WindowTranscript(text=SAMPLE_LINE, words=words, aligned=True)

    def speaker_embed(self, pcm: bytes, segments: list[tuple[float, float]]) -> list[None]:
        return [None for _ in segments]


class MockGate:
    async def observe(self, ctx: GateSessionContext) -> list[GateTrigger]:
        new = [s for s in ctx.segments if s.seq >= ctx.new_from_seq]
        triggers = [
            GateTrigger(
                action="find_owner",
                query=s.text,
                seq_from=s.seq,
                seq_to=s.seq,
                probability=1.0,
                anchor_seq=s.seq,
                provider="mock",
            )
            for s in new
            if s.text.strip().endswith("?")
        ]
        if ctx.on_tick:
            await ctx.on_tick(
                GateTick(
                    provider="mock",
                    latency_ms=0,
                    considered_lines=len(new),
                    decision="find_owner" if triggers else "none",
                    p=1.0 if triggers else 0.0,
                )
            )
        return triggers


class ProviderGate:
    """Route each session to the offline gate or a configured external gate."""

    def __init__(self) -> None:
        from app.live.jev_settings import get_jev_settings

        self.settings = get_jev_settings()
        self.mock = MockGate()
        self.external = None
        if self.settings.provider_available("jev") or self.settings.provider_available("openai"):
            from app.live.jev_gate import create_gate

            self.external = create_gate(settings=self.settings)

    async def observe(self, ctx: GateSessionContext) -> list[GateTrigger] | None:
        provider = self.settings.resolve_provider(ctx.gate_provider)
        if provider == "mock":
            return await self.mock.observe(ctx)
        if self.external is None:
            return []
        return await self.external.observe(ctx)

    def mark_resolved(self, *args, **kwargs) -> None:
        if self.external is not None:
            self.external.mark_resolved(*args, **kwargs)

    def note_surfaced(self, *args, **kwargs) -> None:
        if self.external is not None:
            self.external.note_surfaced(*args, **kwargs)

    def end_session(self, *args, **kwargs) -> None:
        if self.external is not None:
            self.external.end_session(*args, **kwargs)

    def provider_status(self) -> dict:
        if self.external is None:
            return {"mock": {"healthy": True, "last_error": None}}
        return {"mock": {"healthy": True, "last_error": None}, **self.external.provider_status()}

    async def aclose(self) -> None:
        if self.external is not None:
            await self.external.aclose()

    async def warmup(self) -> None:
        if self.external is not None:
            await self.external.warmup()
