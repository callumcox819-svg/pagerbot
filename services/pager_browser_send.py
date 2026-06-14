"""Send Pager messages via browser — take chat first, then send like operator UI."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Sequence

from services.pager_auth import PAGER_BASE, UA, playwright_sign_in_on_page

from services.script_engine import (
    SAVED_REPLY_FOLDER_NAMES,
    load_script,
    script_ui_snippet,
    script_verify_snippet,
)

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


async def _verify_outgoing_dom(page, text: str) -> dict:
    """Pager UI: successful operator bubble is right-aligned (items-end / flex-row-reverse)."""
    snippet = (text or "")[:80]
    result = await page.evaluate(
        """(snippet) => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const want = norm(snippet).slice(0, 40);
            if (!want) return { ok: false };

            const rows = document.querySelectorAll(
                '[class*="flex-row-reverse"][class*="items-end"],'
                + '[class*="flex-col"][class*="items-end"]'
            );
            for (const row of rows) {
                const txt = norm(row.innerText || row.textContent || '');
                if (!want || !txt.includes(want.slice(0, 20))) continue;

                const err = row.querySelector(
                    '[class*="text-red"], [class*="text-error"],'
                    + '[class*="fill-red"], svg[class*="red"],'
                    + '[aria-label*="error" i], [title*="не достав" i],'
                    + '[title*="failed" i]'
                );
                if (err) {
                    return { ok: false, ghost: true, reason: 'dom_error_icon' };
                }
                const hasAvatar = !!row.querySelector(
                    'img, [class*="rounded-full"]'
                );
                return { ok: true, hasAvatar, preview: txt.slice(0, 48) };
            }
            return { ok: false };
        }""",
        snippet,
    )
    return result if isinstance(result, dict) else {"ok": False}


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
        dom = await _verify_outgoing_dom(page, text)
        if dom.get("ghost"):
            return {"ok": False, "ghost": True}
        if dom.get("ok"):
            await page.wait_for_timeout(1200)
            retry = await page.evaluate(
                f"""async ({{msgUrl, userId, snippet}}) => {{
                    {_SAFE_FETCH}
                    const r = await safeJsonFetch(msgUrl);
                    if (r.html) return {{ ok: false }};
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
                            via: 'dom_then_api',
                        }};
                    }}
                    return {{ ok: false }};
                }}""",
                {"msgUrl": api_msg, "userId": user_id, "snippet": snippet},
            )
            if isinstance(retry, dict) and retry.get("ok"):
                return retry
        await page.wait_for_timeout(800)

    return {"ok": False}


async def _wait_for_chat_composer(page, timeout_ms: int = 45000) -> bool:
    """Wait until messenger input is mounted (SPA hydration)."""
    selectors = [
        'textarea[placeholder*="Напишіть"]',
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


async def _ensure_chat_api_ready(
    page,
    *,
    conv_id: str,
    org_id: str,
    timeout_ms: int = 25000,
) -> bool:
    """Chat is ready for POST when message + conversation APIs respond in browser."""
    cid = (conv_id or "").strip()
    oid = (org_id or "").strip()
    if not cid or not oid:
        return False
    msg_url = _api(f"/api/message?convId={cid}&orgId={oid}&pageSize=5&page=1")
    conv_url = _api(f"/api/conversation/{cid}?orgId={oid}")
    attempts = max(1, timeout_ms // 1000)
    for attempt in range(attempts):
        ok = await page.evaluate(
            f"""async ({{msgUrl, convUrl}}) => {{
                {_SAFE_FETCH}
                const convR = await safeJsonFetch(convUrl);
                const msgR = await safeJsonFetch(msgUrl);
                if (convR.html || msgR.html) return false;
                const convOk = convR.ok && convR.data && (convR.data.id || convR.data.channelId);
                const msgs = msgR.data;
                const msgOk = msgR.ok && (Array.isArray(msgs) || (msgs && Array.isArray(msgs.items)));
                return convOk && msgOk;
            }}""",
            {"msgUrl": msg_url, "convUrl": conv_url},
        )
        if ok:
            logger.info(
                "browser chat API ready conv=%s try=%s",
                cid[:8],
                attempt,
            )
            return True
        await page.wait_for_timeout(1000)
    logger.warning("browser chat API timeout conv=%s", cid[:8])
    return False


async def _wait_for_inbox_hydrated(
    page,
    *,
    org_id: str,
    channel_id: str = "",
    timeout_ms: int = 90000,
) -> bool:
    """Wait until Pager inbox finishes loading (skeletons → real chat rows)."""
    oid = (org_id or "").strip()
    ch = (channel_id or "").strip()
    api_conv = _api(f"/api/conversation?orgId={oid}&pageSize=20&page=1")
    if ch:
        api_conv += f"&channelId={ch}"

    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000.0
    while asyncio.get_event_loop().time() < deadline:
        state = await page.evaluate(
            """() => {
                const pulses = document.querySelectorAll('.animate-pulse');
                const links = [...document.querySelectorAll('a[href*="/chats/"]')]
                    .filter(a => /[0-9a-f]{8}-[0-9a-f]{4}-/.test(a.getAttribute('href') || ''));
                const text = document.body.innerText || '';
                const choose = text.includes('Оберіть чат') || text.includes('Select a chat');
                const search = !!document.querySelector('input[placeholder="Пошук"], input[placeholder*="Пошук"]');
                return {
                    pulses: pulses.length,
                    links: links.length,
                    chooseChat: choose,
                    hasSearch: search,
                };
            }"""
        )
        if isinstance(state, dict):
            links = int(state.get("links") or 0)
            pulses = int(state.get("pulses") or 0)
            if links >= 1 and pulses < 25:
                logger.info(
                    "browser inbox hydrated links=%s pulses=%s",
                    links,
                    pulses,
                )
                return True
            if state.get("hasSearch") and not state.get("chooseChat") and pulses < 15:
                logger.info("browser inbox hydrated (search ready)")
                return True

        if oid:
            ok = await page.evaluate(
                f"""async ({{url}}) => {{
                    {_SAFE_FETCH}
                    const r = await safeJsonFetch(url);
                    if (r.html) return false;
                    const data = r.data;
                    if (Array.isArray(data)) return data.length > 0;
                    if (data && Array.isArray(data.items)) return data.items.length > 0;
                    return false;
                }}""",
                {"url": api_conv},
            )
            if ok:
                logger.info("browser inbox hydrated (conversation API)")
                await page.wait_for_timeout(2000)
                return True

        await page.wait_for_timeout(2000)

    logger.warning("browser inbox hydration timeout")
    return False


async def _wait_for_chat_thread(
    page,
    conv_id: str,
    *,
    timeout_ms: int = 60000,
) -> bool:
    """Wait until a specific chat thread is open (not «Оберіть чат»)."""
    cid = (conv_id or "").strip()
    if not cid:
        return False
    attempts = max(1, timeout_ms // 1500)
    for attempt in range(attempts):
        if await _chat_thread_active(page, cid):
            logger.info("browser thread active conv=%s try=%s", cid[:8], attempt)
            return True
        await page.wait_for_timeout(1500)
    return False


async def _open_conversation_direct(
    page,
    *,
    locale: str,
    org_slug: str,
    conv_id: str,
    channel_id: str = "",
    org_id: str = "",
) -> None:
    """Open chat URL exactly like Pager SPA (with channelId query)."""
    ch = (channel_id or "").strip()
    chat_url = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"
    if ch:
        chat_url += f"?channelId={ch}"
    api_needle = f"convId={conv_id}"
    try:
        async with page.expect_response(
            lambda r: (
                api_needle in r.url
                and "/api/message" in r.url
                and r.status == 200
            ),
            timeout=25000,
        ):
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
        logger.info("browser chat direct conv=%s (api)", conv_id[:8])
        return
    except Exception:
        pass
    await page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
    await _fast_goto_chat(
        page,
        locale=locale,
        org_slug=org_slug,
        conv_id=conv_id,
        org_id=org_id,
        channel_id=ch,
    )


async def _click_no_status_tab(page) -> bool:
    """Filter inbox to «Без статусу» — matches worker backlog queue."""
    for pattern in (
        r"без\s*статусу",
        r"no\s*status",
        r"without\s*status",
    ):
        tab = page.get_by_role("tab", name=re.compile(pattern, re.I))
        if await tab.count():
            await tab.first.click(force=True)
            await page.wait_for_timeout(1200)
            logger.info("browser tab «Без статусу»")
            return True
        link = page.get_by_role("link", name=re.compile(pattern, re.I))
        if await link.count():
            await link.first.click(force=True)
            await page.wait_for_timeout(1200)
            logger.info("browser link «Без статусу»")
            return True
        btn = page.locator("button, a, [role='tab']").filter(
            has_text=re.compile(pattern, re.I)
        )
        if await btn.count():
            await btn.first.click(force=True)
            await page.wait_for_timeout(1200)
            logger.info("browser filter «Без статусу»")
            return True
    return False


async def _click_conv_in_sidebar(page, conv_id: str) -> bool:
    """Click chat row in sidebar — Next.js Link or clickable row."""
    cid = conv_id.strip()
    if not cid:
        return False
    clicked = await page.evaluate(
        f"""() => {{
            const id = "{cid}";
            const candidates = [
                ...document.querySelectorAll(
                    'a[href*="' + id + '"], [href*="' + id + '"]'
                ),
            ];
            for (const el of candidates) {{
                try {{
                    el.scrollIntoView({{block: 'center', behavior: 'instant'}});
                }} catch (_) {{}}
                el.click();
                return 'link';
            }}
            const all = document.querySelectorAll(
                'a, [role="button"], [class*="cursor-pointer"], li, div'
            );
            for (const el of all) {{
                const href = el.getAttribute('href') || '';
                const html = el.outerHTML || '';
                if (!href.includes(id) && !html.includes(id)) continue;
                try {{
                    el.scrollIntoView({{block: 'center', behavior: 'instant'}});
                }} catch (_) {{}}
                el.click();
                return 'row';
            }}
            return '';
        }}"""
    )
    if clicked:
        await page.wait_for_timeout(2500)
        logger.info("browser sidebar click conv=%s (%s)", cid[:8], clicked)
        return True
    return False


async def _search_and_open_chat(
    page, *, client_name: str, conv_id: str
) -> bool:
    """Use Pager search box when chat row is off-screen."""
    name = (client_name or "").strip()
    if not name or len(name) < 2:
        return False
    search = page.get_by_placeholder(re.compile(r"пошук|search", re.I))
    if not await search.count():
        search = page.locator('input[type="search"], input[type="text"]').first
        if not await search.count():
            return False
    try:
        await search.first.click()
        await search.first.fill("")
        await search.first.fill(name[:40])
        await page.wait_for_timeout(1800)
        if await _click_conv_in_sidebar(page, conv_id):
            return True
        await search.first.fill("")
        await page.wait_for_timeout(400)
    except Exception as exc:
        logger.debug("search open conv=%s: %s", conv_id[:8], exc)
    return False


async def _ensure_inbox(
    page,
    *,
    locale: str,
    org_slug: str,
    channel_id: str = "",
    org_id: str = "",
) -> str:
    ch = (channel_id or "").strip()
    qs = f"?channelId={ch}" if ch else ""
    inbox = f"{PAGER_BASE}/{locale}/{org_slug}/chats{qs}"
    on_inbox = f"/{org_slug}/chats" in page.url
    if not on_inbox or (ch and f"channelId={ch}" not in page.url):
        await page.goto(inbox, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(1500)
    if org_id:
        await _wait_for_inbox_hydrated(
            page, org_id=org_id, channel_id=ch, timeout_ms=90000
        )
    await _click_no_status_tab(page)
    return f"{PAGER_BASE}/{locale}/{org_slug}/chats"


async def _chat_thread_active(page, conv_id: str) -> bool:
    cid = (conv_id or "").strip()
    if cid and cid in page.url:
        pass
    elif cid:
        in_dom = await page.evaluate(
            f"""() => {{
                const id = "{cid}";
                return document.querySelector('a[href*="' + id + '"]') !== null
                    || (location.href || '').includes(id);
            }}"""
        )
        if not in_dom:
            return False
    try:
        body = await page.locator("body").inner_text()
        if "Оберіть чат" in body or "Select a chat" in body:
            return False
    except Exception:
        pass
    has_thread = await page.evaluate(
        """() => {
            const main = document.querySelector('main') || document.body;
            const text = (main.innerText || '').trim();
            if (/\\d{1,2}:\\d{2}/.test(text) && text.length > 180) return true;
            const bubbles = document.querySelectorAll(
                '[class*="message"], [class*="Message"],'
                + '[data-testid*="message"], img[src*="fbcdn"]'
            );
            return bubbles.length > 1;
        }"""
    )
    if has_thread:
        return True
    return await _wait_for_chat_composer(page, timeout_ms=5000)


async def _open_conversation_in_ui(
    page,
    *,
    locale: str,
    org_slug: str,
    conv_id: str,
    client_name: str = "",
) -> bool:
    """Open chat in Pager SPA: inbox + «Без статусу» → sidebar/search → URL."""
    inbox = await _ensure_inbox(page, locale=locale, org_slug=org_slug)
    chat_url = f"{inbox}/{conv_id}"

    if await _chat_thread_active(page, conv_id):
        logger.info("browser chat already open conv=%s", conv_id[:8])
        await _click_take_ui(page, conv_id)
        return True

    opened = False
    if await _click_conv_in_sidebar(page, conv_id):
        opened = await _chat_thread_active(page, conv_id)

    if not opened and client_name:
        if await _search_and_open_chat(
            page, client_name=client_name, conv_id=conv_id
        ):
            opened = await _chat_thread_active(page, conv_id)

    if not opened:
        await page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2500)
        opened = await _chat_thread_active(page, conv_id)

    await _click_take_ui(page, conv_id)

    if opened:
        logger.info("browser chat open conv=%s (active)", conv_id[:8])
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        return True

    for attempt in range(2):
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2000)
        await _click_take_ui(page, conv_id)
        if await _chat_thread_active(page, conv_id):
            logger.info(
                "browser chat open conv=%s (retry %s)",
                conv_id[:8],
                attempt + 1,
            )
            return True

    logger.warning(
        "browser chat not active conv=%s url=%s",
        conv_id[:8],
        page.url[:80],
    )
    return False


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


async def _fetch_saved_reply_text_via_api(
    page,
    *,
    org_id: str,
    script_key: str,
) -> str:
    """Load reply text from /api/reply/folder + /api/reply (same as UI sidebar)."""
    oid = (org_id or "").strip()
    snippet = script_ui_snippet(script_key)
    folder_url = _api(f"/api/reply/folder?orgId={oid}")
    result = await page.evaluate(
        f"""async ({{folderUrl, snippet, folderNames}}) => {{
            {_SAFE_FETCH}
            const fr = await safeJsonFetch(folderUrl);
            if (fr.html || !fr.ok) return {{ error: 'folder fetch failed' }};
            const folders = Array.isArray(fr.data) ? fr.data : [];
            const names = folderNames.map(n => n.toLowerCase());
            const folder = folders.find(f => {{
                const n = (f.name || '').toLowerCase();
                return names.some(x => n.includes(x) || x.includes(n));
            }});
            if (!folder || !folder.id) return {{ error: 'zambia folder not found' }};
            const rr = await safeJsonFetch('/api/reply?folderId=' + folder.id);
            if (rr.html || !rr.ok) return {{ error: 'replies fetch failed' }};
            const replies = Array.isArray(rr.data) ? rr.data : [];
            const needle = snippet.toLowerCase();
            const hit = replies.find(r => (r.text || '').toLowerCase().includes(needle));
            if (!hit || !hit.text) return {{ error: 'reply not found', folder: folder.name }};
            return {{ ok: true, text: hit.text, folder: folder.name }};
        }}""",
        {
            "folderUrl": folder_url,
            "snippet": snippet,
            "folderNames": list(SAVED_REPLY_FOLDER_NAMES),
        },
    )
    if isinstance(result, dict) and result.get("ok") and result.get("text"):
        logger.info(
            "browser saved-reply API key=%s folder=%s chars=%s",
            script_key,
            result.get("folder"),
            len(str(result.get("text") or "")),
        )
        return str(result["text"])
    raise RuntimeError(
        f"Saved reply API miss key={script_key}: {result.get('error') if isinstance(result, dict) else result}"
    )


async def _browser_post_message_spa(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    text: str,
    locale: str,
    org_slug: str,
) -> dict:
    """POST /api/message with the same JSON body Pager SPA uses (fixes imageUrl 500)."""
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    ref = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"
    conv_url = _api(f"/api/conversation/{conv_id}?orgId={oid}")
    members_url = _api("/api/organizationMember")
    post_url = _api(f"/api/message?orgId={oid}&userId={uid}")

    result = await page.evaluate(
        f"""async ({{convUrl, membersUrl, postUrl, referer, convId, userId, text}}) => {{
            {_SAFE_FETCH}
            const convR = await safeJsonFetch(convUrl);
            if (convR.html || !convR.ok) {{
                return {{ status: convR.status, error: 'conv fetch failed', html: convR.html }};
            }}
            const conv = convR.data || {{}};
            const channelId = conv.channelId || (conv.channel && conv.channel.id) || '';
            const recipient = conv.clientPSID || conv.clientPsid || conv.recipient
                || (conv.client && (conv.client.psid || conv.client.PSID)) || '';
            let imageUrl = '';
            const respUser = conv.responsibleUser || conv.responsibleuser || {{}};
            if (respUser.imageUrl) imageUrl = respUser.imageUrl;
            if (!imageUrl) {{
                const memR = await safeJsonFetch(membersUrl);
                const members = Array.isArray(memR.data) ? memR.data : [];
                for (const m of members) {{
                    const mid = m.userId || m.pagerUserId || (m.user && m.user.id) || m.id || '';
                    if (mid === userId) {{
                        imageUrl = m.imageUrl || (m.user && m.user.imageUrl) || '';
                        break;
                    }}
                }}
            }}
            const now = new Date().toISOString();
            const payload = {{
                id: crypto.randomUUID(),
                channelId,
                text,
                conversationId: convId,
                messageDirection: 'outgoing',
                authorId: userId,
                author: {{ id: userId, imageUrl: imageUrl || '' }},
                recipient,
                createdAt: now,
                updatedAt: now,
                lastMessageAt: now,
                optimistic: true,
                isDelivered: null,
                replyToMessageId: null,
            }};
            const headers = {{
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            }};
            if (referer) headers['Referer'] = referer;
            const r = await safeJsonFetch(postUrl, {{
                method: 'POST',
                headers,
                body: JSON.stringify(payload),
            }});
            const p = r.data || {{}};
            const err = typeof p.error === 'string'
                ? p.error
                : (typeof p.message === 'string' ? p.message : (r.raw || '').slice(0, 300));
            return {{
                status: r.status,
                html: r.html,
                error: err,
                authorId: p.authorId || '',
                isDelivered: !!p.isDelivered,
                facebookMessageId: p.facebookMessageId || '',
                channelId,
                hadRecipient: !!recipient,
                hadImageUrl: !!imageUrl,
            }};
        }}""",
        {
            "convUrl": conv_url,
            "membersUrl": members_url,
            "postUrl": post_url,
            "referer": ref,
            "convId": conv_id,
            "userId": uid,
            "text": text,
        },
    )

    if not isinstance(result, dict):
        raise RuntimeError(f"SPA POST invalid response conv={conv_id[:8]}")

    if result.get("html"):
        raise RuntimeError(
            f"SPA POST HTML conv={conv_id[:8]} — session expired"
        )

    status = int(result.get("status") or 0)
    author = str(result.get("authorId") or "").strip()
    fb = str(result.get("facebookMessageId") or "").strip()
    err = str(result.get("error") or "")

    if status < 400 and author == uid and fb:
        logger.info(
            "browser SPA POST sent conv=%s author=%s fb=%s ch=%s",
            conv_id[:8],
            author[:16],
            fb[:12],
            str(result.get("channelId") or "")[:8],
        )
        return result

    raise RuntimeError(
        f"SPA POST failed conv={conv_id[:8]} status={status}: {err[:200]}"
    )


async def _open_saved_replies_panel(page) -> bool:
    """Click «…» saved-replies button left of the message composer."""
    if await page.get_by_text("Збережені відповіді", exact=False).count():
        return True
    opened = await page.evaluate(
        """() => {
            const ta = document.querySelector(
                'textarea[placeholder*="Напиш"], textarea[placeholder*="повідом"],'
                + 'textarea[placeholder*="message" i]'
            );
            if (!ta) return false;
            const taRect = ta.getBoundingClientRect();
            const buttons = [...document.querySelectorAll('button, [role="button"]')]
                .filter(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) return false;
                    return Math.abs(r.top - taRect.top) < 48
                        && r.right <= taRect.left + 8
                        && r.bottom > taRect.top - 20;
                })
                .sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
            const trigger = buttons[0];
            if (!trigger) return false;
            trigger.click();
            return true;
        }"""
    )
    if opened:
        await page.wait_for_timeout(1200)
        logger.info("browser saved-replies panel opened")
        return True
    for sel in (
        'button:has(svg.tabler-icon-message-2)',
        'button:has(svg[class*="message"])',
    ):
        loc = page.locator(sel)
        if await loc.count():
            try:
                await loc.last.click(timeout=5000)
                await page.wait_for_timeout(1200)
                logger.info("browser saved-replies panel opened (%s)", sel)
                return True
            except Exception:
                continue
    return False


async def _open_zambia_saved_folder(page) -> bool:
    """Open «Замбія» folder inside saved replies."""
    for name in SAVED_REPLY_FOLDER_NAMES:
        loc = page.get_by_text(name, exact=False)
        if await loc.count():
            try:
                await loc.first.click(timeout=5000)
                await page.wait_for_timeout(1000)
                logger.info("browser saved-replies folder=%s", name)
                return True
            except Exception:
                continue
    folder = page.locator("div, button, a").filter(
        has_text=re.compile(r"Замб", re.I)
    )
    if await folder.count():
        await folder.first.click(force=True)
        await page.wait_for_timeout(1000)
        logger.info("browser saved-replies folder=Замб (regex)")
        return True
    return False


async def _ensure_zambia_replies_sidebar(page) -> bool:
    """Saved replies sidebar open on Zambia folder."""
    intro = page.get_by_text("Hi! I want to show you", exact=False)
    if await intro.count():
        return True
    if not await _open_saved_replies_panel(page):
        return False
    await page.wait_for_timeout(800)
    if await intro.count():
        return True
    if not await _open_zambia_saved_folder(page):
        return False
    await page.wait_for_timeout(800)
    return await intro.count() > 0 or await page.get_by_text(
        "How it works", exact=False
    ).count() > 0


async def _click_saved_reply_send(page, snippet: str) -> bool:
    """Click blue send icon on the saved reply row matching snippet."""
    sn = (snippet or "").strip()
    if not sn:
        return False
    clicked = await page.evaluate(
        """(snippet) => {
            const needle = snippet.toLowerCase();
            const candidates = [];
            for (const el of document.querySelectorAll('div, li, article, button')) {
                const t = (el.innerText || '').trim();
                if (!t || t.length > 600) continue;
                if (!t.toLowerCase().includes(needle)) continue;
                candidates.push({ el, len: t.length });
            }
            candidates.sort((a, b) => a.len - b.len);
            for (const { el } of candidates) {
                let row = el;
                for (let i = 0; i < 6 && row; i++) {
                    const buttons = [...row.querySelectorAll('button')];
                    if (buttons.length >= 1) {
                        const sendBtn = buttons[buttons.length - 1];
                        sendBtn.click();
                        return true;
                    }
                    row = row.parentElement;
                }
            }
            return false;
        }""",
        sn,
    )
    if clicked:
        await page.wait_for_timeout(1800)
        logger.info("browser saved-reply send snippet=%r", sn[:40])
        return True
    row = page.locator("div").filter(has_text=sn).last
    if await row.count():
        btn = row.locator("button").last
        if await btn.count():
            await btn.click(force=True)
            await page.wait_for_timeout(1800)
            logger.info("browser saved-reply send (locator) snippet=%r", sn[:40])
            return True
    return False


async def _send_via_saved_reply(
    page,
    *,
    script_key: str,
    conv_id: str,
    org_id: str,
    user_id: str,
    locale: str = "uk",
    org_slug: str = "",
) -> None:
    """Send canned reply: load text from /api/reply (Замбія), POST like Pager SPA."""
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    try:
        text = await _fetch_saved_reply_text_via_api(
            page, org_id=oid, script_key=script_key
        )
    except Exception as exc:
        logger.warning(
            "saved-reply API key=%s: %s — using local script",
            script_key,
            exc,
        )
        text = load_script("zm", script_key)

    verify_text = script_verify_snippet(script_key)
    await _browser_post_message_spa(
        page,
        conv_id=conv_id,
        org_id=oid,
        user_id=uid,
        text=text,
        locale=locale,
        org_slug=org_slug,
    )

    verify = await _verify_message_delivered(
        page,
        conv_id=conv_id,
        org_id=oid,
        user_id=uid,
        text=verify_text,
        attempts=20,
    )
    if verify.get("ghost"):
        raise RuntimeError(
            f"Saved reply delivery error (conv={conv_id[:8]}) — red !"
        )
    if verify.get("ok"):
        logger.info(
            "browser saved-reply sent key=%s conv=%s author=%s fb=%s",
            script_key,
            conv_id[:8],
            str(verify.get("authorId") or uid)[:16],
            str(verify.get("facebookMessageId") or "")[:12],
        )
        return
    raise RuntimeError(
        f"Saved reply not verified key={script_key} conv={conv_id[:8]}"
    )


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


async def _send_from_open_chat(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    text: str,
    locale: str,
    org_slug: str,
    skip_ui: bool = False,
) -> None:
    """Send like operator UI: composer first, then in-page POST {conversationId, text} only."""
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    ref = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"

    if not skip_ui:
        if conv_id not in page.url:
            if not await _chat_thread_active(page, conv_id):
                await _wait_for_chat_thread(page, conv_id, timeout_ms=15000)
            if conv_id not in page.url and not await _chat_thread_active(page, conv_id):
                await _fast_goto_chat(
                    page,
                    locale=locale,
                    org_slug=org_slug,
                    conv_id=conv_id,
                    org_id=oid,
                )
                await page.wait_for_timeout(2500)
                await _wait_for_chat_thread(page, conv_id, timeout_ms=30000)

        if await _browser_send_via_ui(page, text):
            verify = await _verify_message_delivered(
                page,
                conv_id=conv_id,
                org_id=oid,
                user_id=uid,
                text=text,
                attempts=15,
            )
            if verify.get("ghost"):
                raise RuntimeError(
                    f"Message delivery error (conv={conv_id[:8]}) — red !"
                )
            if verify.get("ok"):
                logger.info(
                    "browser UI sent conv=%s author=%s fb=%s",
                    conv_id[:8],
                    str(verify.get("authorId") or uid)[:16],
                    str(verify.get("facebookMessageId") or "")[:12],
                )
                return
    elif not await _ensure_chat_api_ready(
        page, conv_id=conv_id, org_id=oid, timeout_ms=10000
    ):
        logger.warning(
            "batch send: message API not warm conv=%s — POST anyway",
            conv_id[:8],
        )

    post_url = _api(f"/api/message?orgId={oid}&userId={uid}")
    try:
        await _browser_post_message_spa(
            page,
            conv_id=conv_id,
            org_id=oid,
            user_id=uid,
            text=text,
            locale=locale,
            org_slug=org_slug,
        )
        return
    except Exception as spa_exc:
        logger.warning(
            "SPA POST failed conv=%s: %s — trying minimal POST",
            conv_id[:8],
            spa_exc,
        )

    payload = {"conversationId": conv_id, "text": text}
    result = await page.evaluate(
        f"""async ({{url, payload, referer}}) => {{
            {_SAFE_FETCH}
            const headers = {{
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            }};
            if (referer) headers['Referer'] = referer;
            const r = await safeJsonFetch(url, {{
                method: 'POST',
                headers,
                body: JSON.stringify(payload),
            }});
            const p = r.data || {{}};
            const err = typeof p.error === 'string'
                ? p.error
                : (typeof p.message === 'string' ? p.message : (r.raw || '').slice(0, 300));
            return {{
                status: r.status,
                html: r.html,
                error: err,
                authorId: p.authorId || '',
                isDelivered: !!p.isDelivered,
                facebookMessageId: p.facebookMessageId || '',
            }};
        }}""",
        {"url": post_url, "payload": payload, "referer": ref},
    )

    if result.get("html"):
        raise RuntimeError(
            f"Send POST HTML conv={conv_id[:8]} — open chat in browser first"
        )

    status = int(result.get("status") or 0)
    author = str(result.get("authorId") or "").strip()
    fb = str(result.get("facebookMessageId") or "").strip()
    err = str(result.get("error") or "")

    if status < 400 and author == uid and fb:
        logger.info(
            "browser page POST sent conv=%s author=%s fb=%s",
            conv_id[:8],
            author[:16],
            fb[:12],
        )
        return

    verify = await _verify_message_delivered(
        page,
        conv_id=conv_id,
        org_id=oid,
        user_id=uid,
        text=text,
        attempts=12,
    )
    if verify.get("ghost"):
        raise RuntimeError(
            f"Message delivery error (conv={conv_id[:8]}) — red !"
        )
    if verify.get("ok"):
        logger.info(
            "browser verified sent conv=%s author=%s fb=%s",
            conv_id[:8],
            str(verify.get("authorId") or uid)[:16],
            str(verify.get("facebookMessageId") or "")[:12],
        )
        return

    raise RuntimeError(
        f"Send failed conv={conv_id[:8]} status={status}: {err[:200]}"
    )


async def _batch_send_one_conv(
    context,
    page,
    *,
    conv_id: str,
    texts: list[str],
    script_keys: list[str] | None = None,
    client_name: str,
    channel_hint: str,
    org_id: str,
    org_slug: str,
    user_id: str,
    locale: str,
) -> None:
    """Open chat, take, send via saved replies (Замбія) or POST fallback."""
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    slug = (org_slug or "").strip()
    ch = (channel_hint or "").strip()
    keys = [k.strip() for k in (script_keys or []) if (k or "").strip()]
    bodies = [t.strip() for t in texts if (t or "").strip()]
    if not keys and not bodies:
        return

    await _open_conversation_direct(
        page,
        locale=locale,
        org_slug=slug,
        conv_id=conv_id,
        channel_id=ch,
        org_id=oid,
    )
    if not await _ensure_chat_api_ready(
        page, conv_id=conv_id, org_id=oid, timeout_ms=30000
    ):
        await _open_conversation_direct(
            page,
            locale=locale,
            org_slug=slug,
            conv_id=conv_id,
            channel_id=ch,
            org_id=oid,
        )
        if not await _ensure_chat_api_ready(
            page, conv_id=conv_id, org_id=oid, timeout_ms=20000
        ):
            raise RuntimeError(
                f"Chat API not ready conv={conv_id[:8]} — cannot send"
            )

    client = await _browser_pager_client(
        context,
        org_id=oid,
        org_slug=slug,
        locale=locale,
        user_id=uid,
    )
    taken = await client.take_conversation(conv_id, uid)
    if not taken:
        await _click_take_ui(page, conv_id)
        taken = await client.take_conversation(conv_id, uid)
    if not taken:
        resp = await client._conversation_responsible(conv_id)
        if resp != uid:
            raise RuntimeError(f"Take chat failed conv={conv_id[:8]}")

    try:
        got = await _browser_prepare_outbound(
            page, conv_id=conv_id, org_id=oid, user_id=uid
        )
        if got:
            ch = got
    except Exception as exc:
        logger.warning("prepare outbound conv=%s: %s", conv_id[:8], exc)

    for i, key in enumerate(keys):
        if i:
            await asyncio.sleep(1.0)
        try:
            await _send_via_saved_reply(
                page,
                script_key=key,
                conv_id=conv_id,
                org_id=oid,
                user_id=uid,
                locale=locale,
                org_slug=slug,
            )
        except Exception as exc:
            logger.warning(
                "saved-reply failed key=%s conv=%s: %s — SPA POST fallback",
                key,
                conv_id[:8],
                exc,
            )
            body = load_script("zm", key)
            await _browser_post_message_spa(
                page,
                conv_id=conv_id,
                org_id=oid,
                user_id=uid,
                text=body,
                locale=locale,
                org_slug=slug,
            )
            verify = await _verify_message_delivered(
                page,
                conv_id=conv_id,
                org_id=oid,
                user_id=uid,
                text=script_verify_snippet(key),
                attempts=15,
            )
            if not verify.get("ok"):
                raise RuntimeError(
                    f"Send not verified key={key} conv={conv_id[:8]}"
                )

    for i, body in enumerate(bodies):
        if i:
            await asyncio.sleep(0.8)
        await _send_from_open_chat(
            page,
            conv_id=conv_id,
            org_id=oid,
            user_id=uid,
            text=body,
            locale=locale,
            org_slug=slug,
            skip_ui=True,
        )

    patch_url = _api(f"/api/conversation/{conv_id}?userId={uid}&orgId={oid}")
    await page.evaluate(
        f"""async ({{url}}) => {{
            {_SAFE_FETCH}
            await safeJsonFetch(url, {{
                method: 'PATCH',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{conversationState: 'read'}}),
            }});
        }}""",
        {"url": patch_url},
    )
    logger.info("browser batch done conv=%s texts=%s", conv_id[:8], len(bodies))


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
        for attempt in range(6):
            await page.wait_for_timeout(800)
            await _click_take_ui(page, conv_id)
            check = await _poll_take_state(
                page, conv_id=conv_id, org_id=oid, user_id=uid
            )
            if check.get("responsibleOk") or check.get("alreadyTaken"):
                result["responsibleOk"] = True
                result["responsibleId"] = check.get("responsibleId") or uid
                logger.info(
                    "browser prepare conv=%s take ok after poll %s",
                    conv_id[:8],
                    attempt,
                )
                break
        patch_st = int(result.get("patchStatus") or 0)
        if not result.get("responsibleOk"):
            if patch_st in (200, 204):
                logger.warning(
                    "browser prepare conv=%s take unverified PATCH=%s — proceed",
                    conv_id[:8],
                    patch_st,
                )
            else:
                raise RuntimeError(
                    f"Take chat not verified conv={conv_id[:8]} PATCH={patch_st}"
                )

    ch = str(result.get("channelId") or "").strip()
    if not ch:
        refetch = await _poll_take_state(
            page, conv_id=conv_id, org_id=oid, user_id=uid
        )
        conv_url = _api(f"/api/conversation/{conv_id}?orgId={oid}")
        extra = await page.evaluate(
            f"""async ({{convUrl}}) => {{
                {_SAFE_FETCH}
                const convR = await safeJsonFetch(convUrl);
                const conv = convR.data || {{}};
                return conv.channelId || (conv.channel && conv.channel.id) || '';
            }}""",
            {"convUrl": conv_url},
        )
        ch = str(extra or "").strip()
    logger.info(
        "browser prepared conv=%s patch=%s resp=%s channel=%s",
        conv_id[:8],
        result.get("patchStatus"),
        str(result.get("responsibleId") or "")[:16],
        ch[:8] if ch else "?",
    )
    return ch


async def _export_context_cookies(context) -> dict[str, str]:
    """All pager.co.ua cookies including _pager_org_id (needed for worker poll)."""
    out: dict[str, str] = {}
    for c in await context.cookies():
        dom = str(c.get("domain") or "")
        if "pager.co.ua" not in dom:
            continue
        name = str(c.get("name") or "")
        if not name:
            continue
        out[name] = str(c.get("value") or "")
    return out


async def _cookies_from_context(context) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in await context.cookies():
        dom = str(c.get("domain") or "")
        if "pager.co.ua" not in dom:
            continue
        name = str(c.get("name") or "")
        if name.startswith("_pager_"):
            continue
        out[name] = str(c.get("value") or "")
    return out


async def _fast_goto_chat(
    page,
    *,
    locale: str,
    org_slug: str,
    conv_id: str,
    org_id: str = "",
    channel_id: str = "",
) -> None:
    """Open chat URL and wait for Pager to load message history API."""
    ch = (channel_id or "").strip()
    chat_url = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"
    if ch:
        chat_url += f"?channelId={ch}"
    api_needle = f"convId={conv_id}"
    try:
        async with page.expect_response(
            lambda r: (
                api_needle in r.url
                and "/api/message" in r.url
                and r.status == 200
            ),
            timeout=20000,
        ):
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
        logger.info("browser chat loaded conv=%s (api)", conv_id[:8])
        return
    except Exception:
        pass
    await page.goto(chat_url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    oid = (org_id or "").strip()
    if oid:
        api_msg = _api(
            f"/api/message?convId={conv_id}&orgId={oid}&pageSize=5&page=1"
        )
        for _ in range(12):
            loaded = await page.evaluate(
                f"""async ({{msgUrl}}) => {{
                    {_SAFE_FETCH}
                    const r = await safeJsonFetch(msgUrl);
                    return !r.html && Array.isArray(r.data);
                }}""",
                {"msgUrl": api_msg},
            )
            if loaded:
                logger.info("browser chat loaded conv=%s (poll)", conv_id[:8])
                return
            await page.wait_for_timeout(800)
    await page.wait_for_timeout(1500)
    logger.info("browser chat goto conv=%s", conv_id[:8])


async def _browser_pager_client(
    context,
    *,
    org_id: str,
    org_slug: str,
    locale: str,
    user_id: str,
):
    from services.pager_api import PagerClient

    cookies = await _cookies_from_context(context)
    return PagerClient(
        PAGER_BASE,
        cookies,
        org_id=org_id,
        org_slug=org_slug,
        locale=locale,
        org_id_fallback=org_id,
        session_user_id=user_id,
    )


async def _resolve_channel_id(
    client,
    conv_id: str,
    *,
    channel_hint: str = "",
) -> str:
    ch = (channel_hint or "").strip()
    if ch:
        return ch
    conv = await client.open_conversation(conv_id)
    if not conv:
        return ""
    ch = str(conv.get("channelId") or "").strip()
    nested = conv.get("channel")
    if not ch and isinstance(nested, dict):
        ch = str(nested.get("id") or "").strip()
    return ch


async def _rest_take_chat(
    context,
    page,
    *,
    conv_id: str,
    org_id: str,
    org_slug: str,
    user_id: str,
    locale: str,
    channel_id: str = "",
) -> str:
    """Take chat via REST using live browser cookies (not fragile in-page poll)."""
    uid = (user_id or "").strip()
    ch_hint = (channel_id or "").strip()

    await _fast_goto_chat(
        page,
        locale=locale,
        org_slug=org_slug,
        conv_id=conv_id,
        org_id=org_id,
    )
    await page.wait_for_timeout(1200)

    for attempt in range(5):
        client = await _browser_pager_client(
            context,
            org_id=org_id,
            org_slug=org_slug,
            locale=locale,
            user_id=uid,
        )
        taken = await client.take_conversation(conv_id, uid)
        ch = await _resolve_channel_id(client, conv_id, channel_hint=ch_hint)
        if taken and ch:
            logger.info(
                "REST take OK conv=%s channel=%s try=%s",
                conv_id[:8],
                ch[:8],
                attempt,
            )
            return ch
        if attempt in (0, 2):
            await _click_take_ui(page, conv_id)
        if attempt == 3 and ch and not taken:
            resp = await client._conversation_responsible(conv_id)
            if resp == uid:
                logger.info(
                    "REST take late OK conv=%s channel=%s",
                    conv_id[:8],
                    ch[:8],
                )
                taken = True
        if taken and ch:
            await _warm_chat_for_send(
                page,
                conv_id=conv_id,
                org_id=org_id,
                user_id=uid,
                locale=locale,
                org_slug=org_slug,
            )
            return ch
        await page.wait_for_timeout(1000)

    raise RuntimeError(f"Take chat not verified conv={conv_id[:8]}")


async def _warm_chat_for_send(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    locale: str,
    org_slug: str,
) -> None:
    """Load conv in SPA before POST — aiohttp cannot send without this context."""
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    conv_url = _api(f"/api/conversation/{conv_id}?orgId={oid}")
    msg_url = _api(
        f"/api/message?convId={conv_id}&orgId={oid}&pageSize=10&page=1"
    )
    await page.evaluate(
        f"""async ({{convUrl, msgUrl, userId}}) => {{
            {_SAFE_FETCH}
            await safeJsonFetch(convUrl);
            await safeJsonFetch(msgUrl);
        }}""",
        {"convUrl": conv_url, "msgUrl": msg_url, "userId": uid},
    )
    if conv_id not in page.url:
        if not await _click_conv_in_sidebar(page, conv_id):
            await _fast_goto_chat(
                page,
                locale=locale,
                org_slug=org_slug,
                conv_id=conv_id,
                org_id=oid,
            )
    await page.wait_for_timeout(2500)
    logger.info("browser chat warmed conv=%s", conv_id[:8])


async def _rest_send_texts(
    context,
    page,
    *,
    conv_id: str,
    texts: list[str],
    org_id: str,
    org_slug: str,
    user_id: str,
    locale: str,
    channel_id: str,
) -> None:
    uid = (user_id or "").strip()
    ch = (channel_id or "").strip()
    if not ch:
        raise RuntimeError(f"channelId missing conv={conv_id[:8]}")
    referer = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"

    await _warm_chat_for_send(
        page,
        conv_id=conv_id,
        org_id=org_id,
        user_id=uid,
        locale=locale,
        org_slug=org_slug,
    )

    for i, body in enumerate(texts):
        if i:
            await asyncio.sleep(0.8)
        sent = False

        if await _browser_send_via_ui(page, body):
            verify = await _verify_message_delivered(
                page,
                conv_id=conv_id,
                org_id=org_id,
                user_id=uid,
                text=body,
                attempts=15,
            )
            if verify.get("ghost"):
                raise RuntimeError(
                    f"Message delivery error (conv={conv_id[:8]}) — red !"
                )
            if verify.get("ok") and str(
                verify.get("facebookMessageId") or ""
            ).strip():
                logger.info(
                    "browser UI sent conv=%s author=%s fb=%s",
                    conv_id[:8],
                    str(verify.get("authorId") or uid)[:16],
                    str(verify.get("facebookMessageId") or "")[:12],
                )
                sent = True

        if not sent:
            try:
                await _send_one_in_session(
                    context,
                    page,
                    conv_id=conv_id,
                    org_id=org_id,
                    user_id=uid,
                    text=body,
                    referer=referer,
                    channel_id=ch,
                )
                sent = True
            except RuntimeError as exc:
                if "HTML" not in str(exc) and "session not active" not in str(exc):
                    raise
                logger.warning(
                    "browser POST retry conv=%s: %s",
                    conv_id[:8],
                    exc,
                )
                await _warm_chat_for_send(
                    page,
                    conv_id=conv_id,
                    org_id=org_id,
                    user_id=uid,
                    locale=locale,
                    org_slug=org_slug,
                )
                await _send_one_in_session(
                    context,
                    page,
                    conv_id=conv_id,
                    org_id=org_id,
                    user_id=uid,
                    text=body,
                    referer=referer,
                    channel_id=ch,
                )

        if not sent:
            raise RuntimeError(f"Send failed conv={conv_id[:8]}")


async def _ensure_take_verified(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    locale: str,
    org_slug: str,
) -> str:
    """Take chat as operator — required before send (no ghosts)."""
    uid = (user_id or "").strip()
    oid = (org_id or "").strip()
    patch_url = _api(f"/api/conversation/{conv_id}?userId={uid}&orgId={oid}")

    for attempt in range(8):
        await _click_take_ui(page, conv_id)
        await page.evaluate(
            f"""async ({{url, userId}}) => {{
                {_SAFE_FETCH}
                await safeJsonFetch(url, {{
                    method: 'PATCH',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{
                        responsibleUserId: userId,
                        conversationState: 'read',
                    }}),
                }});
            }}""",
            {"url": patch_url, "userId": uid},
        )
        await page.wait_for_timeout(1500)
        check = await _poll_take_state(
            page, conv_id=conv_id, org_id=oid, user_id=uid
        )
        if check.get("sessionError"):
            if attempt in (0, 3):
                await _fast_goto_chat(
                    page, locale=locale, org_slug=org_slug, conv_id=conv_id
                )
                await page.wait_for_timeout(2000)
            else:
                await page.wait_for_timeout(1000)
            continue
        if (
            check.get("responsibleOk")
            or check.get("systemTake")
            or check.get("alreadyTaken")
        ):
            conv_url = _api(f"/api/conversation/{conv_id}?orgId={oid}")
            ch = await page.evaluate(
                f"""async ({{convUrl}}) => {{
                    {_SAFE_FETCH}
                    const convR = await safeJsonFetch(convUrl);
                    const conv = convR.data || {{}};
                    return conv.channelId || (conv.channel && conv.channel.id) || '';
                }}""",
                {"convUrl": conv_url},
            )
            ch = str(ch or "").strip()
            if not ch:
                logger.warning(
                    "take ok but channel empty conv=%s try=%s",
                    conv_id[:8],
                    attempt,
                )
                await page.wait_for_timeout(800)
                continue
            logger.info(
                "browser take verified conv=%s resp=%s channel=%s try=%s",
                conv_id[:8],
                str(check.get("responsibleId") or uid)[:16],
                ch[:8],
                attempt,
            )
            msg_url = _api(
                f"/api/message?convId={conv_id}&orgId={oid}&pageSize=5&page=1"
            )
            await page.evaluate(
                f"""async ({{msgUrl}}) => {{
                    {_SAFE_FETCH}
                    await safeJsonFetch(msgUrl);
                }}""",
                {"msgUrl": msg_url},
            )
            return ch
        await page.wait_for_timeout(800)

    raise RuntimeError(f"Take chat not verified conv={conv_id[:8]}")


async def _fast_take_chat(
    page,
    *,
    conv_id: str,
    org_id: str,
    user_id: str,
    locale: str = "uk",
    org_slug: str = "",
) -> str:
    return await _ensure_take_verified(
        page,
        conv_id=conv_id,
        org_id=org_id,
        user_id=user_id,
        locale=locale,
        org_slug=org_slug,
    )


async def _post_via_context_request(
    context,
    *,
    post_url: str,
    payload: dict,
    referer: str,
) -> dict:
    import json as _json

    resp = await context.request.post(
        post_url,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": referer,
            "Origin": PAGER_BASE,
        },
        data=_json.dumps(payload),
    )
    raw = (await resp.text())[:400]
    data = None
    try:
        data = _json.loads(raw) if raw else None
    except _json.JSONDecodeError:
        data = None
    html = raw.lstrip().startswith("<!")
    body = data if isinstance(data, dict) else {}
    err = ""
    if isinstance(body.get("error"), str):
        err = body["error"]
    elif isinstance(body.get("message"), str):
        err = body["message"]
    elif html:
        err = "HTML response"
    return {
        "status": resp.status,
        "html": html,
        "error": err,
        "raw": raw,
        "authorId": str(body.get("authorId") or ""),
        "isDelivered": bool(body.get("isDelivered")),
        "facebookMessageId": str(body.get("facebookMessageId") or ""),
        "bodyKeys": sorted(payload.keys()),
    }


async def _post_message_try(
    context,
    page,
    *,
    post_url: str,
    payload: dict,
    referer: str,
) -> dict:
    """Try Playwright request context first, then in-page fetch."""
    result = await _post_via_context_request(
        context, post_url=post_url, payload=payload, referer=referer
    )
    if not result.get("html") and int(result.get("status") or 0) < 500:
        return result
    return await _post_message_payload(
        page, post_url=post_url, payload=payload, referer=referer
    )


async def _post_message_payload(
    page,
    *,
    post_url: str,
    payload: dict,
    referer: str = "",
) -> dict:
    ref = (referer or "").strip()
    return await page.evaluate(
        f"""async ({{url, payload, referer}}) => {{
            {_SAFE_FETCH}
            const headers = {{
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            }};
            if (referer) headers['Referer'] = referer;
            const r = await safeJsonFetch(url, {{
                method: 'POST',
                headers,
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
        {"url": post_url, "payload": payload, "referer": ref},
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
    channel_id: str = "",
) -> None:
    """POST from browser session — channelId required when SPA composer inactive."""
    qs = f"orgId={org_id}&userId={user_id}"
    post_url = _api(f"/api/message?{qs}")
    ch = (channel_id or "").strip()
    uid = (user_id or "").strip()
    ref = (referer or "").strip()

    if conv_id not in page.url:
        logger.warning(
            "browser POST conv=%s off chat page — goto",
            conv_id[:8],
        )
        slug_match = re.search(r"/([^/]+)/chats", ref or page.url)
        slug = slug_match.group(1) if slug_match else ""
        loc_match = re.search(r"/(uk|en)/", ref or page.url)
        loc = loc_match.group(1) if loc_match else "uk"
        if slug:
            await _fast_goto_chat(
                page, locale=loc, org_slug=slug, conv_id=conv_id
            )
            ref = f"{PAGER_BASE}/{loc}/{slug}/chats/{conv_id}"

    if not uid:
        raise RuntimeError(f"operator user_id missing conv={conv_id[:8]}")

    payloads: list[dict] = [{"conversationId": conv_id, "text": text}]
    if ch:
        payloads.append(
            {"conversationId": conv_id, "text": text, "channelId": ch}
        )
        payloads.append(
            {
                "conversationId": conv_id,
                "text": text,
                "channelId": ch,
                "authorId": uid,
            }
        )
    last_err = ""
    last_status = 0
    result: dict = {}
    status = 0
    author = ""
    fb = ""

    for payload in payloads:
        result = await _post_message_try(
            context,
            page,
            post_url=post_url,
            payload=payload,
            referer=ref,
        )
        status = int(result.get("status") or 0)
        author = str(result.get("authorId") or "").strip()
        fb = str(result.get("facebookMessageId") or "").strip()
        last_err = str(result.get("error") or result.get("raw") or last_err)[:400]
        last_status = status or last_status
        if result.get("html") or str(last_err).lstrip().startswith("<!DOCTYPE"):
            logger.warning(
                "browser POST HTML conv=%s keys=%s",
                conv_id[:8],
                sorted(payload.keys()),
            )
            continue
        if status < 400 and author == uid and fb and result.get("isDelivered"):
            logger.info(
                "browser POST sent conv=%s author=%s fb=%s keys=%s",
                conv_id[:8],
                author[:16],
                fb[:12],
                sorted(payload.keys()),
            )
            return
        if status >= 400 and (
            "imageurl" in last_err.lower()
            or "channel.findunique" in last_err.lower()
        ):
            continue
        if status >= 400:
            break

    if status >= 400:
        raise RuntimeError(
            f"Send POST failed conv={conv_id[:8]} status={status}: {last_err[:200]}"
        )
    if result.get("html") or str(last_err).lstrip().startswith("<!DOCTYPE"):
        raise RuntimeError(
            f"Send POST HTML conv={conv_id[:8]} — chat session not active"
        )
    if author == uid and fb and result.get("isDelivered"):
        logger.info(
            "browser POST sent conv=%s author=%s fb=%s",
            conv_id[:8],
            author[:16],
            fb[:12],
        )
        return
    if author and author != uid:
        raise RuntimeError(
            f"Send as wrong author conv={conv_id[:8]} author={author[:16]}"
        )

    verify = await _verify_message_delivered(
        page,
        conv_id=conv_id,
        org_id=org_id,
        user_id=user_id,
        text=text,
        attempts=15,
    )
    if verify.get("ghost"):
        raise RuntimeError(
            f"Message delivery error in UI (conv={conv_id[:8]}) — red !"
        )
    if verify.get("ok") and str(verify.get("facebookMessageId") or "").strip():
        logger.info(
            "browser POST sent conv=%s author=%s fb=%s (verified)",
            conv_id[:8],
            str(verify.get("authorId") or user_id)[:16],
            str(verify.get("facebookMessageId") or "")[:12],
        )
        return

    raise RuntimeError(
        f"Send not delivered conv={conv_id[:8]} status={last_status}: "
        f"{last_err or 'no facebookMessageId'}"
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
    channel_id: str = "",
) -> None:
    await _send_from_open_chat(
        page,
        conv_id=conv_id,
        org_id=org_id,
        user_id=user_id,
        text=text,
        locale=locale,
        org_slug=org_slug,
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
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            user_agent=UA,
            locale="uk-UA",
            viewport={"width": 1400, "height": 900},
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
            channel_id = await _browser_prepare_outbound(
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
                    channel_id=channel_id,
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

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            user_agent=UA,
            locale="uk-UA",
            viewport={"width": 1400, "height": 900},
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
                channel_id = await _browser_prepare_outbound(
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
                channel_id = await _browser_prepare_outbound(
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
                    channel_id=channel_id,
                )
        finally:
            await browser.close()


async def send_batch_via_browser(
    jobs: Sequence[
        tuple[str, list[str]]
        | tuple[str, list[str], str]
        | tuple[str, list[str], str, str]
        | tuple[str, list[str], str, str, list[str]]
    ],
    *,
    org_id: str,
    org_slug: str,
    user_id: str,
    locale: str = "uk",
    email: str = "",
    password: str = "",
) -> tuple[set[str], dict[str, str]]:
    """One Playwright login, send to multiple conversations. Returns (ok conv ids, cookies)."""
    if not jobs:
        return set(), {}

    slug = (org_slug or "").strip()
    oid = (org_id or "").strip()
    uid = (user_id or "").strip()
    if not slug or not oid or not uid:
        raise RuntimeError("org_slug, org_id, user_id required")
    if not (email or "").strip() or not (password or "").strip():
        raise RuntimeError("email/password required for batch browser send")

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]
    from playwright.async_api import async_playwright

    ok: set[str] = set()
    fresh_cookies: dict[str, str] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            user_agent=UA,
            locale="uk-UA",
            viewport={"width": 1400, "height": 900},
        )
        context.set_default_timeout(25000)
        page = await context.new_page()
        try:
            logger.info("browser batch login jobs=%s", len(jobs))
            await asyncio.wait_for(
                playwright_sign_in_on_page(page, email.strip(), password),
                timeout=120.0,
            )
            org_inbox = f"{PAGER_BASE}/{locale}/{slug}/chats"
            first_ch = ""
            for job in jobs:
                if len(job) > 3 and (job[3] or "").strip():
                    first_ch = (job[3] or "").strip()
                    break
            if first_ch:
                org_inbox += f"?channelId={first_ch}"
            await page.goto(org_inbox, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2000)
            await _wait_for_inbox_hydrated(
                page, org_id=oid, channel_id=first_ch, timeout_ms=60000
            )
            await _click_no_status_tab(page)
            await _verify_logged_in_operator(page, uid)
            logger.info("browser inbox ready")

            for job in jobs:
                conv_id = job[0]
                texts = job[1]
                client_name = (job[2] if len(job) > 2 else "").strip()
                channel_hint = (job[3] if len(job) > 3 else "").strip()
                script_keys = list(job[4]) if len(job) > 4 else []
                bodies = [t.strip() for t in texts if (t or "").strip()]
                keys = [k.strip() for k in script_keys if (k or "").strip()]
                if not bodies and not keys:
                    continue
                try:
                    logger.info(
                        "browser batch conv=%s texts=%s keys=%s channel=%s client=%r",
                        conv_id[:8],
                        len(bodies),
                        keys,
                        channel_hint[:8] if channel_hint else "?",
                        (client_name or "")[:24],
                    )
                    await _batch_send_one_conv(
                        context,
                        page,
                        conv_id=conv_id,
                        texts=bodies,
                        script_keys=keys,
                        client_name=client_name,
                        channel_hint=channel_hint,
                        org_id=oid,
                        org_slug=slug,
                        user_id=uid,
                        locale=locale,
                    )
                    ok.add(conv_id)
                except Exception as exc:
                    logger.warning(
                        "browser batch conv=%s failed: %s",
                        conv_id[:8],
                        exc,
                    )
        except asyncio.CancelledError:
            try:
                fresh_cookies = await _export_context_cookies(context)
            except Exception:
                pass
            raise
        finally:
            try:
                fresh_cookies = await _export_context_cookies(context)
            except Exception:
                pass
            await browser.close()
    return ok, fresh_cookies


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
