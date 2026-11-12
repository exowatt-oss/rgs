"""Authorization tests use invented identities and never contact an IdP."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.live import service
from app.models.live import LiveSpeaker


def test_absent_identity_returns_401_for_api_and_4401_for_socket(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.deps.user_from_headers", lambda _: None)
    assert client.get("/api/auth/me").status_code == 401
    with client.websocket_connect(f"/api/live/sessions/{uuid4()}/events") as ws:
        assert ws.receive_json()["close_code"] == 4401


def test_machine_identity_denied(client: TestClient, monkeypatch):
    monkeypatch.setattr("app.deps.user_from_headers", lambda _: SimpleNamespace(is_bot=True))
    assert client.get("/api/auth/me").status_code == 403


def test_separate_users_cannot_read_each_others_sessions(client: TestClient, monkeypatch):
    def user_from_test_header(headers: dict[str, str]):
        email = headers["x-test-email"]
        return SimpleNamespace(is_bot=False, email=email, name=email.split("@")[0], picture=None)

    monkeypatch.setattr("app.deps.user_from_headers", user_from_test_header)
    a = {"X-Test-Email": "alice@example.org"}
    b = {"X-Test-Email": "bob@example.org"}
    created = client.post("/api/live/sessions", json={"title": "Fictional demo"}, headers=a)
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert client.get("/api/auth/me", headers=a).json()["is_admin"] is False
    assert client.get("/api/auth/me", headers=b).json()["is_admin"] is False
    people = client.get("/api/live/people", headers=a).json()["people"]
    assert all(person["email"] == "alice@example.org" for person in people)
    assert client.get(f"/api/live/sessions/{sid}", headers=a).status_code == 200
    assert client.get(f"/api/live/sessions/{sid}", headers=b).status_code in {403, 404}
    assert (
        client.post(f"/api/live/sessions/{sid}/stop", json={"discard": True}, headers=a).status_code
        == 202
    )


def test_speaker_rename_cannot_probe_unrelated_local_accounts(client: TestClient, monkeypatch):
    def identity(headers: dict[str, str]):
        email = headers["x-test-email"]
        names = {
            "owner@example.org": "Owner Example",
            "attendee@example.org": "Attendee Example",
            "outsider@example.org": "Outsider Secret",
        }
        return SimpleNamespace(is_bot=False, email=email, name=names[email], picture=None)

    monkeypatch.setattr("app.deps.user_from_headers", identity)
    owner = {"X-Test-Email": "owner@example.org"}
    for email in ("attendee@example.org", "outsider@example.org"):
        assert client.get("/api/auth/me", headers={"X-Test-Email": email}).status_code == 200
    created = client.post(
        "/api/live/sessions",
        json={"title": "Fictional demo", "attendees": [{"email": "attendee@example.org"}]},
        headers=owner,
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    with SessionLocal() as db:
        db.add(
            LiveSpeaker(session_id=UUID(sid), label="S1", display_name="Speaker 1", color_index=0)
        )
        db.commit()

    path = f"/api/live/sessions/{sid}/speakers/S1"
    for body in ({}, {"display_name": "Guest"}):
        responses = [
            client.patch(path, json={**body, "person_email": email}, headers=owner)
            for email in ("outsider@example.org", "missing@example.org")
        ]
        assert all(response.status_code == 400 for response in responses)
        assert responses[0].json() == responses[1].json()
    # The legacy user_email binding must not provide a second account oracle.
    for email in ("outsider@example.org", "missing@example.org"):
        response = client.patch(
            path, json={"display_name": "Guest", "user_email": email}, headers=owner
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "No RGS user with that email."}
    with SessionLocal() as db:
        speaker = service._speaker_row(db, UUID(sid), "S1")
        assert speaker is not None
        assert speaker.display_name == "Speaker 1"
        assert speaker.person_email is None and speaker.user_id is None

    attendee = client.patch(path, json={"person_email": "attendee@example.org"}, headers=owner)
    assert attendee.status_code == 200, attendee.text
    assert attendee.json()["display_name"] == "Attendee Example"
    assert attendee.json()["person"]["email"] == "attendee@example.org"
    assert attendee.json()["user"]["name"] == "Attendee Example"


def test_require_contributor_fails_closed_without_identity():
    session = SimpleNamespace(started_by_user_id=uuid4())
    with pytest.raises(HTTPException) as error:
        service.require_contributor(session, None)
    assert error.value.status_code == 401


def test_admin_grant_is_explicit_and_revocable(client: TestClient, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr("app.deps.user_from_headers", lambda headers: SimpleNamespace(
        is_bot=False, email=headers["x-test-email"], name="Example", picture=None
    ))
    alice = {"X-Test-Email": "alice@example.org"}
    bob = {"X-Test-Email": "bob@example.org"}
    monkeypatch.setenv("ADMIN_EMAILS", "alice@example.org")
    get_settings.cache_clear()
    try:
        assert client.get("/api/auth/me", headers=alice).json()["is_admin"] is True
        assert client.get("/api/auth/me", headers=bob).json()["is_admin"] is False
        created = client.post("/api/live/sessions", headers=bob, json={"title": "Fictional"})
        sid = created.json()["id"]
        assert client.get(f"/api/live/sessions/{sid}", headers=alice).status_code == 200
        monkeypatch.setenv("ADMIN_EMAILS", "")
        get_settings.cache_clear()
        assert client.get("/api/auth/me", headers=alice).json()["is_admin"] is False
        assert client.get(f"/api/live/sessions/{sid}", headers=alice).status_code in {403, 404}
        assert client.post(f"/api/live/sessions/{sid}/stop", headers=bob,
                           json={"discard": True}).status_code == 202
    finally:
        get_settings.cache_clear()
