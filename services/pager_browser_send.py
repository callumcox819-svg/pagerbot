"""Send Pager messages via headless browser when REST API fails."""

from __future__ import annotations

import logging
import re

from services.pager_auth import PAGER_BASE, UA

logger = logging.getLogger(__name__)


async def send_message_via_browser(
    cookies: dict[str, str],
    *,
    conv_id: str,
    text: str,
    org_slug: str,
    locale: str = "uk",
) -> dict[str, str]:
    """Open chat in Chromium, take it if needed, send like a human operator."""
    from playwright.async_api import async_playwright

    slug = (org_slug or "").strip()
    if not slug:
        raise RuntimeError("org_slug required for browser send")

    clean = {
        k: v
        for k, v in cookies.items()
        if not k.startswith("_pager_") and v
    }
    if not clean:
        raise RuntimeError("No session cookies for browser send")

    chat_url = f"{PAGER_BASE}/{locale}/{slug}/chats/{conv_id}"
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1280, "height": 720},
        )
        pw_cookies = [
            {
                "name": name,
                "value": value,
                "domain": ".pager.co.ua",
                "path": "/",
            }
            for name, value in clean.items()
        ]
        await context.add_cookies(pw_cookies)
        page = await context.new_page()
        try:
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2500)

            for pattern in (
                r"take chat|take the chat|взяти чат|взят.*чат",
                r"assign.*me|призначити мене",
            ):
                btn = page.get_by_role(
                    "button", name=re.compile(pattern, re.I)
                )
                if await btn.count():
                    await btn.first.click()
                    await page.wait_for_timeout(800)
                    logger.info("browser take conv=%s", conv_id[:8])
                    break

            editor = page.locator(
                '[contenteditable="true"][role="textbox"], '
                'textarea[placeholder], '
                '[data-testid="message-input"], '
                'div[contenteditable="true"]'
            ).last
            await editor.wait_for(state="visible", timeout=30000)
            await editor.click()
            await editor.fill(text)
            await page.wait_for_timeout(300)

            sent = page.get_by_role(
                "button",
                name=re.compile(r"send|надісл|відправ", re.I),
            )
            if await sent.count():
                await sent.last.click()
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_timeout(2500)

            err = page.locator(
                '[class*="error"], [class*="failed"], [aria-label*="error" i]'
            )
            if await err.count():
                raise RuntimeError("Pager UI shows send error after submit")

            logger.info(
                "browser message sent conv=%s chars=%s",
                conv_id[:8],
                len(text),
            )
            return {"ok": "true", "method": "browser"}
        finally:
            await browser.close()
