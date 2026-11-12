"""Env-driven settings for the Live Record decision gate (Jev / OpenAI Decisions).

Kept separate from `app.config.Settings` so the gate can be enabled, tuned
and rotated without touching the app-wide settings object (and so the gate
module imports nothing heavier than pydantic-settings). Same conventions as
`app.config`: production defaults, `.env` file at the repo root, empty env
values treated as unset.

Contract (see README provider configuration):

- `JEV_GATE_ENABLED`  master switch for the gate (either provider); on by default in RGS.
- `LIVE_GATE_PROVIDER` default provider for new sessions: `mock` | `jev` | `openai`
                      (a session can override it). A provider is usable iff
                      its key is set.
- `JEV_API_KEY`       Optional TypeSafe API key.
- `JEV_ENDPOINT`      System One endpoint.
- `JEV_MODEL`         pinned Jev model the thresholds were tuned against.
- `OPENAI_DECISIONS_API_KEY` / `_MODEL` / `_BASE_URL`  OpenAI Decisions API
                      (`POST {base}/decisions`).

Thresholds are illustrative defaults and may need calibration to each
operator's provider. No production meeting data is included.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# Pinned on purpose: the gate's thresholds are calibrated against one model's
# probability distribution. `jev-latest` would silently move them.
DEFAULT_JEV_MODEL = "jev-1.14.0"

# Gate providers, in display order. `LIVE_GATE_PROVIDER` and the per-session
# `gate_provider` take one of these ids.
PROVIDER_MOCK = "mock"
PROVIDER_JEV = "jev"
PROVIDER_OPENAI = "openai"
GATE_PROVIDERS: tuple[str, ...] = (PROVIDER_MOCK, PROVIDER_JEV, PROVIDER_OPENAI)
PROVIDER_LABELS: dict[str, str] = {
    PROVIDER_MOCK: "Offline mock",
    PROVIDER_JEV: "TypeSafe Jev",
    PROVIDER_OPENAI: "OpenAI Decisions",
}
# No model or endpoint alias is assumed; provide the compatible Decisions API.
DEFAULT_OPENAI_DECISIONS_MODEL = ""
DEFAULT_OPENAI_DECISIONS_BASE_URL = ""


class JevGateSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(Path(__file__).resolve().parents[4] / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        # Accept `threshold=0.6` as well as `JEV_GATE_THRESHOLD=0.6` in code.
        populate_by_name=True,
    )

    # --- Required contract -------------------------------------------------
    jev_api_key: SecretStr = Field(default=SecretStr(""), alias="JEV_API_KEY")
    jev_endpoint: str = Field(default=DEFAULT_JEV_ENDPOINT, alias="JEV_ENDPOINT")
    jev_model: str = Field(default=DEFAULT_JEV_MODEL, alias="JEV_MODEL")
    jev_gate_enabled: bool = Field(default=True, alias="JEV_GATE_ENABLED")
    # Default provider for sessions that do not pick one (`mock` | `jev` | `openai`).
    live_gate_provider: str = Field(default=PROVIDER_MOCK, alias="LIVE_GATE_PROVIDER")

    # --- OpenAI Decisions provider -------------------------------------------
    openai_decisions_api_key: SecretStr = Field(
        default=SecretStr(""), alias="OPENAI_DECISIONS_API_KEY"
    )
    openai_decisions_model: str = Field(
        default=DEFAULT_OPENAI_DECISIONS_MODEL, alias="OPENAI_DECISIONS_MODEL"
    )
    openai_decisions_base_url: str = Field(
        default=DEFAULT_OPENAI_DECISIONS_BASE_URL, alias="OPENAI_DECISIONS_BASE_URL"
    )
    # Same firing rule as Jev (P(not none) >= threshold) on OpenAI's own
    # probability table. Kept separate: the two models are calibrated differently.
    # Illustrative threshold; calibrate with consented data from your deployment.
    openai_threshold: float = Field(default=0.5, ge=0.0, le=1.0, alias="OPENAI_DECISIONS_THRESHOLD")
    openai_timeout_s: float = Field(default=3.0, gt=0.0, alias="OPENAI_DECISIONS_TIMEOUT_S")

    # --- Firing rules ------------------------------------------------------
    # Minimum P(line's action != none) = 1 - p(none) to fire on that line.
    threshold: float = Field(default=0.8, ge=0.0, le=1.0, alias="JEV_GATE_THRESHOLD")
    # No Jev call for this long after a trigger fires. Lines spoken meanwhile are
    # carried (see carry_window_s), not dropped.
    debounce_s: float = Field(default=8.0, ge=0.0, alias="JEV_GATE_DEBOUNCE_S")
    # Suppress a trigger whose topic overlaps one fired within this window.
    dedupe_window_s: float = Field(default=180.0, ge=0.0, alias="JEV_GATE_DEDUPE_WINDOW_S")
    # Jaccard overlap of topic keywords at/above which two topics are "the same".
    dedupe_jaccard: float = Field(default=0.5, ge=0.0, le=1.0, alias="JEV_GATE_DEDUPE_JACCARD")
    # Same speaker + same action within this much transcript time, with no new
    # topic words, is a rephrase of the request that already fired (0 = off).
    # Judge whole spoken sentences (fragments re-joined) instead of ASR fragments.
    sentence_lines: bool = Field(default=True, alias="JEV_GATE_SENTENCE_LINES")
    # A newest line still mid-sentence waits this long for its end (0 = off).
    unfinished_hold_s: float = Field(default=4.0, ge=0.0, alias="JEV_GATE_UNFINISHED_HOLD_S")
    rephrase_window_s: float = Field(default=30.0, ge=0.0, alias="JEV_GATE_REPHRASE_WINDOW_S")
    # Unresolved triggers allowed per session before the gate stops firing.
    max_in_flight: int = Field(default=3, ge=1, alias="JEV_GATE_MAX_IN_FLIGHT")
    # An unresolved trigger stops counting as in flight after this long. Sized to
    # an answer's latency (built-in fallback ~3-5 s, MCP agent ~10-20 s) because
    # the live runtime does not call `mark_resolved` yet; with the default
    # cap that is at most 3 triggers per 20 s per session.
    in_flight_ttl_s: float = Field(default=20.0, gt=0.0, alias="JEV_GATE_IN_FLIGHT_TTL_S")

    # --- Cadence / context -------------------------------------------------
    # Minimum spacing of Jev calls per session (a new line ending in "?"
    # bypasses it). The session loop is expected to call ~every 5 s and on
    # each final segment; this only coalesces bursts.
    min_interval_s: float = Field(default=2.0, ge=0.0, alias="JEV_GATE_MIN_INTERVAL_S")
    # Transcript sent per call (seconds of speech, by segment end time).
    context_window_s: float = Field(default=30.0, gt=0.0, alias="JEV_GATE_CONTEXT_WINDOW_S")
    # Lines that may trigger: the most recent ones within this window ...
    candidate_window_s: float = Field(default=15.0, gt=0.0, alias="JEV_GATE_CANDIDATE_WINDOW_S")
    # ... plus never-judged lines (spoken during debounce / at the in-flight cap)
    # up to this old.
    carry_window_s: float = Field(default=45.0, gt=0.0, alias="JEV_GATE_CARRY_WINDOW_S")
    # Each candidate is one Jev question (~300 input tokens).
    max_candidates: int = Field(default=6, ge=1, le=12, alias="JEV_GATE_MAX_CANDIDATES")
    max_context_chars: int = Field(default=3000, ge=200, alias="JEV_GATE_MAX_CONTEXT_CHARS")

    # --- Transport ---------------------------------------------------------
    timeout_s: float = Field(default=2.0, gt=0.0, alias="JEV_GATE_TIMEOUT_S")
    # Concurrent Jev calls across all sessions in this process.
    max_concurrent_calls: int = Field(default=8, ge=1, alias="JEV_GATE_MAX_CONCURRENT_CALLS")
    callback_timeout_s: float = Field(default=5.0, gt=0.0, alias="JEV_GATE_CALLBACK_TIMEOUT_S")

    # --- Optional fast responder -------------------------------------------
    responder_enabled: bool = Field(default=False, alias="JEV_GATE_RESPONDER_ENABLED")
    # How long an agent has to claim a trigger before the fallback answers.
    claim_wait_s: float = Field(default=3.0, ge=0.0, alias="JEV_GATE_CLAIM_WAIT_S")
    responder_max_results: int = Field(
        default=3, ge=1, le=10, alias="JEV_GATE_RESPONDER_MAX_RESULTS"
    )

    @field_validator("live_gate_provider", mode="before")
    @classmethod
    def _known_provider(cls, value: object) -> str:
        text = str(value or "").strip().lower()
        return text if text in GATE_PROVIDERS else PROVIDER_MOCK

    def provider_available(self, provider: str) -> bool:
        """A provider is usable iff its key is set (the master switch is separate)."""
        if provider == PROVIDER_MOCK:
            return True
        if provider == PROVIDER_JEV:
            return bool(self.jev_api_key.get_secret_value().strip())
        if provider == PROVIDER_OPENAI:
            return bool(self.openai_decisions_api_key.get_secret_value().strip()) and bool(
                self.openai_decisions_model.strip() and self.openai_decisions_base_url.strip()
            )
        return False

    @property
    def available_providers(self) -> tuple[str, ...]:
        return tuple(p for p in GATE_PROVIDERS if self.provider_available(p))

    @property
    def default_provider(self) -> str:
        """`LIVE_GATE_PROVIDER` when usable, else the first usable provider."""
        if self.provider_available(self.live_gate_provider):
            return self.live_gate_provider
        available = self.available_providers
        return available[0] if available else self.live_gate_provider

    def resolve_provider(self, requested: str | None) -> str:
        """The provider a session asking for `requested` actually gets."""
        if requested and self.provider_available(requested):
            return requested
        return self.default_provider

    def threshold_for(self, provider: str) -> float:
        return self.openai_threshold if provider == PROVIDER_OPENAI else self.threshold

    def timeout_for(self, provider: str) -> float:
        return self.openai_timeout_s if provider == PROVIDER_OPENAI else self.timeout_s

    @property
    def configured(self) -> bool:
        """True when the gate may run: switched on and holding at least one provider key."""
        return self.jev_gate_enabled and bool(self.available_providers)


@lru_cache
def get_jev_settings() -> JevGateSettings:
    return JevGateSettings()
