"""OpenAI Decisions API provider for the Live Record gate.

The gate (`app.live.jev_gate.JevGate`) asks one typed `choice` question per
candidate line ("which action does this line call for, or none?"). This
client answers the very same question set with the OpenAI Decisions API
(`POST {base_url}/decisions`, model `gpt-6-luna`; docs:
developers.openai.com/api/docs/guides/decisions), so the gate's windowing,
firing rules, dedupe and debounce are identical for both providers.

Translation (Jev System One → Decisions):

* `state` (the transcript window text) → `input` (a text string).
* `{name: {"type": "choice", "instructions": ..., "criteria": {value: desc}}}`
  → `questions: [{"type": "choice", "name", "instructions",
  "choices": [{"value", "description"}]}]`, in the same order (`none` first).
* Answers `[{"type": "choice", "name", "choice", "probabilities":
  [{"value", "probability"}], "confidence"}]` → `ChoiceAnswer` keyed by name
  (answers come back in question order; an unnamed answer is matched by
  position). `predicate` answers map to `{"true": p, "false": 1 - p}`.
  `refusal` answers are dropped (that line simply does not fire).

Transport mirrors `JevClient`: one keep-alive pool per process, a hard
per-call budget, typed `JevError`s that never contain the key or the
transcript. Retries also mirror Jev: none inside a call — a failed tick
leaves its lines unjudged, the next tick re-asks them, and the gate backs off
after repeated failures (60 s straight away for auth / billing failures,
which do not fix themselves).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from app.live.jev_gate import ChoiceAnswer, JevError, JevResult, JevTimeout
from app.live.jev_settings import (
    DEFAULT_OPENAI_DECISIONS_BASE_URL,
    DEFAULT_OPENAI_DECISIONS_MODEL,
    PROVIDER_OPENAI,
)

_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})
# 429s that are not rate limits: retrying (or waiting a few seconds) is pointless.
_FATAL_CODES = frozenset(
    {"billing_not_active", "insufficient_quota", "invalid_api_key", "account_deactivated"}
)


def to_decision_questions(questions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Jev-style question mapping → Decisions `questions` array (order preserved)."""
    out: list[dict[str, Any]] = []
    for name, q in questions.items():
        qtype = str(q.get("type") or "choice")
        instructions = q.get("instructions") or f"Answer '{name}' about the input."
        if qtype == "choice":
            raw = q.get("choices", q.get("criteria")) or {}
            if isinstance(raw, Mapping):
                choices = [
                    {"value": str(value), "description": str(desc)} for value, desc in raw.items()
                ]
            else:
                choices = [
                    dict(item) if isinstance(item, Mapping) else {"value": str(item)}
                    for item in raw
                ]
            out.append(
                {"type": "choice", "name": name, "instructions": instructions, "choices": choices}
            )
        elif qtype in ("predicate", "boolean"):
            out.append({"type": "predicate", "name": name, "instructions": instructions})
        elif qtype == "score":
            out.append(
                {
                    "type": "score",
                    "name": name,
                    "instructions": instructions,
                    "levels": list(q.get("levels") or []),
                }
            )
        else:
            raise ValueError(f"unsupported question type {qtype!r}")
    return out


def to_decision_input(state: str | Mapping[str, Any] | Sequence[Any]) -> Any:
    """`state` → Decisions `input`: text stays text, structured state goes as `input_json`."""
    if isinstance(state, str):
        return state
    if isinstance(state, Mapping):
        return [{"role": "user", "content": [{"type": "input_json", "data": dict(state)}]}]
    return list(state)  # already a message array


def parse_answers(
    raw_answers: Any, question_names: Sequence[str]
) -> tuple[dict[str, ChoiceAnswer], int]:
    """Decisions `answers` → `{name: ChoiceAnswer}` plus the number of refusals."""
    answers: dict[str, ChoiceAnswer] = {}
    refusals = 0
    if not isinstance(raw_answers, list):
        return answers, refusals
    for index, raw in enumerate(raw_answers):
        if not isinstance(raw, Mapping):
            continue
        name = raw.get("name")
        if name is None and index < len(question_names):
            name = question_names[index]
        if name is None:
            continue
        kind = raw.get("type")
        if kind == "refusal":
            refusals += 1
            continue
        confidence = raw.get("confidence")
        conf = float(confidence) if isinstance(confidence, int | float) else None
        if kind == "predicate":
            p = raw.get("probability")
            if not isinstance(p, int | float):
                continue
            p = max(0.0, min(1.0, float(p)))
            answers[str(name)] = ChoiceAnswer(
                choice="true" if p >= 0.5 else "false",
                probabilities={"true": p, "false": 1.0 - p},
                confidence=conf,
            )
            continue
        probs: dict[str, float] = {}
        for item in raw.get("probabilities") or ():
            if not isinstance(item, Mapping):
                continue
            prob = item.get("probability")
            if not isinstance(prob, int | float):
                continue
            # Score answers carry the level label; choice answers the value.
            key = item.get("label") if kind == "score" else item.get("value")
            if key is None:
                key = item.get("value")
            probs[str(key)] = float(prob)
        choice = raw.get("choice")
        if choice is None and probs:
            choice = max(probs, key=lambda k: probs[k])
        answers[str(name)] = ChoiceAnswer(
            choice=str(choice) if choice is not None else "",
            probabilities=probs,
            confidence=conf,
        )
    return answers, refusals


def _error_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    err = body.get("error") if isinstance(body, Mapping) else None
    if not isinstance(err, Mapping):
        return None
    code = err.get("code") or err.get("type")
    return str(code)[:64] if code else None


class OpenAIDecisionsClient:
    """Minimal async Decisions client with the `JevAsker` interface."""

    provider = PROVIDER_OPENAI

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_OPENAI_DECISIONS_BASE_URL,
        model: str = DEFAULT_OPENAI_DECISIONS_MODEL,
        timeout_s: float = 3.0,
        max_connections: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenAI Decisions API key is empty")
        self._url = base_url.rstrip("/") + "/decisions"
        self._model = model
        self._timeout_s = timeout_s
        self.refusals = 0
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
                "User-Agent": "rgs-live-gate/1",
            },
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
                keepalive_expiry=90.0,
            ),
            transport=transport,
        )

    @property
    def model(self) -> str:
        return self._model

    async def ask(
        self,
        state: str | Mapping[str, Any] | Sequence[Any],
        questions: Mapping[str, Mapping[str, Any]],
        *,
        timeout_s: float | None = None,
    ) -> JevResult:
        budget = timeout_s if timeout_s is not None else self._timeout_s
        names = list(questions)
        payload = {
            "model": self._model,
            "input": to_decision_input(state),
            "questions": to_decision_questions(questions),
        }
        started = time.perf_counter()
        try:
            async with asyncio.timeout(budget):
                resp = await self._client.post(self._url, json=payload)
        except (TimeoutError, httpx.TimeoutException):
            raise JevTimeout(
                f"OpenAI Decisions call exceeded {budget:.2f}s", retryable=True
            ) from None
        except httpx.HTTPError as exc:
            raise JevError(
                f"OpenAI Decisions transport error: {type(exc).__name__}", retryable=True
            ) from None
        latency_ms = (time.perf_counter() - started) * 1000.0
        if resp.status_code != 200:
            code = _error_code(resp)
            fatal = resp.status_code in (401, 403) or code in _FATAL_CODES
            raise JevError(
                f"OpenAI Decisions HTTP {resp.status_code}" + (f" ({code})" if code else ""),
                status=resp.status_code,
                retryable=resp.status_code in _RETRYABLE_STATUS and not fatal,
                code=code,
                fatal=fatal,
            )
        try:
            body = resp.json()
        except ValueError:
            raise JevError(
                "OpenAI Decisions returned a non-JSON body", status=resp.status_code
            ) from None
        if not isinstance(body, Mapping):
            raise JevError("OpenAI Decisions returned an unexpected body", status=resp.status_code)
        answers, refusals = parse_answers(body.get("answers"), names)
        self.refusals += refusals
        usage = body.get("usage") or {}
        return JevResult(
            answers=answers,
            model=body.get("model") or self._model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
