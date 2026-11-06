"""Grailed (experimental).

Grailed has no public API. Its own website searches through Algolia, using a search-only
key that ships with the page. This connector reads that key from grailed.com and runs the
same kind of search the site does. It's unofficial, it can break whenever Grailed changes
their site, and automated access may be against Grailed's terms, so it's off unless you
enable it, and it should be used gently (personal use, infrequent checks).

If auto-discovery stops working, set GRAILED_ALGOLIA_APP_ID / GRAILED_ALGOLIA_API_KEY
(and optionally GRAILED_ALGOLIA_INDEX) from your browser's network tab.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime

import httpx

from . import Listing, ListingUpdate, SourceStatus

HOME = "https://www.grailed.com/"
DEFAULT_INDEXES = ["Listing_by_date_added_production", "Listing_production"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Referer": HOME,
    "Origin": "https://www.grailed.com",
}

# Patterns for the Algolia app id + search key embedded in the page
_KEY_PATTERNS = [
    re.compile(r'"?(?:appId|applicationId|app_id|ALGOLIA_APP_ID)"?\s*[:=]\s*"([A-Z0-9]{10})"'
               r'.{0,400}?"?(?:apiKey|searchApiKey|search_api_key|searchKey|ALGOLIA_API_KEY)"?'
               r'\s*[:=]\s*"([A-Za-z0-9]{20,})"', re.S),
    re.compile(r'"?(?:apiKey|searchApiKey|search_api_key|searchKey|ALGOLIA_API_KEY)"?\s*[:=]\s*'
               r'"([A-Za-z0-9]{20,})".{0,400}?"?(?:appId|applicationId|app_id|ALGOLIA_APP_ID)"?'
               r'\s*[:=]\s*"([A-Z0-9]{10})"', re.S),
]


def find_algolia_credentials(html: str) -> tuple[str, str] | None:
    """Return (app_id, api_key) from Grailed's page source, if present."""
    m = _KEY_PATTERNS[0].search(html)
    if m:
        return m.group(1), m.group(2)
    m = _KEY_PATTERNS[1].search(html)
    if m:
        return m.group(2), m.group(1)
    return None


def _ts(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v / 1000 if v > 1e12 else v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def parse_hits(body: dict) -> list[Listing]:
    out = []
    for h in body.get("hits") or []:
        lid = str(h.get("id") or h.get("objectID") or "")
        if not lid:
            continue
        designers = h.get("designer_names") or ", ".join(
            d.get("name", "") for d in h.get("designers") or [] if isinstance(d, dict))
        title = h.get("title", "")
        if designers and designers.lower() not in title.lower():
            title = f"{designers} · {title}"
        photo = h.get("cover_photo") or {}
        out.append(Listing(
            listing_id=lid,
            title=title,
            price=float(h["price"]) if h.get("price") is not None else None,
            currency="USD",
            url=f"https://www.grailed.com/listings/{lid}",
            image_url=photo.get("url", "") if isinstance(photo, dict) else "",
            size_text=str(h.get("size") or ""),
            condition=str(h.get("condition") or "").replace("is_", "").replace("_", " "),
            listed_at=_ts(h.get("created_at") or h.get("bumped_at")),
        ))
    return out


class GrailedSource:
    key = "grailed"
    label = "Grailed"
    experimental = True

    def __init__(self):
        self.app_id = os.getenv("GRAILED_ALGOLIA_APP_ID", "")
        self.api_key = os.getenv("GRAILED_ALGOLIA_API_KEY", "")
        self.indexes = [os.getenv("GRAILED_ALGOLIA_INDEX")] if os.getenv("GRAILED_ALGOLIA_INDEX") \
            else list(DEFAULT_INDEXES)
        self._discovered_at = 0.0
        self._last_error = ""

    def status(self) -> SourceStatus:
        if self._last_error:
            return SourceStatus(False, self._last_error)
        if self.app_id:
            return SourceStatus(True, f"Unofficial search · index {self.indexes[0]}")
        return SourceStatus(True, "Unofficial search · connects on first check")

    def _discover(self) -> None:
        resp = httpx.get(HOME, headers=HEADERS, timeout=30, follow_redirects=True)
        creds = find_algolia_credentials(resp.text)
        if not creds:
            self._last_error = ("Couldn't find Grailed's search key on grailed.com. Set "
                                "GRAILED_ALGOLIA_APP_ID / GRAILED_ALGOLIA_API_KEY manually.")
            raise RuntimeError(self._last_error)
        self.app_id, self.api_key = creds
        self._discovered_at = time.time()

    def search(self, query: str, *, max_price: float | None, brand: str = "",
               limit: int = 40) -> list[Listing]:
        manual = bool(os.getenv("GRAILED_ALGOLIA_API_KEY"))
        if not self.app_id or (not manual and time.time() - self._discovered_at > 6 * 3600):
            self._discover()
        params = {"query": query, "hitsPerPage": limit}
        if max_price:
            params["numericFilters"] = [f"price_i<={max_price * 1.5:.0f}"]
        last = ""
        for index in self.indexes:
            resp = httpx.post(
                f"https://{self.app_id.lower()}-dsn.algolia.net/1/indexes/{index}/query",
                headers={**HEADERS, "X-Algolia-Application-Id": self.app_id,
                         "X-Algolia-API-Key": self.api_key},
                json=params,
                timeout=30,
            )
            if resp.status_code == 200:
                self._last_error = ""
                if index != self.indexes[0]:
                    self.indexes.remove(index)
                    self.indexes.insert(0, index)
                return parse_hits(resp.json())
            if resp.status_code == 400 and "price_i" in resp.text:
                params.pop("numericFilters", None)  # field name differs; filter by price later
                resp = httpx.post(
                    f"https://{self.app_id.lower()}-dsn.algolia.net/1/indexes/{index}/query",
                    headers={**HEADERS, "X-Algolia-Application-Id": self.app_id,
                             "X-Algolia-API-Key": self.api_key},
                    json=params, timeout=30)
                if resp.status_code == 200:
                    self._last_error = ""
                    return parse_hits(resp.json())
            if resp.status_code in (401, 403) and not manual:
                self.app_id = ""  # key rotated; rediscover next time
            last = f"Grailed search failed ({resp.status_code}): {resp.text[:200]}"
        self._last_error = last
        raise RuntimeError(last)

    # ---- seen-before listings ------------------------------------------------
    def _get_object(self, object_id: str) -> dict | None:
        if not self.app_id:
            self._discover()
        resp = httpx.get(
            f"https://{self.app_id.lower()}-dsn.algolia.net/1/indexes/{self.indexes[0]}/{object_id}",
            headers={**HEADERS, "X-Algolia-Application-Id": self.app_id,
                     "X-Algolia-API-Key": self.api_key},
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"Grailed lookup failed ({resp.status_code})")
        return resp.json()

    def refresh(self, listing: dict) -> ListingUpdate | None:
        hit = self._get_object(listing["listing_id"])
        if hit is None or hit.get("sold") or hit.get("deleted"):
            return ListingUpdate(available=False)
        return ListingUpdate(price=float(hit["price"]) if hit.get("price") is not None else None)

    def details(self, listing: dict) -> str:
        hit = self._get_object(listing["listing_id"]) or {}
        parts = [str(hit.get(k) or "") for k in ("description", "measurements")]
        return "\n".join(p for p in parts if p)[:6000]
