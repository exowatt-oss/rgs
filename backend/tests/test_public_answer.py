"""Fake HTTP provider; payloads are invented library-app content."""

from __future__ import annotations

import json

import httpx

from app.live.answer import GatewayLLM, gateway_config, parse_answer


def test_optional_answer_endpoint_requires_explicit_rgs_config(monkeypatch):
    monkeypatch.delenv("RGS_ANSWER_API_URL", raising=False)
    monkeypatch.delenv("RGS_ANSWER_API_KEY", raising=False)
    assert gateway_config() is None
    monkeypatch.setenv("RGS_ANSWER_API_URL", "https://api.example.org")
    monkeypatch.setenv("RGS_ANSWER_API_KEY", "fake-local-only")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "answer",
                        "input": {
                            "title": "Demo owner",
                            "answer_md": "Alice owns the sample docs.",
                            "grounded": True,
                        },
                    }
                ]
            },
        )

    answer = GatewayLLM(transport=httpx.MockTransport(handler)).complete(
        "This is a test.", "Who owns the sample docs?"
    )
    assert seen[0][0] == "https://api.example.org/v1/messages"
    assert parse_answer(answer)["title"] == "Demo owner"
