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
    """Open chat in SPA — inbox warm-up then direct URL (sidebar unreliable for backlog)."""
    inbox = f"{PAGER_BASE}/{locale}/{org_slug}/chats"
    chat_url = f"{inbox}/{conv_id}"

    if inbox not in page.url or conv_id in page.url:
        if conv_id not in page.url:
            if inbox not in page.url:
                await page.goto(inbox, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2000)
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2000)
    else:
        clicked = await page.evaluate(
            f"""() => {{
                const links = document.querySelectorAll('a[href*="{conv_id}"]');
                for (const el of links) {{
                    el.click();
                    return true;
                }}
                return false;
            }}"""
        )
        if clicked:
            await page.wait_for_timeout(2500)
        else:
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(2000)

    await _click_take_ui(page, conv_id)

    if await _chat_thread_active(page, conv_id):
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

    logger.warning("browser chat not active conv=%s url=%s", conv_id[:8], page.url[:80])
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
            if patch_st in (200, 204):
                logger.warning(
                    "browser prepare conv=%s PATCH %s — proceed without verified take",
                    conv_id[:8],
                    patch_st,
                )
            else:
                raise RuntimeError(
                    f"Take chat failed conv={conv_id[:8]} PATCH={patch_st}"
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
            "browser POST conv=%s off chat page — re-open",
            conv_id[:8],
        )
        slug_match = re.search(r"/([^/]+)/chats", page.url)
        slug = slug_match.group(1) if slug_match else ""
        loc_match = re.search(r"/(uk|en)/", page.url)
        loc = loc_match.group(1) if loc_match else "uk"
        if slug:
            await _open_conversation_in_ui(
                page, locale=loc, org_slug=slug, conv_id=conv_id
            )
            ref = f"{PAGER_BASE}/{loc}/{slug}/chats/{conv_id}"

    payloads: list[dict] = []
    if ch and uid:
        payloads.append(
            {
                "conversationId": conv_id,
                "text": text,
                "channelId": ch,
                "authorId": uid,
            }
        )
    payloads.append({"conversationId": conv_id, "text": text})

    last_err = ""
    last_status = 0

    for payload in payloads:
        result = await _post_message_payload(
            page, post_url=post_url, payload=payload, referer=ref
        )
        status = int(result.get("status") or 0)
        author = str(result.get("authorId") or "").strip()
        fb = str(result.get("facebookMessageId") or "").strip()
        last_err = str(result.get("error") or result.get("raw") or last_err)[:400]
        last_status = status or last_status

        if result.get("html") or str(last_err).lstrip().startswith("<!DOCTYPE"):
            logger.warning(
                "browser POST conv=%s got HTML keys=%s",
                conv_id[:8],
                sorted(payload.keys()),
            )
            continue
        if status >= 400:
            logger.warning(
                "browser POST conv=%s status=%s keys=%s err=%s",
                conv_id[:8],
                status,
                sorted(payload.keys()),
                last_err[:160],
            )
        elif author == uid and (result.get("isDelivered") or fb):
            logger.info(
                "browser POST sent conv=%s author=%s fb=%s keys=%s",
                conv_id[:8],
                author[:16],
                fb[:12],
                sorted(payload.keys()),
            )
            return
        elif author and author != uid:
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
        dom = await _verify_outgoing_dom(page, text)
        if dom.get("ghost"):
            raise RuntimeError(
                f"Message delivery error in UI (conv={conv_id[:8]}) — red !"
            )
        raise RuntimeError(
            f"Message stuck as Facebook page (conv={conv_id[:8]}) — not retrying"
        )

    dom = await _verify_outgoing_dom(page, text)
    if dom.get("ghost"):
        raise RuntimeError(
            f"Message delivery error in UI (conv={conv_id[:8]}) — red !"
        )
    if dom.get("ok") and dom.get("hasAvatar"):
        logger.info(
            "browser DOM verified conv=%s preview=%r",
            conv_id[:8],
            dom.get("preview", "")[:40],
        )
        return

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
    channel_id: str = "",
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
            await asyncio.wait_for(
                playwright_sign_in_on_page(page, email.strip(), password),
                timeout=120.0,
            )
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
                    chat_url = f"{PAGER_BASE}/{locale}/{slug}/chats/{conv_id}"
                    opened = await _open_conversation_in_ui(
                        page, locale=locale, org_slug=slug, conv_id=conv_id
                    )
                    if not opened:
                        logger.warning(
                            "batch conv=%s chat not active — skip send",
                            conv_id[:8],
                        )
                        continue
                    await _click_take_ui(page, conv_id)
                    channel_id = await _browser_prepare_outbound(
                        page, conv_id=conv_id, org_id=oid, user_id=uid
                    )
                    await _click_take_ui(page, conv_id)
                    await page.wait_for_timeout(800)
                    if not await _wait_for_chat_composer(page, timeout_ms=35000):
                        logger.warning(
                            "batch conv=%s composer slow — UI/POST anyway",
                            conv_id[:8],
                        )
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
                    ok.add(conv_id)
                    await page.goto(org_inbox, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(800)
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
