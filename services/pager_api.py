"""Pager REST API client (session cookies)."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class PagerAPIError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Pager API {status}: {body[:200]}")


class PagerClient:
    def __init__(self, base_url: str, cookies: dict[str, str]) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = cookies

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = {
            "Accept": "*/*",
            "Cookie": self._cookie_header(),
            "Referer": f"{self.base_url}/",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise PagerAPIError(resp.status, text)
                if not text:
                    return None
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text

    async def list_conversations(self, page: int = 1, page_size: int = 30) -> list[dict]:
        data = await self._request(
            "GET",
            "/api/conversation",
            params={"pageSize": page_size, "page": page},
        )
        return data if isinstance(data, list) else []

    async def list_messages(self, conv_id: str, page: int = 1, page_size: int = 50) -> list[dict]:
        data = await self._request(
            "GET",
            "/api/message",
            params={"convId": conv_id, "pageSize": page_size, "page": page},
        )
        return data if isinstance(data, list) else []

    async def send_message(self, conv_id: str, text: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/message",
            json_body={"conversationId": conv_id, "text": text},
        )

    async def patch_status(self, conv_id: str, status_id: str, user_id: str) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"/api/conversation/{conv_id}",
            params={"statusId": status_id, "userId": user_id},
            json_body={"statusId": status_id},
        )

    async def list_channels_from_conversations(self) -> list[dict[str, str]]:
        """Derive unique channels from recent conversations."""
        seen: dict[str, str] = {}
        for page in (1, 2):
            convs = await self.list_conversations(page=page, page_size=50)
            for c in convs:
                ch = c.get("channel") or {}
                cid = (c.get("channelId") or "").strip()
                name = (ch.get("name") or cid).strip()
                if cid and cid not in seen:
                    seen[cid] = name
        return [{"channel_id": k, "name": v} for k, v in seen.items()]

    async def probe_session(self) -> dict[str, Any]:
        convs = await self.list_conversations(page_size=1)
        pager_user_id = ""
        org_id = ""
        if convs:
            org_id = str(convs[0].get("organizationId") or "")
            ru = convs[0].get("responsibleUser") or {}
            pager_user_id = str(ru.get("id") or convs[0].get("responsibleuserId") or "")
        return {"ok": True, "org_id": org_id, "pager_user_id": pager_user_id}
