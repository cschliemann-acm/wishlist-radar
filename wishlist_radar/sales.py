"""Sample sales: NYC calendars and online sales, read by Claude from the listing pages.

Each source is just a web page that lists sales. Rather than a fragile parser per site, the
page is turned into plain text (keeping link addresses) and Claude Haiku extracts the sales.
A page that can't be read (blocked, needs JavaScript) shows the reason on the Sales page.
"""

from __future__ import annotations

import html
import re
from datetime import date
from urllib.parse import urljoin

import httpx

DEFAULT_SOURCES = [
    # (name, url, region)
    ("Chicmi: New York", "https://www.chicmi.com/new-york/sales/", "nyc"),
    ("NYC Insider Guide", "https://nycinsiderguide.com/nyc-sample-sale", "nyc"),
    ("Time Out New York", "https://www.timeout.com/newyork/shopping/sample-sales-in-nyc-this-week", "nyc"),
    ("260 Sample Sale", "https://260samplesale.com/", "nyc"),
    ("Arlettie (US)", "https://www.arlettie.com/us/en", "nyc"),
]
REFRESH_HOURS = 6
MAX_PAGE_CHARS = 60000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


def html_to_text(page: str, base_url: str, images: bool = False, limit: int = MAX_PAGE_CHARS) -> str:
    """Readable text with link targets kept, e.g. 'Our Legacy sample sale (https://…)'.
    With images=True, product photos are kept as [image: url] too."""
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>", " ", page)
    if images:
        def img(m: re.Match) -> str:
            tag = m.group(0)
            src = re.search(r'(?:data-src|src)=["\']([^"\']+)["\']', tag, re.I)
            if not src or src.group(1).startswith("data:"):
                return " "
            return f" [image: {urljoin(base_url, html.unescape(src.group(1)))}] "
        page = re.sub(r"(?is)<img\b[^>]*>", img, page)

    def link(m: re.Match) -> str:
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if not text or href.startswith(("#", "javascript:", "mailto:")):
            return f" {text} "
        return f" {text} ({urljoin(base_url, html.unescape(href))}) "

    page = re.sub(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', link, page)
    page = re.sub(r"(?i)<(br|/p|/div|/li|/h\d|/tr|/article|/section)\b[^>]*>", "\n", page)
    page = re.sub(r"<[^>]+>", " ", page)
    page = html.unescape(page)
    page = re.sub(r"[ \t\r\f\v]+", " ", page)
    page = re.sub(r"\n\s*\n+", "\n", page)
    return page.strip()[:limit]


def fetch_page_text(url: str) -> str:
    r = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"the site blocks automated access ({r.status_code})")
    if r.status_code >= 400:
        raise RuntimeError(f"page returned {r.status_code}")
    text = html_to_text(r.text, str(r.url))
    if len(text) < 200:
        raise RuntimeError("page has almost no text (it probably needs JavaScript to load)")
    return text


def sale_key(brand: str, start: str) -> str:
    b = re.sub(r"[^a-z0-9]+", "", brand.lower())
    return f"{b}|{start or 'tba'}"


def brand_matches(sale_text: str, brands: list[str]) -> list[str]:
    """Which of your brands this sale is for (whole-word, case-insensitive)."""
    text = f" {re.sub(r'[^a-z0-9]+', ' ', sale_text.lower())} "
    hits = []
    for b in brands:
        norm = re.sub(r"[^a-z0-9]+", " ", b.lower()).strip()
        if len(norm) >= 3 and f" {norm} " in text:
            hits.append(b)
    return hits


def status_of(sale: dict, today: date | None = None) -> str:
    """'now', 'upcoming', 'tba' or 'past'."""
    today = today or date.today()
    start, end = sale.get("start_date") or "", sale.get("end_date") or ""
    try:
        s = date.fromisoformat(start) if start else None
        e = date.fromisoformat(end) if end else s
    except ValueError:
        return "tba"
    if s is None:
        return "tba"
    if e and e < today:
        return "past"
    return "now" if s <= today else "upcoming"
