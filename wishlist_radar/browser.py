"""A real (headless) Chrome for sites that only work with JavaScript.

Opens a page like a normal visit, waits for it to load, scrolls once so lazy-loaded products
appear, and returns the HTML. Each call starts and closes its own browser, so it's safe from
any thread. Sites with strong bot protection may still refuse a cloud server; that's detected
and reported rather than silently returning nothing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

BLOCK_SIGNS = re.compile(
    r"captcha|are you a human|verify you are human|access denied|request blocked|"
    r"px-captcha|datadome|unusual traffic|pardon our interruption|cf-chl-|just a moment\.\.\.",
    re.I)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


class BlockedError(RuntimeError):
    pass


@dataclass
class Rendered:
    url: str
    status: int
    html: str


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def render(url: str, timeout_s: int = 45) -> Rendered:
    from playwright.sync_api import sync_playwright

    launch = {"headless": True, "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
    if os.getenv("CHROMIUM_PATH"):
        launch["executable_path"] = os.environ["CHROMIUM_PATH"]
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        try:
            ctx = browser.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # busy sites never go fully idle; what's loaded is enough
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(1500)
            html = page.content()
            status = resp.status if resp else 0
            final = page.url
        finally:
            browser.close()
    if status in (401, 403, 429) or (BLOCK_SIGNS.search(html[:20000]) and len(html) < 200_000):
        raise BlockedError(f"the site blocked the browser ({status or 'challenge page'})")
    if status >= 400:
        raise RuntimeError(f"page returned {status}")
    return Rendered(url=final, status=status, html=html)
