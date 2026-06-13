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
    org_id: str,
    org_slug: str,
    user_id: str = "",
    locale: str = "uk",
) -> dict[str, str]:
    """Open chat in browser session and POST /api/message from page context."""
    from playwright.async_api import async_playwright

    slug = (org_slug or "").strip()
    oid = (org_id or "").strip()
    if not slug or not oid:
        raise RuntimeError("org_slug and org_id required for browser send")

    clean = {
        k: v for k, v in cookies.items() if not k.startswith("_pager_") and v
    }
    if not clean:
        raise RuntimeError("No session cookies for browser send")

    chat_url = f"{PAGER_BASE}/{locale}/{slug}/chats/{conv_id}"
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
    uid = (user_id or "").strip()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            user_agent=UA,
            locale="en-US",
            viewport={"width": 1280, "height": 720},
        )
        await context.add_cookies(
            [
                {
                    "name": name,
                    "value": value,
                    "domain": ".pager.co.ua",
                    "path": "/",
                }
                for name, value in clean.items()
            ]
        )
        page = await context.new_page()
        try:
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(3000)

            if "sign-in" in page.url:
                raise RuntimeError("Browser session expired (redirected to sign-in)")

            for pattern in (
                r"take chat|take the chat|взяти чат|взят.*чат",
                r"assign.*me|призначити мене",
            ):
                btn = page.get_by_role(
                    "button", name=re.compile(pattern, re.I)
                )
                if await btn.count():
                    await btn.first.click()
                    await page.wait_for_timeout(1000)
                    logger.info("browser take conv=%s", conv_id[:8])
                    break

            if uid:
                take_result = await page.evaluate(
                    """async ({convId, orgId, userId}) => {
                        const url = `/api/conversation/${convId}?userId=${encodeURIComponent(userId)}&orgId=${encodeURIComponent(orgId)}`;
                        const resp = await fetch(url, {
                            method: 'PATCH',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                responsibleUserId: userId,
                                conversationState: 'read',
                            }),
                        });
                        return {status: resp.status, body: (await resp.text()).slice(0, 200)};
                    }""",
                    {"convId": conv_id, "orgId": oid, "userId": uid},
                )
                logger.info(
                    "browser PATCH take conv=%s status=%s",
                    conv_id[:8],
                    take_result.get("status"),
                )

            send_result = await page.evaluate(
                """async ({orgId, convId, text}) => {
                    const resp = await fetch(
                        `/api/message?orgId=${encodeURIComponent(orgId)}`,
                        {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({conversationId: convId, text: text}),
                        }
                    );
                    const body = await resp.text();
                    let parsed = null;
                    try { parsed = JSON.parse(body); } catch (_) {}
                    return {
                        status: resp.status,
                        body: body.slice(0, 400),
                        isDelivered: parsed && parsed.isDelivered,
                        facebookMessageId: parsed && parsed.facebookMessageId,
                    };
                }""",
                {"orgId": oid, "convId": conv_id, "text": text},
            )

            status = int(send_result.get("status") or 0)
            if status >= 400:
                raise RuntimeError(
                    f"Browser fetch POST failed {status}: {send_result.get('body', '')[:120]}"
                )

            if not send_result.get("isDelivered") and not send_result.get(
                "facebookMessageId"
            ):
                raise RuntimeError(
                    f"Browser POST ok but not delivered: {send_result.get('body', '')[:120]}"
                )

            logger.info(
                "browser message sent conv=%s chars=%s fb=%s",
                conv_id[:8],
                len(text),
                str(send_result.get("facebookMessageId") or "")[:12],
            )
            return {"ok": "true", "method": "browser_fetch"}
        finally:
            await browser.close()
