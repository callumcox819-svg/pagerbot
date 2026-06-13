"""Pager REST API client (session cookies)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp

from config import DEFAULT_ORG_ID_BY_SLUG, resolve_pager_org_id
from services.pager_auth import UA

logger = logging.getLogger(__name__)


def is_session_error(exc: PagerAPIError) -> bool:
    """Pager often returns 400 'Organization ID required' when cookies expired."""
    if exc.status in (401, 403):
        return True
    body = (exc.body or "").lower()
    return exc.status == 400 and "organization id required" in body


def _clean_cookies(cookies: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in cookies.items() if not k.startswith("_pager_")}


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
    script = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.S,
    )
    if script:
        try:
            found = _extract_org_from_payload(json.loads(script.group(1)))
            if found:
                return found
        except json.JSONDecodeError:
            pass

    for pattern in (
        r'"orgId"\s*:\s*"(org_[^"]+)"',
        r'"organizationId"\s*:\s*"(org_[^"]+)"',
        r"orgId=(org_[^&\"'\s]+)",
        r"(org_[a-zA-Z0-9]{20,})",
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
        *,
        org_slug: str = "",
        locale: str = "uk",
        org_id_fallback: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = cookies
        slug = (org_slug or "").strip()
        self.org_slug = slug
        self.locale = (locale or "uk").strip() or "uk"
        self.org_id_fallback = (org_id_fallback or "").strip()
        self.org_id = resolve_pager_org_id(
            org_id,
            org_id_fallback,
            org_slug=slug,
        )

    def _api_headers(self) -> dict[str, str]:
        referer = f"{self.base_url}/"
        if self.org_slug:
            referer = f"{self.base_url}/{self.locale}/{self.org_slug}/chats"
        return {
            "Accept": "*/*",
            "User-Agent": UA,
            "Cookie": self._cookie_header(),
            "Referer": referer,
            "Origin": self.base_url,
        }

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in _clean_cookies(self.cookies).items())

    async def warm_session(self) -> None:
        """Load org chats page — refreshes cookie context before API calls."""
        html = await self._fetch_chats_html()
        if html and not self.org_id:
            org_id = _org_from_html(html)
            if org_id:
                self.org_id = org_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._api_headers()
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
                    cookie_keys = sorted(_clean_cookies(self.cookies).keys())
                    body_preview = ""
                    if json_body is not None:
                        body_preview = f" body={sorted(json_body.keys())}"
                    logger.warning(
                        "Pager API %s %s params=%s%s cookies=%s -> %s",
                        method,
                        path,
                        params,
                        body_preview,
                        cookie_keys,
                        text[:120],
                    )
                    raise PagerAPIError(resp.status, text)
                if not text:
                    return None
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text

    async def _fetch_chats_html(self) -> str:
        paths: list[str] = []
        if self.org_slug:
            paths.append(f"/{self.locale}/{self.org_slug}/chats")
        paths.extend([f"/{self.locale}/chats", "/chats"])
        headers = {
            "Accept": "text/html",
            "Cookie": self._cookie_header(),
            "Referer": f"{self.base_url}/",
        }
        async with aiohttp.ClientSession() as session:
            for path in paths:
                try:
                    async with session.get(
                        f"{self.base_url}{path}",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                        allow_redirects=True,
                    ) as resp:
                        html = await resp.text()
                        final = str(resp.url)
                        match = re.search(r"/(?:uk|en)/([^/]+)/chats", final, re.I)
                        if match and not self.org_slug:
                            slug = match.group(1).lower()
                            if slug not in {"chats", "sign-in", "en", "uk", "api"}:
                                self.org_slug = slug
                        if html:
                            return html
                except Exception as exc:
                    logger.debug("fetch chats html %s: %s", path, exc)
        return ""

    async def _try_org_from_conversations(self) -> str:
        """Some sessions return conversations without orgId query param."""
        try:
            data = await self._request(
                "GET",
                "/api/conversation",
                params={"pageSize": 1, "page": 1},
            )
            if isinstance(data, list) and data:
                org_id = str(
                    data[0].get("organizationId") or data[0].get("orgId") or ""
                ).strip()
                if org_id.startswith("org_"):
                    self.org_id = org_id
                    return org_id
        except PagerAPIError as exc:
            logger.debug("org from conversation (no orgId param): %s", exc)
        return ""

    async def _try_org_by_slug(self) -> str:
        if not self.org_slug:
            return ""
        for path in (
            f"/api/organization/{self.org_slug}",
            f"/api/organizations/{self.org_slug}",
        ):
            try:
                data = await self._request("GET", path)
                org_id = _extract_org_from_payload(data)
                if org_id:
                    self.org_id = org_id
                    return org_id
            except PagerAPIError as exc:
                logger.debug("discover org via %s: %s", path, exc)
        return ""

    async def discover_org_id(self) -> str:
        if self.org_id:
            return self.org_id

        if self.org_id_fallback:
            self.org_id = self.org_id_fallback
            return self.org_id

        if self.org_slug:
            known = DEFAULT_ORG_ID_BY_SLUG.get(self.org_slug.lower(), "")
            if known:
                self.org_id = known
                return self.org_id

        org_id = await self._try_org_from_conversations()
        if org_id:
            return org_id

        org_id = await self._try_org_by_slug()
        if org_id:
            return org_id

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
            html = await self._fetch_chats_html()
            if html:
                org_id = _org_from_html(html)
                if org_id:
                    self.org_id = org_id
                    return org_id
                if not self.org_slug:
                    slug = _org_slug_from_html(html)
                    if slug:
                        self.org_slug = slug
        except Exception as exc:
            logger.debug("discover org via chats html: %s", exc)

        return ""

    async def list_conversations(
        self,
        page: int = 1,
        page_size: int = 30,
        *,
        channel_id: str = "",
        status_id: str | None = None,
    ) -> list[dict]:
        org_id = await self._ensure_org_id()
        params: dict[str, Any] = {
            "orgId": org_id,
            "pageSize": page_size,
            "page": page,
        }
        if status_id is not None:
            params["statusId"] = status_id
        try:
            data = await self._request("GET", "/api/conversation", params=params)
        except PagerAPIError as exc:
            if not is_session_error(exc):
                raise
            await self.warm_session()
            data = await self._request("GET", "/api/conversation", params=params)
        convs = data if isinstance(data, list) else []
        if channel_id:
            convs = [c for c in convs if str(c.get("channelId") or "") == channel_id]
        return convs

    async def collect_conversations(
        self,
        enabled_channel_ids: set[str],
        *,
        max_pages: int = 5,
    ) -> list[dict]:
        """Chats for enabled channels: «Без статусу» + active funnel folders."""
        from services.status_ids import ACTIVE_FUNNEL_STATUS_IDS, is_no_status, should_process_conversation

        seen: dict[str, dict] = {}

        def _add(convs: list[dict]) -> None:
            for conv in convs:
                ch = str(conv.get("channelId") or "")
                if ch not in enabled_channel_ids:
                    continue
                if not should_process_conversation(conv):
                    continue
                cid = str(conv.get("id") or "")
                if cid:
                    seen[cid] = conv

        for channel_id in enabled_channel_ids:
            for page in range(1, max_pages + 1):
                convs = await self.list_conversations(
                    page=page,
                    page_size=50,
                    channel_id=channel_id,
                )
                if not convs:
                    break
                _add(convs)

        # «Без статусу» — extra pass (Pager tab often not in first pages per channel).
        for page in range(1, max_pages + 1):
            convs = await self.list_conversations(page=page, page_size=100)
            no_status = [c for c in convs if is_no_status(c)]
            if not no_status:
                break
            _add(no_status)

        for status_id in ACTIVE_FUNNEL_STATUS_IDS:
            for page in range(1, 3):
                convs = await self.list_conversations(
                    page=page,
                    page_size=50,
                    status_id=status_id,
                )
                if not convs:
                    break
                _add(convs)

        return list(seen.values())

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
                paths: list[str] = []
                if self.org_slug:
                    paths.append(f"/{self.locale}/{self.org_slug}/chats")
                paths.extend([f"/{self.locale}/chats", "/chats"])
                for path in paths:
                    async with session.get(
                        f"{self.base_url}{path}",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                        allow_redirects=True,
                    ) as resp:
                        final_url = str(resp.url)
                        match = re.search(r"/(?:uk|en)/([^/]+)/chats", final_url, re.I)
                        if match:
                            slug = match.group(1).lower()
                            if slug not in {"chats", "sign-in", "en", "uk", "api"}:
                                self.org_slug = slug
                                return slug
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

    async def list_messages(self, conv_id: str, page: int = 1, page_size: int = 50) -> list[dict]:
        org_id = await self._ensure_org_id()
        data = await self._request(
            "GET",
            "/api/message",
            params={
                "convId": conv_id,
                "pageSize": page_size,
                "page": page,
                "orgId": org_id,
            },
        )
        return data if isinstance(data, list) else []

    async def send_message(
        self,
        conv_id: str,
        text: str,
        *,
        channel_id: str = "",
        conv: dict | None = None,
        author_id: str = "",
    ) -> dict[str, Any]:
        org_id = await self._ensure_org_id()
        conv_data = conv or {}
        ch = (channel_id or str(conv_data.get("channelId") or "")).strip()
        author = (
            author_id
            or str(conv_data.get("responsibleuserId") or "")
            or str((conv_data.get("responsibleUser") or {}).get("id") or "")
        ).strip()

        params = {"orgId": org_id}
        base: dict[str, Any] = {"text": text}
        for key in ("pagePSID", "clientPSID", "clientId"):
            val = conv_data.get(key)
            if val:
                base[key] = val
        if ch:
            base["channelId"] = ch
        if author:
            base["authorId"] = author

        # Pager GET uses convId; POST accepts convId or conversationId depending on version.
        attempts: list[dict[str, Any]] = [
            {**base, "convId": conv_id},
            {**base, "conversationId": conv_id},
        ]
        if not ch and not author:
            attempts.append({"text": text, "conversationId": conv_id})

        last_exc: PagerAPIError | None = None
        for body in attempts:
            try:
                return await self._request(
                    "POST",
                    "/api/message",
                    params=params,
                    json_body=body,
                )
            except PagerAPIError as exc:
                last_exc = exc
                logger.debug(
                    "send_message keys=%s -> %s",
                    sorted(body.keys()),
                    exc.body[:120],
                )
        if last_exc:
            raise last_exc
        raise PagerAPIError(400, '{"error":"send_message failed"}')

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
        try:
            data = await self._request(
                "GET", "/api/channel", params={"orgId": org_id}
            )
        except PagerAPIError as exc:
            logger.warning("GET /api/channel orgId=%s: %s", org_id, exc)
            data = None
        if isinstance(data, list):
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
        try:
            return await self.list_channels_from_conversations()
        except PagerAPIError as exc:
            raise PagerAPIError(
                exc.status,
                f'{exc.body} (orgId={org_id or "missing"})',
            ) from exc

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
