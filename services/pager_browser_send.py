"""Send Pager messages via browser — take chat first, then send like operator UI."""

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

_SAFE_FETCH = """
async function safeJsonFetch(url, init) {
    const resp = await fetch(url, init);
    const raw = await resp.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch (_) {}
    return {
        ok: resp.ok,
        status: resp.status,
        raw: raw.slice(0, 400),
        data,
        html: raw.trimStart().startsWith('<!'),
    };
}
"""


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

    patch_result = await page.evaluate(
        f"""async ({{convId, orgId, userId}}) => {{
            {_SAFE_FETCH}
            const url = `/api/conversation/${{convId}}?userId=${{encodeURIComponent(userId)}}&orgId=${{encodeURIComponent(orgId)}}`;
            const r = await safeJsonFetch(url, {{
                method: 'PATCH',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    responsibleUserId: userId,
                    conversationState: 'read',
                }}),
            }});
            return {{status: r.status, html: r.html, body: r.raw}};
        }}""",
        {"convId": conv_id, "orgId": oid, "userId": uid},
    )
    logger.info(
        "browser PATCH assign conv=%s status=%s html=%s",
        conv_id[:8],
        patch_result.get("status"),
        patch_result.get("html"),
    )
    await page.wait_for_timeout(800)

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

    for attempt in range(12):
        check = await page.evaluate(
            f"""async ({{convId, orgId, userId}}) => {{
                {_SAFE_FETCH}
                const convR = await safeJsonFetch(
                    `/api/conversation/${{convId}}?orgId=${{encodeURIComponent(orgId)}}`
                );
                const msgR = await safeJsonFetch(
                    `/api/message?convId=${{convId}}&orgId=${{encodeURIComponent(orgId)}}&pageSize=20&page=1`
                );
                if (convR.html || msgR.html) {{
                    return {{sessionError: true, html: convR.html || msgR.html}};
                }}
                const conv = convR.data || {{}};
                const list = Array.isArray(msgR.data) ? msgR.data : [];
                const respId = conv.responsibleuserId || conv.responsibleUserId
                    || (conv.responsibleUser && conv.responsibleUser.id);
                const alreadyTaken = list.some(m => m.newResponsibleId === userId);
                const systemTake = list.some(m =>
                    m.newResponsibleId === userId
                    && (m.oldResponsibleId == null || m.oldResponsibleId !== userId)
                );
                return {{
                    sessionError: false,
                    responsibleOk: respId === userId,
                    systemTake,
                    alreadyTaken,
                    responsibleId: respId || '',
                }};
            }}""",
            {"convId": conv_id, "orgId": oid, "userId": uid},
        )
        if check.get("sessionError"):
            logger.warning("browser take poll conv=%s session html (attempt %s)", conv_id[:8], attempt)
            await page.wait_for_timeout(700)
            continue
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
        await page.wait_for_timeout(700)

    raise RuntimeError(
        f"Chat not taken — no «взяв чат» for {uid[:16]} (conv={conv_id[:8]})"
    )


async def _verify_message_delivered(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    text: str,
    attempts: int = 10,
) -> dict:
    """Poll history until Support message is delivered to Messenger."""
    snippet = (text or "")[:80]
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()

    for attempt in range(attempts):
        result = await page.evaluate(
            f"""async ({{convId, orgId, userId, snippet}}) => {{
                {_SAFE_FETCH}
                const r = await safeJsonFetch(
                    `/api/message?convId=${{convId}}&orgId=${{encodeURIComponent(orgId)}}&pageSize=15&page=1`
                );
                if (r.html) return {{sessionError: true}};
                const list = Array.isArray(r.data) ? r.data : [];
                const hit = list.find(m =>
                    m.messageDirection === 'outgoing'
                    && m.authorId === userId
                    && m.text
                    && m.text.startsWith(snippet.slice(0, 40))
                    && (m.isDelivered || m.facebookMessageId)
                );
                if (hit) {{
                    return {{
                        ok: true,
                        authorId: hit.authorId,
                        isDelivered: hit.isDelivered,
                        facebookMessageId: hit.facebookMessageId || '',
                        id: hit.id || '',
                    }};
                }}
                const ghost = list.find(m =>
                    m.messageDirection === 'outgoing'
                    && !m.authorId
                    && m.text
                    && m.text.startsWith(snippet.slice(0, 40))
                );
                return {{
                    ok: false,
                    ghost: !!ghost,
                    count: list.length,
                }};
            }}""",
            {
                "convId": conv_id,
                "orgId": oid,
                "userId": uid,
                "snippet": snippet,
            },
        )
        if result.get("sessionError"):
            await page.wait_for_timeout(800)
            continue
        if result.get("ok"):
            return result
        await page.wait_for_timeout(900)

    return {"ok": False}


async def _browser_post_message(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    text: str,
) -> dict:
    """POST /api/message with userId query (same as operator UI)."""
    return await page.evaluate(
        f"""async ({{orgId, convId, text, userId}}) => {{
            {_SAFE_FETCH}
            const qs = `orgId=${{encodeURIComponent(orgId)}}&userId=${{encodeURIComponent(userId)}}`;
            const attempts = [
                {{ url: `/api/message?${{qs}}`, body: {{ conversationId: convId, text }} }},
                {{ url: `/api/message?${{qs}}`, body: {{ convId, text }} }},
                {{ url: `/api/message?orgId=${{encodeURIComponent(orgId)}}`, body: {{ conversationId: convId, text, authorId: userId }} }},
            ];
            for (const a of attempts) {{
                const r = await safeJsonFetch(a.url, {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(a.body),
                }});
                const parsed = r.data || {{}};
                if (r.status < 400 && !r.html) {{
                    return {{
                        status: r.status,
                        body: r.raw,
                        isDelivered: parsed.isDelivered,
                        facebookMessageId: parsed.facebookMessageId,
                        authorId: parsed.authorId,
                        id: parsed.id,
                    }};
                }}
            }}
            return {{ status: 502, body: 'all POST attempts failed' }};
        }}""",
        {"orgId": org_id, "convId": conv_id, "text": text, "userId": user_id},
    )


async def _browser_send_via_ui(page, text: str) -> bool:
    """Type in composer and press Enter — exactly like operator."""
    selectors = [
        'textarea[placeholder*="повідом"]',
        'textarea[placeholder*="message" i]',
        'textarea',
        '[contenteditable="true"]',
    ]
    for sel in selectors:
        loc = page.locator(sel).last
        if await loc.count():
            try:
                await loc.wait_for(state="visible", timeout=8000)
                await loc.click()
                await loc.fill(text)
                await page.wait_for_timeout(400)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(1200)
                logger.info("browser UI typed message (%s chars)", len(text))
                return True
            except Exception as exc:
                logger.debug("UI send selector %s: %s", sel, exc)
    return False


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
    """Take chat as Support → send → verify Messenger delivery."""
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
            await page.goto(chat_url, wait_until="networkidle", timeout=90000)
            await page.wait_for_timeout(1500)

            if "sign-in" in page.url:
                raise RuntimeError("Browser session expired (redirected to sign-in)")

            await _verify_logged_in_operator(page, uid)
            await _browser_take_and_verify(
                page, conv_id=conv_id, org_id=oid, user_id=uid
            )

            send_result = await _browser_post_message(
                page,
                conv_id=conv_id,
                org_id=oid,
                user_id=uid,
                text=text,
            )
            author = str(send_result.get("authorId") or "").strip()
            delivered = bool(
                send_result.get("isDelivered") or send_result.get("facebookMessageId")
            )

            if author != uid or not delivered:
                logger.info(
                    "browser POST author=%s delivered=%s — trying UI send",
                    author or "null",
                    delivered,
                )
                ui_ok = await _browser_send_via_ui(page, text)
                if not ui_ok:
                    status = int(send_result.get("status") or 0)
                    if status >= 400:
                        raise RuntimeError(
                            f"Browser POST failed {status}: {send_result.get('body', '')[:120]}"
                        )

            verify = await _verify_message_delivered(
                page,
                conv_id=conv_id,
                org_id=oid,
                user_id=uid,
                text=text,
            )
            if not verify.get("ok"):
                raise RuntimeError(
                    f"Message not delivered to Messenger (author={uid[:16]})"
                )

            author = str(verify.get("authorId") or uid).strip()
            logger.info(
                "browser sent conv=%s author=%s fb=%s",
                conv_id[:8],
                author[:16],
                str(verify.get("facebookMessageId") or "")[:12],
            )
            return {"ok": "true", "method": "browser_take_then_send", "authorId": author}
        finally:
            await browser.close()
