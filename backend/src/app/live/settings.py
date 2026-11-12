"""Standalone live settings. Offline mock transcription is the default.

Opt in to a configured HTTP Nemotron router with ASR_PROVIDER=nemotron and
NEMOTRON_BASE_URL. SageMaker transport can be configured independently.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

NEMOTRON_MODEL = "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B"


class LiveSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(Path(__file__).resolve().parents[4] / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    # -------- Feature flag --------
    live_record_enabled: bool = Field(default=True)

    # -------- Transcription provider --------
    asr_provider: Literal["mock", "nemotron"] = Field(default="mock")

    # -------- Optional Nemotron SageMaker endpoint --------
    nemotron_aws_access_key_id: str = Field(default="")
    nemotron_aws_secret_access_key: str = Field(default="")
    nemotron_aws_region: str = Field(default="us-east-1")
    nemotron_endpoint_name: str = Field(default="nemotron-transcribe")
    nemotron_model: str = Field(default=NEMOTRON_MODEL)
    # Shared with the batch Nemotron ASR settings (same env names): use
    # boto3's default credential chain instead of the dedicated IAM user.
    nemotron_allow_default_credentials: bool = Field(default=False)
    # Optional operator-owned HTTP endpoint implementing the provider contract:
    # when set, `/invocations` on this base URL is used instead of SageMaker.
    nemotron_base_url: str = Field(default="")
    nemotron_api_key: str = Field(default="")
    # Live-only knobs (distinct env names from the batch path's).
    # Client-side read timeout per InvokeEndpoint. SageMaker's own sync limit
    # is 60 s; live windows are small, so fail fast and retry the next tick.
    live_nemotron_timeout_s: float = Field(default=30.0, gt=0, le=60)
    live_nemotron_max_tokens: int = Field(default=2048, gt=0)

    # -------- Windowing (live/windowing.py) --------
    # How often the trailing window is re-transcribed.
    live_step_s: float = Field(default=3.0, gt=0.5, le=30)
    # Audio before the commit point re-sent for acoustic/language context.
    live_left_context_s: float = Field(default=4.0, ge=0, le=10)
    # Words ending at least this far behind the audio head may be committed
    # once two consecutive hypotheses agree on them.
    live_stable_horizon_s: float = Field(default=2.0, ge=0.5, le=20)
    # Words this far behind the head are committed even without agreement.
    live_force_horizon_s: float = Field(default=6.0, ge=1, le=30)
    # Re-transcribe each diarization turn separately when the model wrote no
    # speaker tags over multi-speaker audio (see nemotron.needs_turn_split).
    live_turn_split_enabled: bool = True
    # Hard cap on one window's audio; well under SageMaker's 60 s limit.
    live_max_window_s: float = Field(default=30.0, ge=5, le=120)
    # Minimum new audio before a tick bothers calling the endpoint.
    live_min_new_audio_s: float = Field(default=0.8, ge=0, le=10)

    # -------- Speaker clustering (live/speakers.py) --------
    live_speaker_match_threshold: float = Field(default=0.55, ge=-1, le=1)
    live_speaker_new_threshold: float = Field(default=0.30, ge=-1, le=1)
    live_speaker_min_new_s: float = Field(default=2.5, ge=0)
    # Re-clustering: merge speakers whose voiceprints reach this cosine.
    live_speaker_merge_threshold: float = Field(default=0.65, ge=-1, le=1)
    live_max_speakers: int = Field(default=12, ge=1, le=50)
    live_embed_enabled: bool = Field(default=False)

    # -------- Speaker layer provider (live/diarizer.py) --------
    # `sortformer` (opt-in): an external diarizer assigns word speakers by
    # time overlap. The default uses Nemotron's speaker tags.
    # Needs an HTTP endpoint (diarizer URL or NEMOTRON_BASE_URL); otherwise,
    # and per window on any diarizer failure, Nemotron's tags are used.
    # `nemotron`: Nemotron's per-window speaker tags (+ the tracker above).
    live_diarizer: Literal["nemotron", "sortformer"] = Field(default="nemotron")
    # Diarizer base URL (`POST {url}/diarize`) and bearer token; empty = the
    # optional Nemotron endpoint (`NEMOTRON_BASE_URL` / `NEMOTRON_API_KEY`).
    live_diarizer_url: str = Field(default="")
    live_diarizer_api_key: str = Field(default="")
    # Audio re-diarized per window (ending at the window's end).
    live_diarizer_context_s: float = Field(default=60.0, ge=10, le=240)
    live_diarizer_timeout_s: float = Field(default=10.0, gt=0, le=60)
    # Voiceprint merges with a diarizer: only near-identical voiceprints.
    live_diarizer_merge_threshold: float = Field(default=0.8, ge=-1, le=1)

    # Long-context re-diarization (live/consolidate.py): every
    # `live_rediarize_interval_s` of new audio, re-transcribe the last
    # `live_rediarize_window_s` in one request and fix labels the short live
    # windows split (merges) or mislabelled (segment relabels). 0 disables it.
    live_rediarize_window_s: float = Field(default=180.0, ge=0, le=600)
    live_rediarize_interval_s: float = Field(default=40.0, ge=10, le=600)
    live_rediarize_timeout_s: float = Field(default=45.0, gt=0, le=120)

    # -------- Speaker naming (live/people.py, live/naming.py) --------
    # Directory defaults to locally signed-in users. No network roster.
    # LLM naming pass: who is each label (intros, address, replies, roster).
    live_naming_enabled: bool = Field(default=False)
    live_naming_model: str = Field(default="claude-sonnet-4-5-20250929")
    live_naming_timeout_s: float = Field(default=40.0, gt=0, le=120)
    # Rename at this confidence; between the two, only suggest ("maybe X?").
    live_naming_min_confidence: float = Field(default=0.7, ge=0, le=1)
    live_naming_suggest_confidence: float = Field(default=0.4, ge=0, le=1)
    # Periodic pass while someone is still unnamed; name cues run it sooner.
    live_naming_interval_s: float = Field(default=45.0, ge=5, le=600)
    live_naming_min_gap_s: float = Field(default=12.0, ge=0, le=600)

    # -------- Session lifecycle --------
    live_max_session_hours: float = Field(default=4.0, gt=0, le=12)
    # A live session with no audio for this long is finalized by the sweeper.
    live_idle_finalize_minutes: float = Field(default=20.0, gt=0)
    # PCM persisted to object storage in chunks of this many seconds.
    live_chunk_seconds: int = Field(default=10, ge=2, le=60)
    # Opt-in raw audio retention (live/audio_store.py): sessions started by
    # these owners (comma-separated emails) keep their PCM for N days so
    # transcription/speaker failures can be reproduced. Empty = keep none.
    live_audio_retention_emails: str = Field(default="")
    live_audio_retention_days: float = Field(default=7.0, gt=0, le=30)

    # -------- Findings / triggers --------
    live_findings_per_minute: int = Field(default=6, ge=1, le=60)
    live_trigger_claim_ttl_s: float = Field(default=60.0, gt=0)
    live_trigger_expire_s: float = Field(default=300.0, gt=0)
    # Built-in LLM answerer (`app.live.answer`) takes unclaimed gate triggers
    # after this delay. 0 disables it (external agents only).
    live_fallback_after_s: float = Field(default=0.0, ge=0)
    # LLM gateway model for answers / summaries (bare family alias).
    live_answer_model: str = Field(default="claude-sonnet-4-5-20250929")
    live_answer_timeout_s: float = Field(default=25.0, gt=0, le=120)

    # -------- Gate (app/live/gate.py) --------
    jev_gate_enabled: bool = Field(default=True)
    live_gate_interval_s: float = Field(default=5.0, ge=1, le=120)
    live_gate_timeout_s: float = Field(default=8.0, gt=0, le=60)
    live_gate_context_segments: int = Field(default=40, ge=1, le=500)

    @property
    def asr_configured(self) -> bool:
        if self.asr_provider == "mock":
            return True
        if self.nemotron_base_url:
            return True
        has_keys = bool(self.nemotron_aws_access_key_id and self.nemotron_aws_secret_access_key)
        return bool(self.nemotron_endpoint_name) and (
            has_keys or self.nemotron_allow_default_credentials
        )


@lru_cache(maxsize=1)
def get_live_settings() -> LiveSettings:
    return LiveSettings()
