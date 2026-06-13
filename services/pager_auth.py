"""Pager login: Clerk HTTP API (primary) or Playwright fallback."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

PAGER_BASE = "https://www.pager.co.ua"
CLERK_BASE = "https://clerk.pager.co.ua"
CLERK_JS_VERSION = "5.68.0"
CLERK_API_VERSION = "2024-10-01"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


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


def _jar_to_dict(jar: aiohttp.CookieJar) -> dict[str, str]:
    out: dict[str, str] = {}
    for cookie in jar:
        out[cookie.key] = cookie.value
    return out


async def _validate_cookies(cookies: dict[str, str]) -> dict[str, str]:
    from services.pager_api import PagerAPIError, PagerClient

    if not cookies:
        raise RuntimeError("После входа cookies пустые")

    client = PagerClient(PAGER_BASE, cookies)
    try:
        await client.probe_session()
    except PagerAPIError as exc:
        if exc.status in (401, 403):
            raise RuntimeError(
                "Сессия не принята Pager (401). Проверьте email и пароль."
            ) from exc
        logger.warning("probe_session returned %s, keeping cookies anyway", exc.status)
    logger.info("Login OK, cookie keys: %s", ", ".join(sorted(cookies.keys())))
    return cookies


async def login_with_clerk_http(email: str, password: str) -> dict[str, str]:
    """Sign in via Clerk Frontend API — no browser needed."""
    params = {
        "_clerk_js_version": CLERK_JS_VERSION,
        "__clerk_api_version": CLERK_API_VERSION,
    }
    headers = {
        "User-Agent": UA,
        "Origin": PAGER_BASE,
        "Referer": f"{PAGER_BASE}/sign-in",
    }
    form_headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}

    jar = aiohttp.CookieJar(unsafe=True)
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
        async with session.get(f"{PAGER_BASE}/sign-in", headers=headers) as resp:
            await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"Pager sign-in page HTTP {resp.status}")

        async with session.post(
            f"{CLERK_BASE}/v1/client",
            params=params,
            headers={**headers, "Content-Type": "application/json"},
            json={},
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"Clerk client init failed ({resp.status}): {body[:200]}")

        async with session.post(
            f"{CLERK_BASE}/v1/client/sign_ins",
            params=params,
            headers=form_headers,
            data={"identifier": email},
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                errs = data.get("errors") or data
                msg = str(errs)
                if "identifier_not_found" in msg or "Couldn't find" in msg:
                    raise RuntimeError("Аккаунт с таким email не найден в Pager.")
                raise RuntimeError(f"Clerk: {msg[:300]}")

        sign_in = data.get("response") or data
        sign_in_id = sign_in.get("id")
        status = sign_in.get("status") or ""
        if not sign_in_id:
            raise RuntimeError(f"Clerk: no sign_in id in {data}")

        if status != "complete":
            async with session.post(
                f"{CLERK_BASE}/v1/client/sign_ins/{sign_in_id}/attempt_first_factor",
                params=params,
                headers=form_headers,
                data={"strategy": "password", "password": password},
            ) as resp:
                result = await resp.json()
                if resp.status >= 400:
                    errs = result.get("errors") or result
                    msg = str(errs)
                    if "password" in msg.lower() or "form_password_incorrect" in msg:
                        raise RuntimeError("Неверный пароль.")
                    raise RuntimeError(f"Clerk: {msg[:300]}")

            response = result.get("response") or result
            status = response.get("status") or ""
            if status != "complete":
                if status == "needs_second_factor":
                    raise RuntimeError(
                        "На аккаунте включена 2FA — отключите или используйте cookies."
                    )
                raise RuntimeError(f"Clerk: вход не завершён (status={status})")

        async with session.get(f"{PAGER_BASE}/chats", headers=headers) as resp:
            await resp.text()

        async with session.get(
            f"{PAGER_BASE}/api/conversation",
            params={"pageSize": 1, "page": 1},
            headers=headers,
        ) as resp:
            if resp.status == 401:
                raise RuntimeError("Pager API 401 после входа.")

        return await _validate_cookies(_jar_to_dict(jar))


async def login_with_playwright(email: str, password: str) -> dict[str, str]:
    """Headless browser login — fallback if Clerk HTTP fails."""
    from playwright.async_api import async_playwright

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True, args=launch_args)
        except Exception as exc:
            msg = str(exc)
            if "Executable doesn't exist" in msg:
                raise RuntimeError(
                    "Chromium не установлен на сервере. Пересоберите Docker-образ."
                ) from exc
            raise

        context = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()
        try:
            await page.goto(
                f"{PAGER_BASE}/sign-in",
                wait_until="domcontentloaded",
                timeout=90000,
            )

            email_input = page.locator(
                'input[name="identifier"], input[type="email"], #identifier-field'
            ).first
            await email_input.wait_for(state="visible", timeout=30000)
            await email_input.fill(email)

            cont = page.get_by_role(
                "button", name=re.compile(r"continue|продовж|далі|next", re.I)
            )
            if await cont.count():
                await cont.first.click()
            else:
                await page.keyboard.press("Enter")

            pass_input = page.locator(
                'input[name="password"], input[type="password"], #password-field'
            ).first
            await pass_input.wait_for(state="visible", timeout=30000)
            await pass_input.fill(password)

            sign_in = page.get_by_role(
                "button",
                name=re.compile(r"sign in|sign-in|увійти|continue|продовж", re.I),
            )
            if await sign_in.count():
                await sign_in.last.click()
            else:
                await page.keyboard.press("Enter")

            try:
                await page.wait_for_url(re.compile(r"/chats"), timeout=90000)
            except Exception:
                path = await page.evaluate("() => window.location.pathname")
                if "sign-in" in path:
                    err_el = page.locator(
                        '[class*="cl-formFieldErrorText"], [class*="formFieldError"], [role="alert"]'
                    )
                    err_text = ""
                    if await err_el.count():
                        err_text = (await err_el.first.inner_text()).strip()
                    hint = err_text or "Остались на странице входа"
                    raise RuntimeError(f"{hint}. Проверьте email/пароль.")
                await page.goto(
                    f"{PAGER_BASE}/chats",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )

            await page.wait_for_timeout(2000)
            cookies_list = await context.cookies()
            cookies = {c["name"]: c["value"] for c in cookies_list}
            return await _validate_cookies(cookies)
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
        cookies = await _validate_cookies(cookies)
        return {"cookies": cookies, "method": "cookie"}

    if email and password:
        errors: list[str] = []
        for method_name, login_fn in (
            ("clerk_http", login_with_clerk_http),
            ("playwright", login_with_playwright),
        ):
            try:
                cookies = await login_fn(email, password)
                return {"cookies": cookies, "method": method_name}
            except RuntimeError as exc:
                logger.warning("%s login failed: %s", method_name, exc)
                errors.append(f"{method_name}: {exc}")
            except Exception as exc:
                logger.exception("%s login error", method_name)
                errors.append(f"{method_name}: {exc}")

        raise RuntimeError("\n".join(errors))

    raise ValueError("Need email+password or cookies")
