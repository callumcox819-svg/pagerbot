"""Send Pager messages via browser — take chat first, then POST (like operator UI)."""

from __future__ import annotations

import logging
import re

from services.pager_auth import PAGER_BASE, UA

logger = logging.getLogger(__name__)

_TAKE_UI = (
    r"take chat|take the chat|take dialog|взяти чат|взяти діалог|взяв.*чат",
    r"assign.*me|призначити мене|забрати чат",
    r"^take$|^взяти$",
)


async def _verify_logged_in_operator(page, expected_uid: str) -> None:
    """Session must be Тех Саппорт, not Facebook/page personal account."""
    uid = (expected_uid or "").strip()
    clerk_uid = await page.evaluate(
        """async () => {
            try {
                const r = await fetch(
                    'https://clerk.pager.co.ua/v1/client?_clerk_js_version=5.68.0&__clerk_api_version=2024-10-01'
                );
                const d = await r.json();
                const client = d.response || d.client || d;
                const sessions = client.sessions || [];
                if (!sessions.length) return '';
                const user = sessions[0].user || {};
                return user.id || '';
            } catch (e) {
                return '';
            }
        }"""
    )
    clerk_uid = str(clerk_uid or "").strip()
    if clerk_uid and clerk_uid != uid:
        raise RuntimeError(
            f"Wrong Pager login: session user={clerk_uid[:20]}, "
            f"need operator={uid[:20]} (sapportteh / Тех Саппорт)"
        )
    if clerk_uid:
        logger.info("browser session operator=%s", clerk_uid[:16])


async def _browser_take_and_verify(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
) -> None:
    """Take chat as Тех Саппорт — wait for «взяв(-ла) чат» before send."""
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    if not uid:
        raise RuntimeError("operator user_id required to take chat")

    # 1) Assign responsible via API first
    patch_result = await page.evaluate(
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
        "browser PATCH assign conv=%s status=%s",
        conv_id[:8],
        patch_result.get("status"),
    )
    await page.wait_for_timeout(800)

    # 2) UI «взяти чат» — same as operator click
    for pattern in _TAKE_UI:
        btn = page.get_by_role("button", name=re.compile(pattern, re.I))
        if await btn.count():
            await btn.first.click()
            await page.wait_for_timeout(1500)
            logger.info("browser UI take conv=%s", conv_id[:8])
            break
    else:
        for pattern in (r"без відповідаль|no responsible|немає відповід",):
            label = page.get_by_text(re.compile(pattern, re.I))
            if await label.count():
                await label.first.click()
                await page.wait_for_timeout(600)
                me = page.get_by_text(re.compile(r"тех саппорт|тех.саппорт", re.I))
                if await me.count():
                    await me.first.click()
                    await page.wait_for_timeout(1200)
                    logger.info("browser assign dropdown conv=%s", conv_id[:8])
                break

    # 3) MUST see system «взяв чат» OR newResponsibleId before sending
    for attempt in range(12):
        check = await page.evaluate(
            """async ({convId, orgId, userId}) => {
                const convResp = await fetch(
                    `/api/conversation/${convId}?orgId=${encodeURIComponent(orgId)}`
                );
                const convBody = await convResp.text();
                let conv = null;
                try { conv = JSON.parse(convBody); } catch (_) {}

                const msgResp = await fetch(
                    `/api/message?convId=${convId}&orgId=${encodeURIComponent(orgId)}&pageSize=20&page=1`
                );
                const msgs = await msgResp.json();
                const list = Array.isArray(msgs) ? msgs : [];

                const respId = conv && (
                    conv.responsibleuserId || conv.responsibleUserId
                    || (conv.responsibleUser && conv.responsibleUser.id)
                );
                const alreadyTaken = list.some(m => m.newResponsibleId === userId);
                const systemTake = list.some(m =>
                    m.newResponsibleId === userId
                    && (m.oldResponsibleId == null || m.oldResponsibleId !== userId)
                );
                return {
                    responsibleOk: respId === userId,
                    systemTake,
                    alreadyTaken,
                    responsibleId: respId || '',
                };
            }""",
            {"convId": conv_id, "orgId": oid, "userId": uid},
        )
        if check.get("responsibleOk") and (
            check.get("systemTake") or check.get("alreadyTaken")
        ):
            logger.info(
                "browser take OK conv=%s resp=%s taken=%s",
                conv_id[:8],
                check.get("responsibleId", "")[:16],
                check.get("systemTake") or check.get("alreadyTaken"),
            )
            await page.wait_for_timeout(1000)
            return
        if check.get("systemTake"):
            logger.info("browser take event conv=%s (waiting responsible)", conv_id[:8])
        await page.wait_for_timeout(700)

    raise RuntimeError(
        f"Chat not taken — no «взяв чат» for {uid[:16]} (conv={conv_id[:8]})"
    )


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
    """Take chat as Support → verify → one POST (author must be operator)."""
    from playwright.async_api import async_playwright

    slug = (org_slug or "").strip()
    oid = (org_id or "").strip()
    uid = (user_id or "").strip()
    if not slug or not oid:
        raise RuntimeError("org_slug and org_id required for browser send")
    if not uid:
        raise RuntimeError("operator user_id required for browser send")

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

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            user_agent=UA,
            locale="uk-UA",
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
            await page.wait_for_timeout(2500)

            if "sign-in" in page.url:
                raise RuntimeError("Browser session expired (redirected to sign-in)")

            await _verify_logged_in_operator(page, uid)
            await _browser_take_and_verify(
                page, conv_id=conv_id, org_id=oid, user_id=uid
            )

            send_result = await page.evaluate(
                """async ({orgId, convId, text, userId}) => {
                    const resp = await fetch(
                        `/api/message?orgId=${encodeURIComponent(orgId)}`,
                        {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                conversationId: convId,
                                text: text,
                            }),
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
                        authorId: parsed && parsed.authorId,
                    };
                }""",
                {"orgId": oid, "convId": conv_id, "text": text, "userId": uid},
            )

            status = int(send_result.get("status") or 0)
            if status >= 400:
                raise RuntimeError(
                    f"Browser POST failed {status}: {send_result.get('body', '')[:120]}"
                )

            author = str(send_result.get("authorId") or "").strip()
            if author != uid:
                raise RuntimeError(
                    f"Wrong sender authorId={author or 'null'} — "
                    f"expected Support {uid[:16]} (not Facebook page)"
                )

            if not send_result.get("isDelivered") and not send_result.get(
                "facebookMessageId"
            ):
                raise RuntimeError(
                    f"Message not delivered: {send_result.get('body', '')[:120]}"
                )

            logger.info(
                "browser sent conv=%s author=%s fb=%s",
                conv_id[:8],
                author[:16],
                str(send_result.get("facebookMessageId") or "")[:12],
            )
            return {"ok": "true", "method": "browser_take_then_send", "authorId": author}
        finally:
            await browser.close()
