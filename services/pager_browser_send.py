"""Send Pager messages via browser — take chat first, then send like operator UI."""

from __future__ import annotations

import asyncio
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


async def _ensure_inbox(page, *, locale: str, org_slug: str) -> str:
    inbox = f"{PAGER_BASE}/{locale}/{org_slug}/chats"
    if inbox not in page.url:
        await page.goto(inbox, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(2000)
    await _click_no_status_tab(page)
    return inbox


async def _chat_thread_active(page, conv_id: str) -> bool:
    if conv_id not in page.url:
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
) -> None:
    """Open chat URL and wait for Pager to load message history API."""
    chat_url = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"
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
                return ch
        await page.wait_for_timeout(1000)

    raise RuntimeError(f"Take chat not verified conv={conv_id[:8]}")


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
    from services.pager_api import PagerAPIError, message_accepted

    uid = (user_id or "").strip()
    ch = (channel_id or "").strip()
    if not ch:
        raise RuntimeError(f"channelId missing conv={conv_id[:8]}")
    referer = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"
    client = await _browser_pager_client(
        context,
        org_id=org_id,
        org_slug=org_slug,
        locale=locale,
        user_id=uid,
    )

    for i, body in enumerate(texts):
        if i:
            await asyncio.sleep(0.8)
        try:
            result = await client.post_message_after_take(
                conv_id, body, user_id=uid, channel_id=ch
            )
            if message_accepted(result, uid):
                logger.info(
                    "REST sent conv=%s author=%s fb=%s",
                    conv_id[:8],
                    str(result.get("authorId") or uid)[:16],
                    str(result.get("facebookMessageId") or "")[:12],
                )
                continue
            logger.warning(
                "REST not accepted conv=%s author=%s delivered=%s",
                conv_id[:8],
                str(result.get("authorId") or "")[:16],
                result.get("isDelivered"),
            )
        except PagerAPIError as exc:
            logger.warning(
                "REST post failed conv=%s: %s",
                conv_id[:8],
                (exc.body or str(exc))[:200],
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

    if not ch:
        raise RuntimeError(f"channelId missing conv={conv_id[:8]} — cannot send")
    if not uid:
        raise RuntimeError(f"operator user_id missing conv={conv_id[:8]}")

    payload = {
        "conversationId": conv_id,
        "text": text,
        "channelId": ch,
        "authorId": uid,
    }
    last_err = ""
    last_status = 0

    result = await _post_message_payload(page, post_url=post_url, payload=payload, referer=ref)
    status = int(result.get("status") or 0)
    author = str(result.get("authorId") or "").strip()
    fb = str(result.get("facebookMessageId") or "").strip()
    last_err = str(result.get("error") or result.get("raw") or last_err)[:400]
    last_status = status or last_status

    if result.get("html") or str(last_err).lstrip().startswith("<!DOCTYPE"):
        raise RuntimeError(
            f"Send POST HTML conv={conv_id[:8]} — chat session not active"
        )
    if status >= 400:
        raise RuntimeError(
            f"Send POST failed conv={conv_id[:8]} status={status}: {last_err[:200]}"
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
    from services.pager_api import PagerAPIError, PagerClient, message_accepted

    referer = f"{PAGER_BASE}/{locale}/{org_slug}/chats/{conv_id}"
    uid = (user_id or "").strip()
    ch = (channel_id or "").strip()
    if not ch:
        raise RuntimeError(f"channelId missing conv={conv_id[:8]}")
    await page.wait_for_timeout(2000)

    cookies = await _cookies_from_context(context)
    if cookies:
        client = PagerClient(
            PAGER_BASE,
            cookies,
            org_id=org_id,
            org_slug=org_slug,
            locale=locale,
            org_id_fallback=org_id,
            session_user_id=uid,
        )
        try:
            result = await client.post_message_after_take(
                conv_id, text, user_id=uid, channel_id=ch
            )
            if message_accepted(result, uid):
                logger.info(
                    "REST sent conv=%s author=%s fb=%s",
                    conv_id[:8],
                    str(result.get("authorId") or uid)[:16],
                    str(result.get("facebookMessageId") or "")[:12],
                )
                return
            logger.warning(
                "REST not accepted conv=%s author=%s delivered=%s",
                conv_id[:8],
                str(result.get("authorId") or "")[:16],
                result.get("isDelivered"),
            )
        except PagerAPIError as exc:
            logger.warning(
                "REST post failed conv=%s: %s",
                conv_id[:8],
                (exc.body or str(exc))[:200],
            )

    if await _wait_for_chat_composer(page, timeout_ms=35000):
        if await _browser_send_via_ui(page, text):
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
                return

    await _send_one_in_session(
        context,
        page,
        conv_id=conv_id,
        org_id=org_id,
        user_id=user_id,
        text=text,
        referer=referer,
        channel_id=channel_id,
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

    launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    from playwright.async_api import async_playwright

    ok: set[str] = set()
    fresh_cookies: dict[str, str] = {}

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
            await asyncio.wait_for(
                playwright_sign_in_on_page(page, email.strip(), password),
                timeout=120.0,
            )
            org_inbox = f"{PAGER_BASE}/{locale}/{slug}/chats"
            await page.goto(org_inbox, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2000)
            await _click_no_status_tab(page)
            await _verify_logged_in_operator(page, uid)
            logger.info("browser inbox ready")

            for job in jobs:
                conv_id = job[0]
                texts = job[1]
                channel_hint = (job[3] if len(job) > 3 else "").strip()
                bodies = [t.strip() for t in texts if (t or "").strip()]
                if not bodies:
                    continue
                try:
                    logger.info(
                        "browser batch conv=%s texts=%s channel=%s",
                        conv_id[:8],
                        len(bodies),
                        channel_hint[:8] if channel_hint else "?",
                    )
                    channel_id = await _rest_take_chat(
                        context,
                        page,
                        conv_id=conv_id,
                        org_id=oid,
                        org_slug=slug,
                        user_id=uid,
                        locale=locale,
                        channel_id=channel_hint,
                    )
                    await _rest_send_texts(
                        context,
                        page,
                        conv_id=conv_id,
                        texts=bodies,
                        org_id=oid,
                        org_slug=slug,
                        user_id=uid,
                        locale=locale,
                        channel_id=channel_id,
                    )
                    ok.add(conv_id)
                except Exception as exc:
                    logger.warning(
                        "browser batch conv=%s failed: %s",
                        conv_id[:8],
                        exc,
                    )
        finally:
            try:
                for c in await context.cookies():
                    dom = str(c.get("domain") or "")
                    if "pager.co.ua" not in dom:
                        continue
                    name = str(c.get("name") or "")
                    if name.startswith("_pager_"):
                        continue
                    fresh_cookies[name] = str(c.get("value") or "")
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
