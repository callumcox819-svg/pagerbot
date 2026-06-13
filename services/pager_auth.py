"""Pager login: Playwright or cookie import."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def parse_cookie_string(raw: str) -> dict[str, str]:
    """Parse Cookie header or document.cookie style string."""
    raw = raw.strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


async def login_with_playwright(email: str, password: str) -> dict[str, str]:
    """Headless login at pager.co.ua/sign-in. Requires: playwright install chromium."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto("https://www.pager.co.ua/sign-in", wait_until="networkidle", timeout=90000)

            # Clerk sign-in flow (selectors may need adjustment)
            email_input = page.locator('input[name="identifier"], input[type="email"]').first
            await email_input.wait_for(timeout=30000)
            await email_input.fill(email)
            cont = page.get_by_role("button", name=re.compile(r"continue|продовж|далі", re.I))
            if await cont.count():
                await cont.first.click()
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_timeout(1500)
            pass_input = page.locator('input[name="password"], input[type="password"]').first
            await pass_input.wait_for(timeout=30000)
            await pass_input.fill(password)
            sign_in = page.get_by_role("button", name=re.compile(r"sign in|увійти|continue", re.I))
            if await sign_in.count():
                await sign_in.first.click()
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_url(re.compile(r"/chats|/uk/|/en/"), timeout=90000)
            cookies_list = await context.cookies()
            cookies = {c["name"]: c["value"] for c in cookies_list}
            if not cookies.get("__session") and not any("session" in k.lower() for k in cookies):
                raise RuntimeError("Login finished but no session cookie found")
            return cookies
        finally:
            await browser.close()


async def authenticate(
    *,
    email: str = "",
    password: str = "",
    cookie_raw: str = "",
) -> dict[str, Any]:
    if cookie_raw.strip():
        cookies = parse_cookie_string(cookie_raw)
        if not cookies:
            raise ValueError("Could not parse cookies")
        return {"cookies": cookies, "method": "cookie"}

    if email and password:
        try:
            cookies = await login_with_playwright(email, password)
            return {"cookies": cookies, "method": "playwright"}
        except Exception as exc:
            logger.exception("Playwright login failed")
            raise RuntimeError(
                f"Login failed: {exc}. Try /import_cookies with Cookie from DevTools."
            ) from exc

    raise ValueError("Need email+password or cookies")
