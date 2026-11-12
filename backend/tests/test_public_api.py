"""API smoke tests with a generated audio waveform and invented session title."""

from __future__ import annotations

from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from app.live.mock_provider import SAMPLE_LINE
from tests.test_public_providers import sample_pcm


def test_offline_live_session_and_audio_ingest(client):
    cfg = client.get("/api/live/config")
    assert cfg.status_code == 200
    assert cfg.json()["asr_configured"] is True
    assert cfg.json()["gate"]["default_provider"] == "mock"
    response = client.post(
        "/api/live/sessions", json={"title": "Fictional library demo", "recap": False}
    )
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    assert response.json()["gate_provider"] == "mock"
    pcm = sample_pcm(1.0)
    uploaded = client.post(f"/api/live/sessions/{sid}/audio?offset_samples=0", content=pcm)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["received_samples"] == len(pcm) // 2
    assert client.get(f"/api/live/sessions/{sid}").status_code == 200
    assert client.post(f"/api/live/sessions/{sid}/stop", json={"discard": True}).status_code == 202


def test_recap_and_distribution_are_not_available(client):
    response = client.post("/api/live/sessions", json={"title": "Demo", "recap": True})
    assert response.status_code == 501
    created = client.post("/api/live/sessions", json={"title": "Demo"})
    assert created.status_code == 201
    sid = created.json()["id"]
    assert client.post(f"/api/live/sessions/{sid}/recap").status_code == 501
    assert (
        client.patch(
            f"/api/live/sessions/{sid}", json={"share_with_participants": True}
        ).status_code
        == 501
    )


def test_local_demo_rejects_dns_rebinding_host(client):
    assert client.get("/api/live/config", headers={"Host": "evil.invalid"}).status_code == 403


def test_dev_proxy_cannot_relay_lan_browser(client):
    path = "/api/live/config"
    assert client.get(path, headers={
        "Host": "localhost:8000", "Origin": "http://192.0.2.5:5173"
    }).status_code == 403
    assert client.get(path, headers={
        "Host": "localhost:8000", "X-Forwarded-For": "192.0.2.5"
    }).status_code == 403


def test_cross_site_http_write_is_rejected(client):
    result = client.post("/api/live/sessions", json={"title": "Fictional"},
                         headers={"Origin": "https://evil.invalid"})
    assert result.status_code == 403
    result = client.post("/api/live/sessions", json={"title": "Fictional"},
                         headers={"Sec-Fetch-Site": "cross-site"})
    assert result.status_code == 403


def test_cross_site_websocket_origin_is_rejected(client):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            f"/api/live/sessions/{uuid4()}/events", headers={"Origin": "https://evil.invalid"}
        ):
            pass
    assert error.value.code == 4403


def test_fixture_is_clearly_synthetic():
    assert "sample project" in SAMPLE_LINE.lower()
