"""Synthetic fixtures only: prove offline providers and secure auth defaults."""

from __future__ import annotations

import asyncio
import math
import struct
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import get_settings
from app.deps import _identity
from app.live.gate import GateSegment, GateSessionContext, load_gate
from app.live.jev_settings import JevGateSettings, get_jev_settings
from app.live.mock_provider import SAMPLE_LINE, MockGate, MockTranscriber, ProviderGate
from app.live.settings import LiveSettings
from app.main import create_app


def sample_pcm(seconds: float = 1.0) -> bytes:
    """Generated sine wave, not a recording of a person or a call."""
    return b"".join(
        struct.pack("<h", int(1000 * math.sin(i / 40))) for i in range(int(seconds * 16000))
    )


def test_mock_transcription_is_synthetic_and_offline():
    client = MockTranscriber()
    a = client.transcribe(sample_pcm())
    b = client.transcribe(sample_pcm(2.0))
    assert a.text == b.text == SAMPLE_LINE
    assert len(a.words) == len(SAMPLE_LINE.split())
    assert {word.speaker for word in a.words} == {"0"}
    assert client.transcribe(bytes(32000)).words == ()


def test_mock_gate_emits_question_trigger_without_api():
    gate = MockGate()
    context = GateSessionContext(
        session_id=uuid4(),
        meeting_id=None,
        title="Fictional test",
        attendees=(),
        segments=(
            GateSegment(1, "S1", "Alice", 0, 2, "An example."),
            GateSegment(2, "S1", "Alice", 2, 4, SAMPLE_LINE),
        ),
        new_from_seq=2,
        interim_text="",
        last_seq=2,
        audio_seconds=4,
        on_trigger=lambda _: asyncio.sleep(0),
    )
    triggers = asyncio.run(gate.observe(context))
    assert len(triggers) == 1
    assert triggers[0].provider == "mock" and triggers[0].action == "find_owner"
    assert (
        asyncio.run(gate.observe(context.__class__(**{**context.__dict__, "new_from_seq": 3})))
        == []
    )


def test_defaults_have_no_external_provider_or_directory(monkeypatch):
    for key in (
        "NEMOTRON_BASE_URL",
        "NEMOTRON_API_KEY",
        "JEV_API_KEY",
        "OPENAI_DECISIONS_API_KEY",
        "LIVE_GATE_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)
    assert LiveSettings(_env_file=None).asr_provider == "mock"
    assert JevGateSettings(_env_file=None).available_providers == ("mock",)
    assert isinstance(load_gate(), ProviderGate)


def test_external_gate_requires_explicit_credentials_and_endpoint(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("JEV_ENDPOINT", "https://example.org/v1/systemone")
    get_jev_settings.cache_clear()
    try:
        gate = ProviderGate()
        assert "jev" in gate.settings.available_providers
        assert gate.external is not None
    finally:
        get_jev_settings.cache_clear()


def test_production_lifespan_rejects_dev_auth(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="forbidden in production"):
            with TestClient(create_app()):
                pass
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "app_env,environment",
    [
        ("prod", "production"),
        ("staging", "staging"),
        ("local", "staging"),
        ("staging", "development"),
        ("unknown", "unknown"),
    ],
)
def test_dev_identity_cannot_be_used_outside_local(monkeypatch, app_env, environment):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("ENVIRONMENT", environment)
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as error:
            _identity({})
        assert error.value.status_code == 500
    finally:
        get_settings.cache_clear()


def test_unsigned_proxy_headers_rejected_in_production(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setattr("app.deps.get_auth_settings", lambda: SimpleNamespace(
        mode="forwarded", require_jwt=False, trust_proxy_headers=True
    ))
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException) as error:
            _identity({"x-forwarded-user-email": "alice@example.org"})
        assert error.value.status_code == 500
    finally:
        get_settings.cache_clear()
