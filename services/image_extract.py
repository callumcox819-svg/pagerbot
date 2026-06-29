"""Extract account / game ID from image URL (optional OpenAI vision)."""

from __future__ import annotations

import logging
import re
from typing import Any

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


async def classify_screenshot_kind(url: str, api_key: str = "") -> str:
    data = await analyze_success_screenshot(url, api_key)
    return str(data.get("kind") or "other").lower().replace("deposit_profile", "deposit")


async def analyze_success_screenshot(
    url: str, api_key: str = "", *, geo: str = "zm"
) -> dict[str, Any]:
    """Vision: deposit profile (balance), payment receipt, game ID, link errors."""
    if not api_key or not url:
        return {"is_success": False, "kind": "other", "balance": "", "game_id": ""}
    import base64

    from services.llm_client import chat_completion_json

    try:
        data = await download_image(url)
    except Exception:
        logger.warning("screenshot download failed")
        return {"is_success": False, "kind": "other", "balance": "", "game_id": ""}
    b64 = base64.standard_b64encode(data).decode("ascii")
    raw = await chat_completion_json(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Analyze this phone screenshot from a 1xBet betting funnel. "
                            f"GEO context: {geo}. Reply JSON only:\n"
                            '{"is_success":true|false,'
                            '"kind":"deposit_profile|payment_receipt|game_id|'
                            'link_error|registration|other",'
                            '"balance":"e.g. 1000 XAF or empty",'
                            '"game_id":"digits only or empty",'
                            '"note":"short"}\n'
                            "is_success=true when:\n"
                            "- 1xBet profile/account shows balance (XAF, ZMW, EGP, FCFA)\n"
                            "- OR payment/deposit receipt visible\n"
                            "- OR numeric account/game ID visible (often starts with 17)\n"
                            "deposit_profile = 1xBet screen with name + balance + "
                            "deposit / Mes paris / Faire un dépôt button."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        api_key=api_key,
        max_tokens=120,
    )
    if not raw:
        return {"is_success": False, "kind": "other", "balance": "", "game_id": ""}
    parsed = raw
    gid = re.sub(r"\D", "", str(parsed.get("game_id") or ""))
    if gid and not looks_like_game_id(gid, geo=geo) and len(gid) < 8:
        gid = ""
    return {
        "is_success": bool(parsed.get("is_success")),
        "kind": str(parsed.get("kind") or "other").strip().lower(),
        "balance": str(parsed.get("balance") or "").strip(),
        "game_id": gid,
        "note": str(parsed.get("note") or "").strip(),
    }


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
    return "17"


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
    else:
        m = _ID17_RE.search(text)
    if not m:
        m = _ID17_RE.search(text) or _ID16_RE.search(text) or _ID10_EG_RE.search(text)
    return m.group(1) if m else ""


def looks_like_game_id(gid: str, *, geo: str = "zm") -> bool:
    g = (gid or "").strip()
    if not g.isdigit() or len(g) < 8:
        return False
    if geo == "eg":
        return g.startswith("17") or (g.startswith("10") and len(g) >= 10)
    return g.startswith("17")


def extract_id_from_text(text: str, *, geo: str = "zm") -> str:
    if geo == "eg":
        m = _ID10_EG_RE.search(text or "") or _ID17_RE.search(text or "")
        if m:
            return m.group(1)
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
