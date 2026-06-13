"""Send Pager messages via browser — take chat first, then send like operator UI."""

from __future__ import annotations

import json
import logging
import re
from typing import Sequence

from services.pager_auth import PAGER_BASE, UA, playwright_sign_in_on_page

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


async def _wait_for_chat_composer(page, timeout_ms: int = 45000) -> bool:
    """Wait until messenger input is mounted (SPA hydration)."""
    selectors = [
        'textarea[placeholder*="повідом"]',
        'textarea[placeholder*="message" i]',
        'textarea[placeholder]',
        '[contenteditable="true"][role="textbox"]',
        'div[role="textbox"]',
        '[contenteditable="true"]',
        "textarea",
    ]
    per_sel = max(4000, timeout_ms // max(len(selectors), 1))
    for sel in selectors:
        try:
            loc = page.locator(sel).last
            await loc.wait_for(state="visible", timeout=per_sel)
            return True
        except Exception:
            continue
    return False


async def _chat_thread_active(page, conv_id: str) -> bool:
    if conv_id not in page.url:
        return False
    try:
        body = await page.locator("body").inner_text()
        if "Оберіть чат" in body or "Select a chat" in body:
            return False
    except Exception:
        pass
    return await _wait_for_chat_composer(page, timeout_ms=8000)


async def _open_conversation_in_ui(
    page,
    *,
    locale: str,
    org_slug: str,
    conv_id: str,
) -> bool:
    """Open chat thread — fast path, composer check optional."""
    chat_url = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"

    await page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    if await _chat_thread_active(page, conv_id):
        logger.info("browser chat open conv=%s (url)", conv_id[:8])
        return True

    inbox = f"{PAGER_BASE}/{locale}/{org_slug}/chats"
    await page.goto(inbox, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1500)
    clicked = await page.evaluate(
        f"""() => {{
            const el = document.querySelector('a[href*="{conv_id}"]');
            if (el) {{ el.click(); return true; }}
            return false;
        }}"""
    )
    if clicked:
        await page.wait_for_timeout(2500)
        if await _chat_thread_active(page, conv_id):
            logger.info("browser chat open conv=%s (click)", conv_id[:8])
            return True

    await page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2000)
    ok = await _chat_thread_active(page, conv_id)
    if ok:
        logger.info("browser chat open conv=%s (retry)", conv_id[:8])
    else:
        logger.warning(
            "browser chat maybe closed conv=%s — will try send anyway",
            conv_id[:8],
        )
    return ok


async def _warm_chat_page(
    page,
    *,
    locale: str,
    org_slug: str,
    conv_id: str,
) -> None:
    """Open org inbox then target chat so Next.js messenger context is ready."""
    ok = await _open_conversation_in_ui(
        page, locale=locale, org_slug=org_slug, conv_id=conv_id
    )
    if not ok:
        logger.warning("chat composer missing conv=%s after open", conv_id[:8])


async def _browser_send_via_ui(page, text: str) -> bool:
    if not await _wait_for_chat_composer(page, timeout_ms=20000):
        return False

    for get_loc in (
        lambda: page.get_by_role("textbox").last,
        lambda: page.locator('textarea[placeholder*="повідом"]').last,
        lambda: page.locator('textarea[placeholder*="message" i]').last,
        lambda: page.locator("textarea").last,
        lambda: page.locator('[contenteditable="true"]').last,
    ):
        try:
            loc = get_loc()
            if not await loc.count():
                continue
            await loc.wait_for(state="visible", timeout=8000)
            await loc.click()
            await loc.fill("")
            await page.keyboard.insert_text(text)
            await page.wait_for_timeout(400)
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2000)
            send_btn = page.get_by_role(
                "button", name=re.compile(r"send|надісл|відправ|submit", re.I)
            )
            if await send_btn.count():
                try:
                    await send_btn.last.click(timeout=3000)
                    await page.wait_for_timeout(1500)
                except Exception:
                    pass
            logger.info("browser UI typed (%s chars)", len(text))
            return True
        except Exception as exc:
            logger.debug("UI send attempt: %s", exc)
    return False


async def _browser_prepare_outbound(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
) -> str:
    """Assign operator + load conv in browser session (required before POST /api/message)."""
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    patch_url = _api(f"/api/conversation/{conv_id}?userId={uid}&orgId={oid}")
    conv_url = _api(f"/api/conversation/{conv_id}?orgId={oid}")
    msg_url = _api(
        f"/api/message?convId={conv_id}&orgId={oid}&pageSize=5&page=1"
    )
    result = await page.evaluate(
        f"""async ({{patchUrl, convUrl, msgUrl, userId}}) => {{
            {_SAFE_FETCH}
            const patch = await safeJsonFetch(patchUrl, {{
                method: 'PATCH',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{
                    responsibleUserId: userId,
                    conversationState: 'read',
                }}),
            }});
            const convR = await safeJsonFetch(convUrl);
            await safeJsonFetch(msgUrl);
            const conv = convR.data || {{}};
            const respId = conv.responsibleuserId || conv.responsibleUserId
                || (conv.responsibleUser && conv.responsibleUser.id) || '';
            const ch = conv.channelId
                || (conv.channel && conv.channel.id) || '';
            return {{
                patchStatus: patch.status,
                sessionError: patch.html || convR.html,
                responsibleOk: respId === userId,
                responsibleId: respId || '',
                channelId: ch || '',
            }};
        }}""",
        {
            "patchUrl": patch_url,
            "convUrl": conv_url,
            "msgUrl": msg_url,
            "userId": uid,
        },
    )
    if result.get("sessionError"):
        raise RuntimeError(
            f"Browser session stale preparing conv={conv_id[:8]}"
        )
    if not result.get("responsibleOk"):
        rid = str(result.get("responsibleId") or "")
        raise RuntimeError(
            f"Take chat not verified conv={conv_id[:8]} "
            f"want={uid[:16]} got={rid[:16] or 'none'}"
        )
    ch = str(result.get("channelId") or "").strip()
    logger.info(
        "browser prepared conv=%s patch=%s resp=%s channel=%s",
        conv_id[:8],
        result.get("patchStatus"),
        str(result.get("responsibleId") or "")[:16],
        ch[:8] if ch else "?",
    )
    return ch


async def _post_message_payload(
    page,
    *,
    post_url: str,
    payload: dict,
) -> dict:
    return await page.evaluate(
        f"""async ({{url, payload}}) => {{
            {_SAFE_FETCH}
            const r = await safeJsonFetch(url, {{
                method: 'POST',
                headers: {{
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                }},
                body: JSON.stringify(payload),
            }});
            const p = r.data || {{}};
            const err = typeof p.error === 'string'
                ? p.error
                : (typeof p.message === 'string' ? p.message : (r.raw || '').slice(0, 400));
            return {{
                status: r.status,
                html: r.html,
                error: err,
                raw: (r.raw || '').slice(0, 400),
                authorId: p.authorId || '',
                isDelivered: !!p.isDelivered,
                facebookMessageId: p.facebookMessageId || '',
                bodyKeys: Object.keys(payload),
            }};
        }}""",
        {"url": post_url, "payload": payload},
    )


async def _send_one_in_session(
    context,
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    text: str,
    referer: str,
) -> None:
    """POST operator payload — {conversationId, text} ONLY (no channelId)."""
    qs = f"orgId={org_id}&userId={user_id}"
    post_url = _api(f"/api/message?{qs}")
    payload = {"conversationId": conv_id, "text": text}
    last_err = ""
    last_status = 0

    try:
        resp = await context.request.post(
            post_url,
            data=json.dumps(payload),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Referer": referer,
            },
        )
        last_status = resp.status
        raw = (await resp.text())[:400]
        if raw.strip().startswith("<!"):
            last_err = "HTML response — chat page not active"
        else:
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {}
            author = str((data or {}).get("authorId") or "").strip()
            fb = str((data or {}).get("facebookMessageId") or "").strip()
            if author == user_id and (
                (data or {}).get("isDelivered") or fb
            ):
                logger.info(
                    "browser API sent conv=%s author=%s fb=%s",
                    conv_id[:8],
                    author[:16],
                    fb[:12],
                )
                return
            last_err = str((data or {}).get("error") or raw)[:400]
    except Exception as exc:
        last_err = str(exc)[:200]
        logger.debug("context.request POST conv=%s: %s", conv_id[:8], exc)

    result = await _post_message_payload(page, post_url=post_url, payload=payload)
    status = int(result.get("status") or 0)
    author = str(result.get("authorId") or "").strip()
    fb = str(result.get("facebookMessageId") or "").strip()
    last_err = str(result.get("error") or result.get("raw") or last_err)[:400]
    last_status = status or last_status

    if result.get("html") or str(last_err).lstrip().startswith("<!DOCTYPE"):
        logger.warning(
            "browser POST conv=%s got HTML — chat not active",
            conv_id[:8],
        )
    elif status >= 400:
        logger.warning(
            "browser POST conv=%s status=%s err=%s",
            conv_id[:8],
            status,
            last_err[:160],
        )
    elif author == user_id and (result.get("isDelivered") or fb):
        logger.info(
            "browser POST sent conv=%s author=%s fb=%s",
            conv_id[:8],
            author[:16],
            fb[:12],
        )
        return
    elif author and author != user_id:
        logger.warning(
            "browser POST wrong author conv=%s author=%s",
            conv_id[:8],
            author[:16],
        )
    else:
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
                "browser POST sent conv=%s author=%s fb=%s (verified)",
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
        f"Send POST failed conv={conv_id[:8]} status={last_status}: "
        f"{last_err or 'not delivered as operator'}"
    )


async def _send_operator_text(
    context,
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    text: str,
    locale: str,
    org_slug: str,
) -> None:
    referer = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"
    if await _browser_send_via_ui(page, text):
        verify = await _verify_message_delivered(
            page,
            conv_id=conv_id,
            org_id=org_id,
            user_id=user_id,
            text=text,
            attempts=15,
        )
        if verify.get("ok"):
            logger.info(
                "browser UI sent conv=%s author=%s fb=%s",
                conv_id[:8],
                str(verify.get("authorId") or user_id)[:16],
                str(verify.get("facebookMessageId") or "")[:12],
            )
            return
        logger.warning("UI unverified conv=%s — trying API POST", conv_id[:8])
    else:
        logger.warning("UI composer missing conv=%s — trying API POST", conv_id[:8])

    await _send_one_in_session(
        context,
        page,
        conv_id=conv_id,
        org_id=org_id,
        user_id=user_id,
        text=text,
        referer=referer,
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
            if not await _open_conversation_in_ui(
                page, locale=locale, org_slug=slug, conv_id=conv_id
            ):
                raise RuntimeError(f"Chat thread not open conv={conv_id[:8]}")
            await _browser_prepare_outbound(
                page,
                conv_id=conv_id,
                org_id=oid,
                user_id=uid,
            )
            await page.wait_for_timeout(800)

            for i, body in enumerate(texts):
                if i:
                    await page.wait_for_timeout(1200)
                await _send_operator_text(
                    context,
                    page,
                    conv_id=conv_id,
                    org_id=oid,
                    user_id=uid,
                    text=body,
                    locale=locale,
                    org_slug=slug,
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


async def send_messages_via_browser_ui(
    cookies: dict[str, str] | None = None,
    *,
    conv_id: str,
    texts: Sequence[str],
    org_id: str,
    org_slug: str,
    user_id: str = "",
    locale: str = "uk",
    skip_take: bool = False,
    email: str = "",
    password: str = "",
) -> None:
    """Send via Playwright — full UI login when credentials available."""
    from playwright.async_api import async_playwright

    slug = (org_slug or "").strip()
    oid = (org_id or "").strip()
    uid = (user_id or "").strip()
    if not slug or not oid or not uid:
        raise RuntimeError("org_slug, org_id, user_id required")

    use_login = bool((email or "").strip() and (password or "").strip())
    clean = {
        k: v
        for k, v in (cookies or {}).items()
        if not k.startswith("_pager_") and v
    }
    if not use_login and not clean:
        raise RuntimeError("No session cookies or credentials for browser send")

    bodies = [t.strip() for t in texts if (t or "").strip()]
    if not bodies:
        raise RuntimeError("No messages to send")

    launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            user_agent=UA,
            locale="uk-UA",
            viewport={"width": 1280, "height": 720},
        )
        context.set_default_timeout(25000)
        page = await context.new_page()
        try:
            if use_login:
                logger.info("browser login for send conv=%s", conv_id[:8])
                await playwright_sign_in_on_page(page, email.strip(), password)
                chat_url = f"{PAGER_BASE}/{locale}/{slug}/chats/{conv_id}"
                await page.goto(
                    chat_url, wait_until="domcontentloaded", timeout=60000
                )
                await page.wait_for_timeout(1500)
            else:
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

            if "sign-in" in page.url:
                raise RuntimeError("Browser session expired (redirected to sign-in)")

            await _verify_logged_in_operator(page, uid)
            if not await _open_conversation_in_ui(
                page, locale=locale, org_slug=slug, conv_id=conv_id
            ):
                logger.warning(
                    "composer not ready conv=%s — POST fallback",
                    conv_id[:8],
                )

            if skip_take:
                await _browser_prepare_outbound(
                    page, conv_id=conv_id, org_id=oid, user_id=uid
                )
            else:
                chat_url = f"{PAGER_BASE}/{locale}/{slug}/chats/{conv_id}"
                await _browser_take_and_verify(
                    page,
                    conv_id=conv_id,
                    org_id=oid,
                    user_id=uid,
                    chat_url=chat_url,
                )
                await _browser_prepare_outbound(
                    page, conv_id=conv_id, org_id=oid, user_id=uid
                )

            await page.wait_for_timeout(1000)
            for i, body in enumerate(bodies):
                if i:
                    await page.wait_for_timeout(1200)
                await _send_operator_text(
                    context,
                    page,
                    conv_id=conv_id,
                    org_id=oid,
                    user_id=uid,
                    text=body,
                    locale=locale,
                    org_slug=slug,
                )
        finally:
            await browser.close()


async def send_batch_via_browser(
    jobs: Sequence[tuple[str, list[str]]],
    *,
    org_id: str,
    org_slug: str,
    user_id: str,
    locale: str = "uk",
    email: str = "",
    password: str = "",
) -> set[str]:
    """One Playwright login, send to multiple conversations. Returns successful conv ids."""
    if not jobs:
        return set()

    slug = (org_slug or "").strip()
    oid = (org_id or "").strip()
    uid = (user_id or "").strip()
    if not slug or not oid or not uid:
        raise RuntimeError("org_slug, org_id, user_id required")
    if not (email or "").strip() or not (password or "").strip():
        raise RuntimeError("email/password required for batch browser send")

    launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    from playwright.async_api import async_playwright

    ok: set[str] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            user_agent=UA,
            locale="uk-UA",
            viewport={"width": 1280, "height": 720},
        )
        context.set_default_timeout(25000)
        page = await context.new_page()
        try:
            logger.info("browser batch login jobs=%s", len(jobs))
            await playwright_sign_in_on_page(page, email.strip(), password)
            org_inbox = f"{PAGER_BASE}/{locale}/{slug}/chats"
            await page.goto(org_inbox, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(1500)
            await _verify_logged_in_operator(page, uid)

            for conv_id, texts in jobs:
                bodies = [t.strip() for t in texts if (t or "").strip()]
                if not bodies:
                    continue
                try:
                    logger.info(
                        "browser batch conv=%s texts=%s",
                        conv_id[:8],
                        len(bodies),
                    )
                    if not await _open_conversation_in_ui(
                        page, locale=locale, org_slug=slug, conv_id=conv_id
                    ):
                        logger.warning(
                            "batch conv=%s composer not ready — trying send",
                            conv_id[:8],
                        )
                    chat_url = f"{PAGER_BASE}/{locale}/{slug}/chats/{conv_id}"
                    await _browser_take_and_verify(
                        page,
                        conv_id=conv_id,
                        org_id=oid,
                        user_id=uid,
                        chat_url=chat_url,
                    )
                    await _browser_prepare_outbound(
                        page, conv_id=conv_id, org_id=oid, user_id=uid
                    )
                    await page.wait_for_timeout(800)
                    for i, body in enumerate(bodies):
                        if i:
                            await page.wait_for_timeout(1200)
                        await _send_operator_text(
                            context,
                            page,
                            conv_id=conv_id,
                            org_id=oid,
                            user_id=uid,
                            text=body,
                            locale=locale,
                            org_slug=slug,
                        )
                    ok.add(conv_id)
                except Exception as exc:
                    logger.warning(
                        "browser batch conv=%s failed: %s",
                        conv_id[:8],
                        exc,
                    )
        finally:
            await browser.close()
    return ok


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
