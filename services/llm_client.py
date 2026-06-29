"""Unified LLM client — one API key for OpenRouter or OpenAI."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def resolve_llm_api_key() -> str:
    """Single key: OpenRouter preferred, OpenAI fallback."""
    return (
        (os.getenv("OPENROUTER_API_KEY") or "").strip()
        or (os.getenv("OPENAI_API_KEY") or "").strip()
    )


def llm_api_base() -> str:
    explicit = (os.getenv("LLM_API_BASE") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if (os.getenv("OPENROUTER_API_KEY") or "").strip():
        return "https://openrouter.ai/api/v1"
    return "https://api.openai.com/v1"


def llm_model() -> str:
    return (
        (os.getenv("LLM_MODEL") or "").strip()
        or "openai/gpt-4o-mini"
    )


def llm_router_mode() -> str:
    """off | learn (observe only) | fallback (scripts when rules fail)."""
    raw = (os.getenv("PAGER_LLM_ROUTER") or "").strip().lower()
    if raw in ("0", "false", "no", "off", ""):
        return "off"
    if raw in ("learn", "observe", "watch"):
        return "learn"
    if raw in ("1", "true", "yes", "fallback", "all"):
        return "fallback"
    return "off"


def llm_router_enabled() -> bool:
    return llm_router_mode() in ("learn", "fallback")


def llm_router_may_send() -> bool:
    """False in learn mode — LLM must not trigger outbound messages."""
    return llm_router_mode() == "fallback"


def llm_router_strict() -> bool:
    """No autonomous pause/escalate — only pick fixed script keys."""
    return (os.getenv("PAGER_LLM_STRICT") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _request_headers(api_key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if "openrouter.ai" in llm_api_base():
        ref = (os.getenv("OPENROUTER_HTTP_REFERER") or "https://pager.co.ua").strip()
        title = (os.getenv("OPENROUTER_APP_NAME") or "pager-ai-bot").strip()
        headers["HTTP-Referer"] = ref
        headers["X-Title"] = title
    return headers


def _parse_json_content(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def chat_completion(
    messages: list[dict[str, Any]],
    *,
    api_key: str = "",
    model: str = "",
    max_tokens: int = 400,
    temperature: float = 0.1,
) -> str:
    key = (api_key or resolve_llm_api_key()).strip()
    if not key:
        return ""
    payload = {
        "model": (model or llm_model()).strip(),
        "messages": messages,
        "max_tokens": max(64, int(max_tokens)),
        "temperature": float(temperature),
    }
    url = f"{llm_api_base()}/chat/completions"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=_request_headers(key),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await resp.text()
                if resp.status >= 400:
                    logger.warning("LLM %s -> %s", resp.status, body[:200])
                    return ""
                data = json.loads(body)
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return str(msg.get("content") or "").strip()
    except Exception:
        logger.exception("LLM chat_completion failed")
        return ""


async def chat_completion_json(
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> dict[str, Any]:
    raw = await chat_completion(messages, **kwargs)
    return _parse_json_content(raw)
