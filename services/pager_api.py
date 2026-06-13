"""Pager REST API client (session cookies)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class PagerAPIError(Exception):
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Pager API {status}: {body[:200]}")


def _extract_org_from_payload(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("id", "organizationId", "orgId"):
            val = data.get(key)
            if val and str(val).startswith("org_"):
                return str(val)
        for key in ("organizations", "items", "data"):
            nested = data.get(key)
            if isinstance(nested, list) and nested:
                found = _extract_org_from_payload(nested[0])
                if found:
                    return found
    if isinstance(data, list) and data:
        return _extract_org_from_payload(data[0])
    return ""


def _org_from_html(html: str) -> str:
    for pattern in (
        r'"orgId"\s*:\s*"(org_[^"]+)"',
        r'"organizationId"\s*:\s*"(org_[^"]+)"',
        r"orgId=(org_[^&\"'\s]+)",
    ):
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return ""


def _org_slug_from_html(html: str) -> str:
    for pattern in (
        r"/(?:uk|en)/([a-z0-9_-]+)/chats",
        r'"slug"\s*:\s*"([a-z0-9_-]+)"',
        r'"orgSlug"\s*:\s*"([a-z0-9_-]+)"',
    ):
        match = re.search(pattern, html, re.I)
        if match:
            slug = match.group(1).lower()
            if slug not in {"chats", "sign-in", "en", "uk", "api"}:
                return slug
    return ""


def _extract_org_slug_from_payload(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("slug", "orgSlug", "organizationSlug"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for key in ("organization", "org"):
            nested = data.get(key)
            if isinstance(nested, dict):
                found = _extract_org_slug_from_payload(nested)
                if found:
                    return found
    if isinstance(data, list) and data:
        return _extract_org_slug_from_payload(data[0])
    return ""


class PagerClient:
    def __init__(
        self,
        base_url: str,
        cookies: dict[str, str],
        org_id: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = cookies
        self.org_id = (org_id or "").strip()
        self.org_slug = ""

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

    async def discover_org_id(self) -> str:
        if self.org_id:
            return self.org_id

        for path in ("/api/organization", "/api/organizations"):
            try:
                data = await self._request("GET", path)
                org_id = _extract_org_from_payload(data)
                if org_id:
                    self.org_id = org_id
                    return org_id
            except PagerAPIError as exc:
                logger.debug("discover org via %s: %s", path, exc)

        try:
            data = await self._request("GET", "/api/channel")
            if isinstance(data, list):
                for item in data:
                    org_id = str(
                        item.get("organizationId") or item.get("orgId") or ""
                    ).strip()
                    if org_id.startswith("org_"):
                        self.org_id = org_id
                        return org_id
        except PagerAPIError as exc:
            logger.debug("discover org via channel: %s", exc)

        try:
            url = f"{self.base_url}/chats"
            headers = {
                "Accept": "text/html",
                "Cookie": self._cookie_header(),
                "Referer": f"{self.base_url}/",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    html = await resp.text()
                    org_id = _org_from_html(html)
                    if org_id:
                        self.org_id = org_id
                        return org_id
        except Exception as exc:
            logger.debug("discover org via /chats html: %s", exc)

        return ""

    async def discover_org_slug(self) -> str:
        if self.org_slug:
            return self.org_slug

        if self.org_id:
            for path in (f"/api/organization?orgId={self.org_id}", "/api/organization"):
                try:
                    data = await self._request("GET", path)
                    slug = _extract_org_slug_from_payload(data)
                    if slug:
                        self.org_slug = slug
                        return slug
                except PagerAPIError as exc:
                    logger.debug("discover org slug via %s: %s", path, exc)

        try:
            headers = {
                "Accept": "text/html",
                "Cookie": self._cookie_header(),
                "Referer": f"{self.base_url}/",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/chats",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                    allow_redirects=True,
                ) as resp:
                    final_url = str(resp.url)
                    match = re.search(r"/(?:uk|en)/([^/]+)/chats", final_url, re.I)
                    if match:
                        self.org_slug = match.group(1)
                        return self.org_slug
                    html = await resp.text()
                    slug = _org_slug_from_html(html)
                    if slug:
                        self.org_slug = slug
                        return slug
        except Exception as exc:
            logger.debug("discover org slug via /chats redirect: %s", exc)

        return ""

    async def _ensure_org_id(self) -> str:
        org_id = await self.discover_org_id()
        if not org_id:
            raise PagerAPIError(
                400,
                '{"error":"Organization ID required — could not auto-detect orgId"}',
            )
        return org_id

    async def list_conversations(self, page: int = 1, page_size: int = 30) -> list[dict]:
        org_id = await self._ensure_org_id()
        data = await self._request(
            "GET",
            "/api/conversation",
            params={"orgId": org_id, "pageSize": page_size, "page": page},
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

    async def list_channels_api(self) -> list[dict[str, str]]:
        """All Messenger/IG channels from Pager API."""
        org_id = await self._ensure_org_id()
        for params in ({"orgId": org_id}, None):
            try:
                data = await self._request("GET", "/api/channel", params=params)
            except PagerAPIError as exc:
                logger.debug("list_channels_api params=%s: %s", params, exc)
                continue
            if not isinstance(data, list):
                continue
            out: list[dict[str, str]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("id") or item.get("channelId") or "").strip()
                name = str(item.get("name") or cid).strip()
                if cid:
                    out.append({"channel_id": cid, "name": name})
            if out:
                return sorted(out, key=lambda x: x["name"].lower())
        logger.warning("GET /api/channel empty — fallback to conversations")
        return await self.list_channels_from_conversations()

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
        org_id = await self._ensure_org_id()
        org_slug = await self.discover_org_slug()
        pager_user_id = ""
        convs = await self.list_conversations(page_size=1)
        if convs:
            org_id = str(convs[0].get("organizationId") or org_id)
            self.org_id = org_id
            ru = convs[0].get("responsibleUser") or {}
            pager_user_id = str(ru.get("id") or convs[0].get("responsibleuserId") or "")
        if not org_slug:
            org_slug = await self.discover_org_slug()
        return {
            "ok": True,
            "org_id": org_id,
            "org_slug": org_slug,
            "pager_user_id": pager_user_id,
        }
