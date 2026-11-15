"""Minimal OpenRouter (OpenAI-compatible) chat client returning parsed JSON. httpx only."""
from __future__ import annotations

import json
import os
import re

import httpx

URL = "https://openrouter.ai/api/v1/chat/completions"
KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-5.5")
TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "180"))


class LLMError(Exception):
    pass


def configured() -> bool:
    return bool(KEY) and os.environ.get("DFS_MOCK") != "1"


def _parse(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start > 0:
        text = text[start:]
    return json.loads(text)


async def ask_json(client: httpx.AsyncClient, system: str, user: str, max_tokens: int = 8000) -> tuple[object, float]:
    """One chat call that must return JSON. Retries once on unparseable output. Returns (data, cost_usd)."""
    cost = 0.0
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(2):
        r = await client.post(URL, timeout=TIMEOUT, headers={"Authorization": f"Bearer {KEY}"}, json={
            "model": MODEL, "messages": messages, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"}, "usage": {"include": True},
        })
        if r.status_code >= 400:
            raise LLMError(f"OpenRouter {r.status_code}: {r.text[:300]}")
        body = r.json()
        cost += float((body.get("usage") or {}).get("cost") or 0)
        choice = (body.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        try:
            return _parse(text), cost
        except (json.JSONDecodeError, ValueError):
            if attempt:
                raise LLMError(f"Model did not return JSON: {text[:200]}")
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "That was not valid JSON. Reply with the JSON object only."}]
    raise LLMError("unreachable")
