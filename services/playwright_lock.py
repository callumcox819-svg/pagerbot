"""Serialize Playwright / Chromium usage (Railway OOM + TargetClosedError)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()

DEFAULT_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
]


@asynccontextmanager
async def playwright_exclusive():
    """Only one Chromium instance at a time across login + worker send."""
    async with _lock:
        yield


async def launch_chromium(
    playwright: Any,
    *,
    headless: bool = True,
    args: list[str] | None = None,
    retries: int = 3,
) -> Any:
    launch_args = list(args or DEFAULT_LAUNCH_ARGS)
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return await playwright.chromium.launch(
                headless=headless,
                args=launch_args,
            )
        except Exception as exc:
            last_exc = exc
            err = str(exc)
            transient = (
                "TargetClosedError" in type(exc).__name__
                or "Target page, context or browser has been closed" in err
                or "Browser closed" in err
            )
            if transient and attempt + 1 < retries:
                wait = 1.5 * (attempt + 1)
                logger.warning(
                    "Chromium launch failed (attempt %s/%s), retry in %.1fs: %s",
                    attempt + 1,
                    retries,
                    wait,
                    err[:120],
                )
                await asyncio.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Chromium launch failed")


@asynccontextmanager
async def chromium_session(
    *,
    headless: bool = True,
    args: list[str] | None = None,
):
    """Exclusive lock + launched Chromium (auto-closed on exit)."""
    from playwright.async_api import async_playwright

    async with playwright_exclusive():
        async with async_playwright() as p:
            browser = await launch_chromium(p, headless=headless, args=args)
            try:
                yield browser
            finally:
                await browser.close()
