"""Send Pager messages via browser — take chat first, then send like operator UI."""

from __future__ import annotations

import logging
import re
from typing import Sequence

from services.pager_auth import PAGER_BASE, UA

logger = logging.getLogger(__name__)

_API = PAGER_BASE.rstrip("/")

_TAKE_UI = (
    r"take chat|take the chat|take dialog|взяти чат|взяти діалог|взяв.*чат",
    r"assign.*me|призначити мене|забрати чат",
    r"^take$|^взяти$",
)

_SAFE_FETCH = """
async function safeJsonFetch(url, init) {
    const resp = await fetch(url, { credentials: 'include', ...init });
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


def _api(path: str) -> str:
    return f"{_API}{path}"


async def _verify_logged_in_operator(page, expected_uid: str) -> None:
    uid = (expected_uid or "").strip()
    clerk_uid = await page.evaluate(
        """async () => {
            try {
                const r = await fetch(
                    'https://clerk.pager.co.ua/v1/client?_clerk_js_version=5.68.0&__clerk_api_version=2024-10-01',
                    { credentials: 'include' }
                );
                const d = await r.json();
                const client = d.response || d.client || d;
                const sessions = client.sessions || [];
                if (!sessions.length) return '';
                return (sessions[0].user || {}).id || '';
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


async def _click_take_ui(page, conv_id: str) -> bool:
    for pattern in _TAKE_UI:
        btn = page.get_by_role("button", name=re.compile(pattern, re.I))
        if await btn.count():
            await btn.first.click(force=True)
            await page.wait_for_timeout(1500)
            logger.info("browser UI take conv=%s", conv_id[:8])
            return True
    take_text = page.get_by_text(re.compile(r"взяти чат|take chat", re.I))
    if await take_text.count():
        await take_text.first.click(force=True)
        await page.wait_for_timeout(1500)
        logger.info("browser text take conv=%s", conv_id[:8])
        return True
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
                return True
    return False


async def _poll_take_state(page, *, conv_id: str, org_id: str, user_id: str) -> dict:
    api_conv = _api(f"/api/conversation/{conv_id}?orgId={org_id}")
    api_msg = _api(
        f"/api/message?convId={conv_id}&orgId={org_id}&pageSize=20&page=1"
    )
    return await page.evaluate(
        f"""async ({{convUrl, msgUrl, userId}}) => {{
            {_SAFE_FETCH}
            const convR = await safeJsonFetch(convUrl);
            const msgR = await safeJsonFetch(msgUrl);
            if (convR.html || msgR.html) {{
                return {{sessionError: true}};
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
        {"convUrl": api_conv, "msgUrl": api_msg, "userId": user_id},
    )


async def _browser_take_and_verify(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    chat_url: str,
) -> None:
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    if not uid:
        raise RuntimeError("operator user_id required to take chat")

    patch_url = _api(
        f"/api/conversation/{conv_id}?userId={uid}&orgId={oid}"
    )
    patch_result = await page.evaluate(
        f"""async ({{url, userId}}) => {{
            {_SAFE_FETCH}
            const r = await safeJsonFetch(url, {{
                method: 'PATCH',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    responsibleUserId: userId,
                    conversationState: 'read',
                }}),
            }});
            return {{status: r.status, html: r.html, ok: r.ok}};
        }}""",
        {"url": patch_url, "userId": uid},
    )
    patch_ok = int(patch_result.get("status") or 0) == 200
    logger.info(
        "browser PATCH assign conv=%s status=%s",
        conv_id[:8],
        patch_result.get("status"),
    )
    await page.wait_for_timeout(600)
    await _click_take_ui(page, conv_id)

    for attempt in range(6):
        check = await _poll_take_state(
            page, conv_id=conv_id, org_id=oid, user_id=uid
        )
        if check.get("sessionError"):
            logger.warning(
                "browser take poll conv=%s html — reload (attempt %s)",
                conv_id[:8],
                attempt,
            )
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)
            await _click_take_ui(page, conv_id)
            continue
        if check.get("responsibleOk") or check.get("systemTake") or check.get(
            "alreadyTaken"
        ):
            logger.info(
                "browser take OK conv=%s resp=%s",
                conv_id[:8],
                check.get("responsibleId", uid)[:16],
            )
            await page.wait_for_timeout(800)
            return
        await page.wait_for_timeout(600)

    if patch_ok:
        logger.warning(
            "browser take unverified conv=%s — PATCH 200, proceeding to send",
            conv_id[:8],
        )
        return

    logger.warning(
        "browser take unverified conv=%s — proceeding after PATCH attempt",
        conv_id[:8],
    )


async def _verify_message_delivered(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    text: str,
    attempts: int = 8,
) -> dict:
    snippet = (text or "")[:80]
    api_msg = _api(
        f"/api/message?convId={conv_id}&orgId={org_id}&pageSize=15&page=1"
    )

    for _ in range(attempts):
        result = await page.evaluate(
            f"""async ({{msgUrl, userId, snippet}}) => {{
                {_SAFE_FETCH}
                const r = await safeJsonFetch(msgUrl);
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
                        facebookMessageId: hit.facebookMessageId || '',
                    }};
                }}
                return {{ ok: false }};
            }}""",
            {"msgUrl": api_msg, "userId": user_id, "snippet": snippet},
        )
        if result.get("sessionError"):
            await page.wait_for_timeout(700)
            continue
        if result.get("ok"):
            return result
        await page.wait_for_timeout(800)

    return {"ok": False}


async def _browser_send_via_ui(page, text: str) -> bool:
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
                await loc.wait_for(state="visible", timeout=10000)
                await loc.click()
                await loc.fill(text)
                await page.wait_for_timeout(300)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(1500)
                logger.info("browser UI sent (%s chars)", len(text))
                return True
            except Exception as exc:
                logger.debug("UI send %s: %s", sel, exc)
    return False


async def _send_one_in_session(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    text: str,
) -> None:
    """POST from browser session only — same as operator UI, never textarea (page identity)."""
    qs = f"orgId={org_id}&userId={user_id}"
    post_url = _api(f"/api/message?{qs}")
    result = await page.evaluate(
        f"""async ({{url, convId, text}}) => {{
            {_SAFE_FETCH}
            const r = await safeJsonFetch(url, {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{ conversationId: convId, text }}),
            }});
            const p = r.data || {{}};
            return {{
                status: r.status,
                html: r.html,
                authorId: p.authorId || '',
                isDelivered: !!p.isDelivered,
                facebookMessageId: p.facebookMessageId || '',
            }};
        }}""",
        {"url": post_url, "convId": conv_id, "text": text},
    )
    status = int(result.get("status") or 0)
    author = str(result.get("authorId") or "").strip()
    fb = str(result.get("facebookMessageId") or "").strip()

    if status >= 400:
        raise RuntimeError(f"Send POST failed status={status}")

    if author == user_id and (result.get("isDelivered") or fb):
        logger.info(
            "browser sent conv=%s author=%s fb=%s",
            conv_id[:8],
            author[:16],
            fb[:12],
        )
        return

    verify = await _verify_message_delivered(
        page,
        conv_id=conv_id,
        org_id=org_id,
        user_id=user_id,
        text=text,
        attempts=12,
    )
    if verify.get("ok"):
        logger.info(
            "browser sent conv=%s author=%s fb=%s (verified)",
            conv_id[:8],
            str(verify.get("authorId") or user_id)[:16],
            str(verify.get("facebookMessageId") or "")[:12],
        )
        return

    page_msg = await _find_outgoing_by_text(
        page, conv_id=conv_id, org_id=org_id, text=text
    )
    if page_msg and not page_msg.get("authorId"):
        raise RuntimeError(
            f"Message stuck as Facebook page (conv={conv_id[:8]}) — not retrying"
        )

    raise RuntimeError(
        f"Message not delivered as operator (conv={conv_id[:8]})"
    )


async def _find_outgoing_by_text(
    page,
    *,
    conv_id: str,
    org_id: str,
    text: str,
) -> dict | None:
    snippet = (text or "")[:60]
    api_msg = _api(
        f"/api/message?convId={conv_id}&orgId={org_id}&pageSize=15&page=1"
    )
    hit = await page.evaluate(
        f"""async ({{msgUrl, snippet}}) => {{
            {_SAFE_FETCH}
            const r = await safeJsonFetch(msgUrl);
            if (r.html) return null;
            const list = Array.isArray(r.data) ? r.data : [];
            const m = list.find(x =>
                x.messageDirection === 'outgoing'
                && x.text
                && x.text.startsWith(snippet.slice(0, 40))
            );
            if (!m) return null;
            return {{
                authorId: m.authorId || '',
                isDelivered: !!m.isDelivered,
                facebookMessageId: m.facebookMessageId || '',
            }};
        }}""",
        {"msgUrl": api_msg, "snippet": snippet},
    )
    return hit if isinstance(hit, dict) else None


async def _run_browser_session(
    cookies: dict[str, str],
    *,
    conv_id: str,
    texts: Sequence[str],
    org_id: str,
    org_slug: str,
    user_id: str,
    locale: str,
    skip_take: bool = False,
) -> None:
    from playwright.async_api import async_playwright

    slug = (org_slug or "").strip()
    oid = (org_id or "").strip()
    uid = (user_id or "").strip()
    if not slug or not oid or not uid:
        raise RuntimeError("org_slug, org_id, user_id required")

    clean = {k: v for k, v in cookies.items() if not k.startswith("_pager_") and v}
    if not clean:
        raise RuntimeError("No session cookies for browser send")

    chat_url = f"{PAGER_BASE}/{locale}/{slug}/chats/{conv_id}"
    launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]

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
            await page.wait_for_timeout(2000)

            if "sign-in" in page.url:
                raise RuntimeError("Browser session expired (redirected to sign-in)")

            await _verify_logged_in_operator(page, uid)
            if skip_take:
                logger.info("browser skip take conv=%s (REST already took)", conv_id[:8])
            else:
                await _browser_take_and_verify(
                    page,
                    conv_id=conv_id,
                    org_id=oid,
                    user_id=uid,
                    chat_url=chat_url,
                )
            await page.wait_for_timeout(1500)

            for i, body in enumerate(texts):
                if i:
                    await page.wait_for_timeout(1200)
                await _send_one_in_session(
                    page,
                    conv_id=conv_id,
                    org_id=oid,
                    user_id=uid,
                    text=body,
                )
        finally:
            await browser.close()


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
    await _run_browser_session(
        cookies,
        conv_id=conv_id,
        texts=[text],
        org_id=org_id,
        org_slug=org_slug,
        user_id=user_id,
        locale=locale,
    )
    return {"ok": "true", "method": "browser_take_then_send", "authorId": user_id}


async def send_messages_via_browser(
    cookies: dict[str, str],
    *,
    conv_id: str,
    texts: Sequence[str],
    org_id: str,
    org_slug: str,
    user_id: str = "",
    locale: str = "uk",
    skip_take: bool = False,
) -> dict[str, str]:
    """Take chat once, send multiple messages in one browser session."""
    bodies = [t.strip() for t in texts if (t or "").strip()]
    if not bodies:
        raise RuntimeError("No messages to send")
    await _run_browser_session(
        cookies,
        conv_id=conv_id,
        texts=bodies,
        org_id=org_id,
        org_slug=org_slug,
        user_id=user_id,
        locale=locale,
        skip_take=skip_take,
    )
    return {"ok": "true", "count": str(len(bodies))}
