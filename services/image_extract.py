"""Extract account / game ID from image URL (optional OpenAI vision)."""

from __future__ import annotations

import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

_ACCOUNT_RE = re.compile(r"ACCOUNT\s*(\d+)", re.I)
_ID16_RE = re.compile(r"\b(16\d{6,})\b")
_ID17_RE = re.compile(r"\b(17\d{6,})\b")
_ID10_EG_RE = re.compile(r"\b(10\d{8,})\b")


async def download_image(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            return await resp.read()


async def extract_id_from_image_url(
    url: str, openai_key: str = "", *, geo: str = "zm"
) -> str:
    if openai_key:
        try:
            return await _vision_openai(url, openai_key, geo=geo)
        except Exception:
            logger.exception("OpenAI vision failed")
    return ""


def _game_id_geo(geo: str) -> str:
    if geo == "eg":
        return "10 or 17"
    return "17" if geo in ("dj", "cm", "zm") else "16"


async def _vision_openai(url: str, api_key: str, *, geo: str = "zm") -> str:
    import base64

    data = await download_image(url)
    b64 = base64.standard_b64encode(data).decode("ascii")
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "This is a casino app screenshot. Extract the ACCOUNT number or "
                            "game ID (often starts with "
                            + _game_id_geo(geo)
                            + "). Reply with digits only, or NONE."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": 50,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
    text = (body["choices"][0]["message"]["content"] or "").strip()
    if text.upper() == "NONE":
        return ""
    if geo == "eg":
        m = _ID10_EG_RE.search(text) or _ID17_RE.search(text)
    elif geo in ("dj", "cm", "zm"):
        m = _ID17_RE.search(text)
    else:
        m = _ID16_RE.search(text)
    if not m:
        m = _ID16_RE.search(text) or _ID17_RE.search(text) or _ID10_EG_RE.search(text)
    return m.group(1) if m else ""


def looks_like_game_id(gid: str, *, geo: str = "zm") -> bool:
    g = (gid or "").strip()
    if not g.isdigit() or len(g) < 8:
        return False
    if geo == "eg":
        return g.startswith("17") or (g.startswith("10") and len(g) >= 10)
    if geo in ("dj", "cm", "zm"):
        return g.startswith("17")
    return g.startswith("16")


def extract_id_from_text(text: str, *, geo: str = "zm") -> str:
    if geo == "eg":
        m = _ID10_EG_RE.search(text or "") or _ID17_RE.search(text or "")
        if m:
            return m.group(1)
    elif geo in ("dj", "cm", "zm"):
        m = _ID17_RE.search(text or "")
        if m:
            return m.group(1)
    m = _ID16_RE.search(text or "")
    if m:
        return m.group(1)
    m = _ACCOUNT_RE.search(text or "")
    if m:
        return m.group(1)
    return ""
