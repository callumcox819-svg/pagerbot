"""Pager REST API client (session cookies)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp

from config import (
    DEFAULT_ORG_ID_BY_SLUG,
    load_settings,
    resolve_operator_user_id,
    resolve_pager_org_id,
)

_settings = load_settings()
from services.pager_auth import UA

logger = logging.getLogger(__name__)


def is_session_error(exc: PagerAPIError) -> bool:
    """Detect stale Clerk session — triggers Playwright re-login."""
    if exc.status in (401, 403):
        return True
    body = (exc.body or "").lower()
    if exc.status == 400 and "organization id required" in body:
        return True
    if "invalid or expired token" in body:
        return True
    return False


def is_org_id_error(exc: PagerAPIError) -> bool:
    body = (exc.body or "").lower()
    return exc.status == 400 and "organization id required" in body


def message_delivered(result: Any) -> bool:
    """True when Pager confirms Messenger delivery."""
    if not isinstance(result, dict):
        return False
    if result.get("isDelivered") is True:
        return True
    fb_id = str(result.get("facebookMessageId") or "").strip()
    if fb_id:
        return True
    # SPA optimistic POST — accepted before FB assigns id
    if result.get("optimistic") is True:
        mid = str(result.get("id") or result.get("messageId") or "").strip()
        if mid or str(result.get("authorId") or "").strip():
            return True
    return False


def message_accepted(result: Any, operator_id: str = "") -> bool:
    """Delivered + from Support operator (not Facebook page ghost)."""
    if not message_delivered(result):
        return False
    uid = (operator_id or "").strip()
    if not uid:
        return True
    author = str(result.get("authorId") or "").strip()
    if author == uid:
        return True
    if not author:
        logger.warning(
            "message delivered authorId=null (fb=%s)",
            str(result.get("facebookMessageId") or "")[:12],
        )
    return False


def _extract_user_id(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("id", "userId", "pagerUserId"):
            val = str(data.get(key) or "").strip()
            if val.startswith("user_"):
                return val
        for key in ("user", "data"):
            nested = data.get(key)
            if nested:
                found = _extract_user_id(nested)
                if found:
                    return found
    return ""


def _clean_cookies(cookies: dict[str, str]) -> dict[str, str]:
    keep_pager = {"_pager_org_id", "_pager_user_id"}
    return {
        k: v
        for k, v in cookies.items()
        if not k.startswith("_pager_") or k in keep_pager
    }


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
        session_user_id: str = "",
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
        self.session_user_id = (session_user_id or "").strip()

    def _chat_referer(self, conv_id: str = "") -> str:
        if self.org_slug and conv_id:
            return f"{self.base_url}/{self.locale}/{self.org_slug}/chats/{conv_id}"
        if self.org_slug:
            return f"{self.base_url}/{self.locale}/{self.org_slug}/chats"
        return f"{self.base_url}/"

    def operator_user_id(self, author_id: str = "") -> str:
        """Pager operator for take-chat + send (Тех Саппорт only)."""
        return resolve_operator_user_id(
            author_id,
            self.session_user_id,
            _settings.pager_user_id,
            org_slug=self.org_slug,
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
        referer: str = "",
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._api_headers()
        if referer:
            headers["Referer"] = referer
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

    async def resolve_org_id_live(self) -> str:
        """Re-detect org from session (cookies / HTML) — not env defaults."""
        saved_org = self.org_id
        saved_fallback = self.org_id_fallback
        self.org_id = ""
        self.org_id_fallback = ""

        cookie_org = str(self.cookies.get("_pager_org_id") or "").strip()
        if cookie_org.startswith("org_"):
            self.org_id = cookie_org
            self.org_id_fallback = saved_fallback or cookie_org
            return cookie_org

        await self.warm_session()
        cookie_org = str(self.cookies.get("_pager_org_id") or "").strip()
        if cookie_org.startswith("org_"):
            self.org_id = cookie_org
            self.org_id_fallback = saved_fallback or cookie_org
            return cookie_org

        for getter in (self._try_org_from_conversations, self._try_org_by_slug):
            org_id = await getter()
            if org_id:
                self.org_id_fallback = saved_fallback or org_id
                return org_id

        try:
            html = await self._fetch_chats_html()
            if html:
                org_id = _org_from_html(html)
                if org_id:
                    self.org_id = org_id
                    if not self.org_slug:
                        slug = _org_slug_from_html(html)
                        if slug:
                            self.org_slug = slug
                    self.org_id_fallback = saved_fallback or org_id
                    return org_id
        except Exception as exc:
            logger.debug("resolve_org_id_live html: %s", exc)

        self.org_id = saved_org
        self.org_id_fallback = saved_fallback
        if self.org_id:
            return self.org_id
        return await self.discover_org_id()

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
        if channel_id:
            params["channelId"] = channel_id
        if status_id is not None:
            params["statusId"] = status_id
        try:
            data = await self._request("GET", "/api/conversation", params=params)
        except PagerAPIError as exc:
            if is_org_id_error(exc):
                org_id = await self.resolve_org_id_live()
                if not org_id:
                    raise
                params["orgId"] = org_id
                data = await self._request("GET", "/api/conversation", params=params)
            elif not is_session_error(exc):
                raise
            else:
                await self.warm_session()
                data = await self._request("GET", "/api/conversation", params=params)
        convs = data if isinstance(data, list) else []
        if channel_id:
            convs = [c for c in convs if str(c.get("channelId") or "") == channel_id]
        return convs

    async def list_statuses_api(self) -> list[dict[str, str]]:
        """Pager status folders from GET /api/status."""
        org_id = await self._ensure_org_id()
        try:
            data = await self._request(
                "GET", "/api/status", params={"orgId": org_id}
            )
        except PagerAPIError as exc:
            logger.warning("GET /api/status orgId=%s: %s", org_id, exc)
            return []
        if not isinstance(data, list):
            return []
        out: list[dict[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or item.get("statusId") or "").strip()
            name = str(item.get("name") or sid).strip()
            if sid:
                out.append({"status_id": sid, "name": name})
        return out

    async def collect_conversations(
        self,
        enabled_channel_ids: set[str],
        *,
        max_pages: int = 5,
        geo: str = "zm",
        channel_geo_map: dict[str, str] | None = None,
        channel_folders: dict[str, set[str] | None] | None = None,
        funnel_statuses: dict[str, str] | None = None,
    ) -> list[dict]:
        """Chats for enabled channels (legacy geo rules or per-channel folder pick)."""
        from services.status_ids import (
            ALL_INBOX_FOLDER_ID,
            NO_STATUS_FOLDER_ID,
            funnel_status_ids as _funnel_ids,
            is_no_status,
            normalize_enabled_folders,
            process_funnel_folders,
            resolve_funnel_statuses,
            should_process_conversation,
        )

        fs = funnel_statuses or resolve_funnel_statuses()
        funnel_ids = _funnel_ids(fs)
        seen: dict[str, dict] = {}
        cmap = channel_geo_map or {}
        default_geo = (geo or "zm").strip().lower() or "zm"

        def _conv_geo(channel_id: str) -> str:
            ch = (channel_id or "").strip()
            raw = str(cmap.get(ch) or "").strip().lower()
            if raw in ("zm", "eg", "dj", "cm"):
                return raw
            return default_geo

        def _add(convs: list[dict]) -> None:
            for conv in convs:
                ch = str(conv.get("channelId") or "")
                if ch not in enabled_channel_ids:
                    continue
                conv_geo = _conv_geo(ch)
                allowed = (channel_folders or {}).get(ch) if channel_folders else None
                if not should_process_conversation(
                    conv,
                    geo=conv_geo,
                    funnel_statuses=fs,
                    allowed_folders=allowed,
                ):
                    continue
                cid = str(conv.get("id") or "")
                if cid:
                    seen[cid] = conv

        async def _legacy_channel(channel_id: str) -> None:
            for page in range(1, max_pages + 1):
                convs = await self.list_conversations(
                    page=page,
                    page_size=50,
                    channel_id=channel_id,
                )
                if not convs:
                    break
                _add(convs)

        async def _legacy_global() -> None:
            for channel_id in enabled_channel_ids:
                await _legacy_channel(channel_id)

            for page in range(1, max(max_pages, 12) + 1):
                convs = await self.list_conversations(page=page, page_size=100)
                if not convs:
                    break
                _add([c for c in convs if is_no_status(c)])

            if process_funnel_folders():
                for status_id in funnel_ids:
                    for page in range(1, 3):
                        convs = await self.list_conversations(
                            page=page,
                            page_size=50,
                            status_id=status_id,
                        )
                        if not convs:
                            break
                        _add(convs)

        async def _resolve_allowed(allowed: set[str]) -> set[str]:
            from services.status_ids import normalize_enabled_folders

            specific, all_inbox = normalize_enabled_folders(allowed)
            if all_inbox:
                statuses = await self.list_statuses_api()
                return {NO_STATUS_FOLDER_ID} | {
                    s["status_id"] for s in statuses if s.get("status_id")
                }
            return specific

        async def _collect_no_status(channel_id: str) -> None:
            for page in range(1, max(max_pages, 15) + 1):
                convs = await self.list_conversations(
                    page=page,
                    page_size=100,
                    channel_id=channel_id,
                )
                if not convs:
                    break
                batch = [c for c in convs if is_no_status(c)]
                _add(batch)
                if len(convs) < 100:
                    break

        async def _collect_status(
            channel_id: str, status_id: str, *, pages: int
        ) -> None:
            for page in range(1, pages + 1):
                convs = await self.list_conversations(
                    page=page,
                    page_size=100,
                    channel_id=channel_id,
                    status_id=status_id,
                )
                if not convs:
                    break
                _add(convs)

        async def _collect_by_folders(channel_id: str, allowed: set[str]) -> None:
            allowed_eff = await _resolve_allowed(allowed)
            specific, all_inbox = normalize_enabled_folders(allowed)
            if all_inbox:
                logger.info(
                    "collect «Всі» channel=%s status_folders=%s",
                    channel_id[:8],
                    len(allowed_eff) - (1 if NO_STATUS_FOLDER_ID in allowed_eff else 0),
                )
            elif specific == {NO_STATUS_FOLDER_ID}:
                logger.info(
                    "collect «Без статусу» only channel=%s",
                    channel_id[:8],
                )
            status_pages = max(max_pages, 20 if ALL_INBOX_FOLDER_ID in allowed else 12)
            wait_sid = str(fs.get("wait_id") or "").strip()
            completed_sid = str(fs.get("completed") or "").strip()
            for status_id in allowed_eff:
                if status_id == NO_STATUS_FOLDER_ID:
                    await _collect_no_status(channel_id)
                else:
                    await _collect_status(
                        channel_id, status_id, pages=status_pages
                    )
            if (
                wait_sid
                and completed_sid
                and wait_sid in allowed_eff
                and completed_sid not in allowed_eff
            ):
                await _collect_status(
                    channel_id, completed_sid, pages=3
                )

        if not channel_folders:
            await _legacy_global()
            return list(seen.values())

        for channel_id in enabled_channel_ids:
            allowed = channel_folders.get(channel_id)
            if allowed is None:
                await _legacy_channel(channel_id)
                continue
            if not allowed:
                continue
            await _collect_by_folders(channel_id, allowed)

        # «Без статусу» for legacy channels only (no explicit folder config).
        legacy_channels = [
            cid
            for cid in enabled_channel_ids
            if channel_folders.get(cid) is None
        ]
        if legacy_channels:
            for page in range(1, max(max_pages, 12) + 1):
                convs = await self.list_conversations(page=page, page_size=100)
                if not convs:
                    break
                _add(
                    [
                        c
                        for c in convs
                        if is_no_status(c)
                        and str(c.get("channelId") or "") in legacy_channels
                    ]
                )
            if process_funnel_folders():
                for status_id in funnel_ids:
                    for page in range(1, 3):
                        convs = await self.list_conversations(
                            page=page,
                            page_size=50,
                            status_id=status_id,
                        )
                        if not convs:
                            break
                        _add(
                            [
                                c
                                for c in convs
                                if str(c.get("channelId") or "") in legacy_channels
                            ]
                        )

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
        params = {
            "convId": conv_id,
            "pageSize": page_size,
            "page": page,
            "orgId": org_id,
        }
        try:
            data = await self._request("GET", "/api/message", params=params)
        except PagerAPIError as exc:
            if not is_session_error(exc):
                raise
            await self.warm_session()
            data = await self._request("GET", "/api/message", params=params)
        return data if isinstance(data, list) else []

    async def wait_message_delivered(
        self,
        conv_id: str,
        text: str,
        *,
        user_id: str = "",
        timeout: float = 30.0,
        poll_interval: float = 0.7,
    ) -> bool:
        """Poll until Messenger assigns facebookMessageId (not optimistic ghost)."""
        uid = self.operator_user_id(user_id)
        needle = (text or "").strip().lower()[:72]
        if not needle:
            return False
        deadline = time.monotonic() + max(3.0, timeout)
        while time.monotonic() < deadline:
            try:
                messages = await self.list_messages(conv_id, page_size=15)
            except PagerAPIError:
                await asyncio.sleep(poll_interval)
                continue
            for msg in messages:
                if str(msg.get("messageDirection") or "").lower() not in (
                    "outgoing",
                    "out",
                ):
                    continue
                author = str(msg.get("authorId") or "").strip()
                if uid and author and author != uid:
                    continue
                body = (msg.get("text") or "").strip().lower()
                if needle[:40] not in body and body[:40] not in needle:
                    continue
                if msg.get("isDelivered") or msg.get("facebookMessageId"):
                    return True
                if msg.get("optimistic") and not msg.get("facebookMessageId"):
                    break
            await asyncio.sleep(poll_interval)
        return False

    async def resolve_session_user_id(self) -> str:
        """Logged-in Pager operator id (Clerk user_…)."""
        if self.session_user_id:
            return self.session_user_id

        org_id = await self._ensure_org_id()
        for path in ("/api/user/me", "/api/users/me", "/api/user"):
            try:
                data = await self._request("GET", path, params={"orgId": org_id})
                uid = _extract_user_id(data)
                if uid:
                    self.session_user_id = uid
                    return uid
            except PagerAPIError:
                continue

        try:
            from services.pager_auth import (
                CLERK_API_VERSION,
                CLERK_BASE,
                CLERK_JS_VERSION,
                extract_clerk_session_info,
            )

            params = {
                "_clerk_js_version": CLERK_JS_VERSION,
                "__clerk_api_version": CLERK_API_VERSION,
            }
            headers = self._api_headers()
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{CLERK_BASE}/v1/client",
                    params=params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status < 400:
                        payload = await resp.json()
                        info = extract_clerk_session_info(payload)
                        uid = str(info.get("pager_user_id") or "").strip()
                        if uid:
                            self.session_user_id = uid
                            return uid
        except Exception as exc:
            logger.debug("Clerk user lookup failed: %s", exc)

        return ""

    async def open_conversation(self, conv_id: str) -> dict[str, Any] | None:
        org_id = await self._ensure_org_id()
        try:
            data = await self._request(
                "GET",
                f"/api/conversation/{conv_id}",
                params={"orgId": org_id},
                referer=self._chat_referer(conv_id),
            )
            return data if isinstance(data, dict) else None
        except PagerAPIError as exc:
            logger.debug("open conv=%s: %s", conv_id[:8], exc.body[:80])
            return None

    async def _conversation_responsible(self, conv_id: str) -> str:
        conv = await self.open_conversation(conv_id)
        if not conv:
            return ""
        nested = conv.get("responsibleUser") or {}
        return str(
            conv.get("responsibleuserId")
            or conv.get("responsibleUserId")
            or nested.get("id")
            or ""
        ).strip()

    async def _take_confirmed(self, conv_id: str, user_id: str) -> bool:
        uid = (user_id or "").strip()
        if not uid:
            return False
        return await self._conversation_responsible(conv_id) == uid

    async def _wait_take(self, conv_id: str, user_id: str, *, attempts: int = 10) -> bool:
        for _ in range(attempts):
            if await self._take_confirmed(conv_id, user_id):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _operator_image_url(
        self, user_id: str, conv: dict[str, Any] | None = None
    ) -> str:
        uid = (user_id or "").strip()
        if conv:
            resp_user = conv.get("responsibleUser") or conv.get("responsibleuser") or {}
            if isinstance(resp_user, dict):
                url = str(resp_user.get("imageUrl") or "").strip()
                if url:
                    return url
        if not uid:
            return ""
        try:
            org_id = await self._ensure_org_id()
            data = await self._request(
                "GET",
                "/api/organizationMember",
                params={"orgId": org_id},
            )
            members = data if isinstance(data, list) else []
            for member in members:
                if not isinstance(member, dict):
                    continue
                user = member.get("user") if isinstance(member.get("user"), dict) else {}
                mid = str(
                    member.get("userId")
                    or member.get("pagerUserId")
                    or user.get("id")
                    or member.get("id")
                    or ""
                ).strip()
                if mid == uid:
                    return str(
                        member.get("imageUrl") or user.get("imageUrl") or ""
                    ).strip()
        except PagerAPIError:
            pass
        return ""

    async def send_message_spa(
        self,
        conv_id: str,
        text: str,
        *,
        user_id: str = "",
        channel_id: str = "",
        conv: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /api/message with SPA payload (author.imageUrl required)."""
        uid = self.operator_user_id(user_id)
        org_id = await self._ensure_org_id()
        referer = self._chat_referer(conv_id)
        conv_data: dict[str, Any] = dict(conv or {})
        if not conv_data:
            opened = await self.open_conversation(conv_id)
            if opened:
                conv_data = opened

        ch = (channel_id or str(conv_data.get("channelId") or "")).strip()
        nested = conv_data.get("channel")
        if not ch and isinstance(nested, dict):
            ch = str(nested.get("id") or "").strip()
        if not ch:
            raise PagerAPIError(
                400,
                json.dumps({"error": "channelId missing", "conv": conv_id[:8]}),
            )

        recipient = str(
            conv_data.get("clientPSID")
            or conv_data.get("clientPsid")
            or conv_data.get("recipient")
            or (conv_data.get("client") or {}).get("psid")
            or (conv_data.get("client") or {}).get("PSID")
            or ""
        ).strip()
        image_url = await self._operator_image_url(uid, conv_data)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        payload: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "channelId": ch,
            "text": text,
            "conversationId": conv_id,
            "messageDirection": "outgoing",
            "authorId": uid,
            "author": {"id": uid, "imageUrl": image_url or ""},
            "recipient": recipient,
            "createdAt": now,
            "updatedAt": now,
            "lastMessageAt": now,
            "optimistic": True,
            "isDelivered": None,
            "replyToMessageId": None,
        }
        params: dict[str, Any] = {"orgId": org_id}
        if uid:
            params["userId"] = uid
        result = await self._request(
            "POST",
            "/api/message",
            params=params,
            json_body=payload,
            referer=referer,
        )
        if not isinstance(result, dict):
            raise PagerAPIError(502, '{"error":"empty message response"}')
        logger.info(
            "SPA send conv=%s user=%s chars=%s fb=%s delivered=%s",
            conv_id[:8],
            (uid or "")[:16],
            len(text),
            str(result.get("facebookMessageId") or "")[:12],
            result.get("isDelivered"),
        )
        return result

    async def take_conversation(self, conv_id: str, user_id: str) -> bool:
        """Assign + take chat for operator (UI «Тех Саппорт взяв(-ла) чат»)."""
        uid = (user_id or "").strip()
        if not uid:
            return False

        if await self._take_confirmed(conv_id, uid):
            logger.info("take conv=%s user=%s (already)", conv_id[:8], uid[:16])
            return True

        org_id = await self._ensure_org_id()
        referer = self._chat_referer(conv_id)
        attempts: list[tuple[dict[str, Any], dict[str, Any] | None]] = [
            (
                {"userId": uid, "orgId": org_id},
                {
                    "responsibleUserId": uid,
                    "conversationState": "read",
                },
            ),
            (
                {"userId": uid, "orgId": org_id},
                {"responsibleUserId": uid},
            ),
            (
                {"userId": uid, "orgId": org_id},
                {"responsibleuserId": uid, "conversationState": "read"},
            ),
            ({"userId": uid}, {"responsibleUserId": uid}),
        ]
        last_exc: PagerAPIError | None = None
        for params, body in attempts:
            try:
                await self._request(
                    "PATCH",
                    f"/api/conversation/{conv_id}",
                    params=params,
                    json_body=body,
                    referer=referer,
                )
            except PagerAPIError as exc:
                last_exc = exc
                continue
            if await self._wait_take(conv_id, uid):
                logger.info("take conv=%s user=%s verified", conv_id[:8], uid[:16])
                return True
        if await self._take_confirmed(conv_id, uid):
            return True
        if last_exc:
            logger.warning(
                "take conv=%s failed: %s",
                conv_id[:8],
                last_exc.body[:120],
            )
        else:
            logger.warning(
                "take conv=%s not verified for user=%s",
                conv_id[:8],
                uid[:16],
            )
        return False

    async def prepare_outbound(
        self,
        conv_id: str,
        *,
        conv: dict | None = None,
        author_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """Warm session, open chat, always take it — returns (user_id, conv_data)."""
        await self.warm_session()
        user_id = self.operator_user_id(author_id)
        conv_data: dict[str, Any] = dict(conv or {})
        fresh = await self.open_conversation(conv_id)
        if fresh:
            conv_data = {**conv_data, **fresh}

        if user_id:
            taken = await self.take_conversation(conv_id, user_id)
            if not taken:
                raise PagerAPIError(
                    502,
                    json.dumps(
                        {
                            "error": "take chat failed — operator not assigned",
                            "conv": conv_id[:8],
                        }
                    ),
                )
            try:
                await self.mark_conversation_read(conv_id, user_id=user_id)
            except Exception:
                pass
            fresh = await self.open_conversation(conv_id)
            if fresh:
                conv_data = {**conv_data, **fresh}
        await self._fetch_conv_chat_page(conv_id)
        try:
            await self.list_messages(conv_id, page_size=1)
        except PagerAPIError:
            pass
        return user_id, conv_data

    async def _fetch_conv_chat_page(self, conv_id: str) -> None:
        """Open chat URL — same context as browser UI before POST /api/message."""
        if not self.org_slug or not conv_id:
            return
        path = f"/{self.locale}/{self.org_slug}/chats/{conv_id}"
        headers = {
            "Accept": "text/html",
            "Cookie": self._cookie_header(),
            "Referer": self._chat_referer(),
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}{path}",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=45),
                    allow_redirects=True,
                ) as resp:
                    await resp.text()
        except Exception as exc:
            logger.debug("fetch conv chat page %s: %s", conv_id[:8], exc)

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
        conv_data = dict(conv or {})
        user_id, conv_data = await self.prepare_outbound(
            conv_id, conv=conv_data, author_id=author_id
        )
        referer = self._chat_referer(conv_id)
        ch = (channel_id or str(conv_data.get("channelId") or "")).strip()
        if not ch:
            nested = conv_data.get("channel")
            if isinstance(nested, dict):
                ch = str(nested.get("id") or "").strip()
        if not ch:
            raise PagerAPIError(
                400,
                json.dumps({"error": "channelId missing", "conv": conv_id[:8]}),
            )

        # REST requires channelId in body (aiohttp is not browser UI session).
        body: dict[str, Any] = {
            "conversationId": conv_id,
            "text": text,
            "channelId": ch,
        }
        if user_id:
            body["authorId"] = user_id

        params: dict[str, Any] = {"orgId": org_id}
        if user_id:
            params["userId"] = user_id

        logger.info(
            "send conv=%s channel=%s user=%s chars=%s",
            conv_id[:8],
            ch[:8],
            (user_id or "")[:16],
            len(text),
        )

        try:
            result = await self._request(
                "POST",
                "/api/message",
                params=params,
                json_body=body,
                referer=referer,
            )
        except PagerAPIError as exc:
            logger.warning(
                "send failed conv=%s body=%s -> %s",
                conv_id[:8],
                sorted(body.keys()),
                exc.body[:200],
            )
            raise
        if not isinstance(result, dict):
            raise PagerAPIError(502, '{"error":"empty message response"}')
        if message_accepted(result, user_id):
            logger.info(
                "Pager message sent conv=%s user=%s chars=%s fb=%s",
                conv_id[:8],
                (user_id or "")[:16],
                len(text),
                str(result.get("facebookMessageId") or "")[:12],
            )
            return result
        if message_delivered(result):
            author = str(result.get("authorId") or "null")
            raise PagerAPIError(
                502,
                json.dumps(
                    {
                        "error": "Delivered as Facebook page, not operator",
                        "authorId": author,
                    }
                ),
            )
        raise PagerAPIError(
            502,
            json.dumps(
                {
                    "error": "Message not accepted",
                    "authorId": str(result.get("authorId") or ""),
                    "isDelivered": result.get("isDelivered"),
                }
            ),
        )

    async def post_message_minimal(
        self,
        conv_id: str,
        text: str,
        *,
        user_id: str = "",
    ) -> dict[str, Any]:
        """POST like operator UI after take: {conversationId, text} + userId query."""
        uid = self.operator_user_id(user_id)
        org_id = await self._ensure_org_id()
        referer = self._chat_referer(conv_id)
        params: dict[str, Any] = {"orgId": org_id}
        if uid:
            params["userId"] = uid
        body: dict[str, Any] = {"conversationId": conv_id, "text": text}
        logger.info(
            "post minimal conv=%s user=%s chars=%s",
            conv_id[:8],
            (uid or "")[:16],
            len(text),
        )
        result = await self._request(
            "POST",
            "/api/message",
            params=params,
            json_body=body,
            referer=referer,
        )
        if not isinstance(result, dict):
            raise PagerAPIError(502, '{"error":"empty message response"}')
        return result

    async def post_message_after_take(
        self,
        conv_id: str,
        text: str,
        *,
        user_id: str = "",
        channel_id: str = "",
    ) -> dict[str, Any]:
        """POST after browser take — UI-style body first, channelId body as fallback."""
        uid = self.operator_user_id(user_id)
        try:
            await self.list_messages(conv_id, page_size=3)
        except PagerAPIError:
            pass
        try:
            result = await self.post_message_minimal(conv_id, text, user_id=uid)
            if message_delivered(result):
                return result
        except PagerAPIError as exc:
            body_l = (exc.body or "").lower()
            if "imageurl" not in body_l and "channel" not in body_l:
                raise

        await self._fetch_conv_chat_page(conv_id)
        org_id = await self._ensure_org_id()
        referer = self._chat_referer(conv_id)
        ch = (channel_id or "").strip()
        if not ch:
            try:
                conv = await self.open_conversation(conv_id)
                ch = str(conv.get("channelId") or "").strip()
                nested = conv.get("channel")
                if not ch and isinstance(nested, dict):
                    ch = str(nested.get("id") or "").strip()
            except PagerAPIError:
                ch = ""
        if not ch:
            raise PagerAPIError(
                400,
                json.dumps({"error": "channelId missing", "conv": conv_id[:8]}),
            )
        params: dict[str, Any] = {"orgId": org_id}
        if uid:
            params["userId"] = uid
        body: dict[str, Any] = {
            "conversationId": conv_id,
            "text": text,
            "channelId": ch,
        }
        if uid:
            body["authorId"] = uid
        logger.info(
            "post after take conv=%s user=%s channel=%s chars=%s",
            conv_id[:8],
            (uid or "")[:16],
            ch[:8],
            len(text),
        )
        result = await self._request(
            "POST",
            "/api/message",
            params=params,
            json_body=body,
            referer=referer,
        )
        if not isinstance(result, dict):
            raise PagerAPIError(502, '{"error":"empty message response"}')
        return result

    async def send_operator_message(
        self,
        conv_id: str,
        text: str,
        *,
        conv: dict | None = None,
        author_id: str = "",
    ) -> dict[str, Any]:
        """Send like operator UI: POST {conversationId, text} + userId query only."""
        org_id = await self._ensure_org_id()
        user_id, conv_data = await self.prepare_outbound(
            conv_id, conv=conv, author_id=author_id
        )
        referer = self._chat_referer(conv_id)
        body: dict[str, Any] = {"conversationId": conv_id, "text": text}
        params: dict[str, Any] = {"orgId": org_id}
        if user_id:
            params["userId"] = user_id

        logger.info(
            "operator send conv=%s user=%s chars=%s",
            conv_id[:8],
            (user_id or "")[:16],
            len(text),
        )

        try:
            result = await self._request(
                "POST",
                "/api/message",
                params=params,
                json_body=body,
                referer=referer,
            )
        except PagerAPIError as exc:
            logger.warning(
                "operator send failed conv=%s -> %s",
                conv_id[:8],
                exc.body[:200],
            )
            raise

        if not isinstance(result, dict):
            raise PagerAPIError(502, '{"error":"empty message response"}')
        if message_accepted(result, user_id):
            logger.info(
                "operator sent conv=%s author=%s fb=%s",
                conv_id[:8],
                str(result.get("authorId") or "")[:16],
                str(result.get("facebookMessageId") or "")[:12],
            )
            return result
        if message_delivered(result):
            raise PagerAPIError(
                502,
                json.dumps(
                    {
                        "error": "Delivered as Facebook page, not operator",
                        "authorId": str(result.get("authorId") or ""),
                    }
                ),
            )
        raise PagerAPIError(
            502,
            json.dumps(
                {
                    "error": "operator message not accepted",
                    "authorId": str(result.get("authorId") or ""),
                    "isDelivered": result.get("isDelivered"),
                }
            ),
        )

    async def mark_conversation_read(
        self,
        conv_id: str,
        *,
        user_id: str = "",
    ) -> None:
        org_id = await self._ensure_org_id()
        params: dict[str, Any] = {"orgId": org_id}
        if user_id:
            params["userId"] = user_id
        try:
            await self._request(
                "PATCH",
                f"/api/conversation/{conv_id}",
                params=params,
                json_body={"conversationState": "read"},
            )
        except PagerAPIError as exc:
            logger.warning(
                "mark read conv=%s: %s",
                conv_id[:8],
                exc.body[:120],
            )

    async def patch_status(self, conv_id: str, status_id: str, user_id: str) -> dict[str, Any]:
        org_id = await self._ensure_org_id()
        return await self._request(
            "PATCH",
            f"/api/conversation/{conv_id}",
            params={"userId": user_id, "orgId": org_id},
            json_body={"statusId": status_id},
            referer=self._chat_referer(conv_id),
        )

    async def list_channels_api(self) -> list[dict[str, str]]:
        """All Messenger/IG channels from Pager API."""
        await self.warm_session()
        org_id = await self._ensure_org_id()
        data: Any = None
        for attempt in range(2):
            try:
                org_id = await self._ensure_org_id()
                data = await self._request(
                    "GET", "/api/channel", params={"orgId": org_id}
                )
                break
            except PagerAPIError as exc:
                if attempt == 0 and is_org_id_error(exc):
                    org_id = await self.resolve_org_id_live()
                    if not org_id:
                        raise
                    continue
                logger.warning("GET /api/channel orgId=%s: %s", org_id, exc)
                data = None
                break
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
        pager_user_id = await self.resolve_session_user_id()
        convs = await self.list_conversations(page_size=1)
        if convs:
            org_id = str(convs[0].get("organizationId") or org_id)
            self.org_id = org_id
            if not pager_user_id:
                pager_user_id = str(
                    convs[0].get("responsibleuserId")
                    or (convs[0].get("responsibleUser") or {}).get("id")
                    or ""
                )
                if pager_user_id:
                    self.session_user_id = pager_user_id
        if not org_slug:
            org_slug = await self.discover_org_slug()
        return {
            "ok": True,
            "org_id": org_id,
            "org_slug": org_slug,
            "pager_user_id": pager_user_id or self.session_user_id,
        }
