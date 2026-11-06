"""Yahoo! Japan Auctions (experimental), bought through the Buyee proxy service.

Japan's biggest resale market, and the main source for Japanese brands (Kapital, Visvim,
Needles, Comoli…). There's no public API for overseas use, so this reads the public search
results page. It can break when Yahoo changes the page, and automated access may be against
their terms: it's off unless you tick it on an item, and checks should be infrequent.

Searches work best in Japanese, so each item gets a Japanese query (written once by Claude).
"""

from __future__ import annotations

import html
import re

import httpx

from . import Listing, SourceStatus

SEARCH_URL = "https://auctions.yahoo.co.jp/search/search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Accept-Language": "ja,en;q=0.8",
}
_TAG = re.compile(r"<a\b[^>]*\bdata-auction-id=\"[^\"]+\"[^>]*>", re.I)
_ATTR = re.compile(r'data-auction-([a-z]+)="([^"]*)"', re.I)


def buyee_url(auction_id: str) -> str:
    return f"https://buyee.jp/item/yahoo/auction/{auction_id}"


def parse_results(page: str) -> list[Listing]:
    seen: dict[str, Listing] = {}
    for tag in _TAG.findall(page):
        attrs = {k.lower(): html.unescape(v) for k, v in _ATTR.findall(tag)}
        aid = attrs.get("id", "")
        if not aid or aid in seen:
            continue
        price = re.sub(r"[^\d.]", "", attrs.get("price", "") or "")
        seen[aid] = Listing(
            listing_id=aid,
            title=attrs.get("title", ""),
            price=float(price) if price else None,
            currency="JPY",
            url=buyee_url(aid),
            image_url=attrs.get("img", ""),
            condition="Auction (Yahoo! Japan via Buyee)",
        )
    return [l for l in seen.values() if l.title]


class YahooJapanSource:
    key = "yahoojp"
    label = "Yahoo! Japan"
    experimental = True

    def __init__(self):
        self._last_error = ""

    def status(self) -> SourceStatus:
        if self._last_error:
            return SourceStatus(False, self._last_error)
        return SourceStatus(True, "Unofficial search · links open on Buyee")

    def search(self, query: str, *, max_price: float | None, brand: str = "",
               limit: int = 50) -> list[Listing]:
        r = httpx.get(SEARCH_URL, params={"p": query, "n": str(limit), "s1": "new", "o1": "d",
                                          "exflg": "1"},
                      headers=HEADERS, timeout=30, follow_redirects=True)
        if r.status_code != 200:
            self._last_error = f"Yahoo! Japan search failed ({r.status_code})"
            raise RuntimeError(self._last_error)
        results = parse_results(r.text)
        if not results and "Product" not in r.text and "auction" not in r.text.lower():
            self._last_error = "Yahoo! Japan returned a page without results (layout may have changed)"
            raise RuntimeError(self._last_error)
        self._last_error = ""
        return results
