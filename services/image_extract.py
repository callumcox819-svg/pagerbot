"""Extract account / game ID from image URL (OpenRouter / OpenAI vision)."""

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

_FB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _download_cookies(cookies: dict[str, str] | None) -> dict[str, str] | None:
    if not cookies:
        return None
    return {
        str(k): str(v)
        for k, v in cookies.items()
        if v and not str(k).startswith("_pager_")
    }


async def download_image(
    url: str, *, cookies: dict[str, str] | None = None
) -> bytes:
    headers = {
        "User-Agent": _FB_UA,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    low = (url or "").lower()
    if "fbcdn.net" in low or "facebook.com" in low:
        headers["Referer"] = "https://www.facebook.com/"
    jar = _download_cookies(cookies)
    async with aiohttp.ClientSession(cookies=jar, headers=headers) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            return await resp.read()


def _parse_game_id_text(text: str, *, geo: str) -> str:
    raw = (text or "").strip()
    if not raw or raw.upper() == "NONE":
        return ""
    if geo == "eg":
        m = _ID10_EG_RE.search(raw) or _ID17_RE.search(raw)
    else:
        m = _ID17_RE.search(raw)
    if not m:
        m = _ID17_RE.search(raw) or _ID16_RE.search(raw) or _ID10_EG_RE.search(raw)
    return m.group(1) if m else ""


async def classify_screenshot_kind(
    url: str, api_key: str = "", *, cookies: dict[str, str] | None = None
) -> str:
    data = await analyze_success_screenshot(url, api_key, cookies=cookies)
    return str(data.get("kind") or "other").lower().replace("deposit_profile", "deposit")


async def analyze_success_screenshot(
    url: str,
    api_key: str = "",
    *,
    geo: str = "zm",
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Vision: deposit profile (balance), payment receipt, game ID, link errors."""
    if not api_key or not url:
        return {"is_success": False, "kind": "other", "balance": "", "game_id": ""}
    import base64

    from services.llm_client import chat_completion_json

    try:
        data = await download_image(url, cookies=cookies)
    except Exception:
        logger.warning("screenshot download failed url=%s", (url or "")[:80])
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
    url: str,
    api_key: str = "",
    *,
    geo: str = "zm",
    cookies: dict[str, str] | None = None,
) -> str:
    if not api_key or not url:
        return ""
    analysis = await analyze_success_screenshot(
        url, api_key, geo=geo, cookies=cookies
    )
    gid = str(analysis.get("game_id") or "").strip()
    if gid and looks_like_game_id(gid, geo=geo):
        return gid
    import base64

    from services.llm_client import chat_completion

    try:
        data = await download_image(url, cookies=cookies)
    except Exception:
        return ""
    b64 = base64.standard_b64encode(data).decode("ascii")
    text = await chat_completion(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "This is a casino app screenshot. Extract the ACCOUNT number or "
                            "game ID (often starts with "
                            + ("10 or 17" if geo == "eg" else "17")
                            + "). Reply with digits only, or NONE."
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
        max_tokens=50,
    )
    return _parse_game_id_text(text, geo=geo)


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
