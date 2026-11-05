"""eBay via the official Browse API (https://developer.ebay.com/api-docs/buy/browse/overview.html).

Uses an application token (client-credentials grant), so no eBay login is needed, only a
developer keyset.
"""

from __future__ import annotations

import time
from datetime import datetime

import httpx

from . import Listing, SourceStatus

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"

MARKETPLACES = {
    "EBAY_US": ("United States", "USD"),
    "EBAY_GB": ("United Kingdom", "GBP"),
    "EBAY_DE": ("Germany", "EUR"),
    "EBAY_FR": ("France", "EUR"),
    "EBAY_IT": ("Italy", "EUR"),
    "EBAY_ES": ("Spain", "EUR"),
    "EBAY_CA": ("Canada", "CAD"),
    "EBAY_AU": ("Australia", "AUD"),
}


def _parse_time(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_results(body: dict) -> list[Listing]:
    out = []
    for it in body.get("itemSummaries") or []:
        price = it.get("price") or {}
        if not price and it.get("currentBidPrice"):
            price = it["currentBidPrice"]
        image = (it.get("image") or {}).get("imageUrl") or next(
            (i.get("imageUrl") for i in it.get("thumbnailImages") or []), "")
        out.append(Listing(
            listing_id=it["itemId"],
            title=it.get("title", ""),
            price=float(price["value"]) if price.get("value") else None,
            currency=price.get("currency", ""),
            url=it.get("itemWebUrl", ""),
            image_url=image or "",
            condition=it.get("condition", ""),
            listed_at=_parse_time(it.get("itemCreationDate")),
        ))
    return out


class EbaySource:
    key = "ebay"
    label = "eBay"
    experimental = False

    def __init__(self, client_id: str, client_secret: str, marketplace: str = "EBAY_US"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace = marketplace
        self._token = ""
        self._token_expires = 0.0
        self._last_error = ""

    @property
    def currency(self) -> str:
        return MARKETPLACES.get(self.marketplace, ("", "USD"))[1]

    def status(self) -> SourceStatus:
        if not (self.client_id and self.client_secret):
            return SourceStatus(False, "Add EBAY_CLIENT_ID and EBAY_CLIENT_SECRET to the server's settings")
        if self._last_error:
            return SourceStatus(False, self._last_error)
        return SourceStatus(True, f"Official API · {self.marketplace}")

    def _access_token(self) -> str:
        if time.time() < self._token_expires - 60:
            return self._token
        resp = httpx.post(
            TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials", "scope": SCOPE},
            timeout=30,
        )
        if resp.status_code != 200:
            self._last_error = f"eBay login failed ({resp.status_code}): {resp.text[:200]}"
            raise RuntimeError(self._last_error)
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + body.get("expires_in", 7200)
        return self._token

    def search(self, query: str, *, max_price: float | None, brand: str = "",
               limit: int = 50) -> list[Listing]:
        params = {"q": query, "limit": str(limit), "sort": "newlyListed",
                  "filter": "buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER}"}
        if max_price:
            # Fetch somewhat above budget too, so the item page shows what the market looks like
            params["filter"] += f",price:[..{max_price * 1.5:.0f}],priceCurrency:{self.currency}"
        resp = httpx.get(
            SEARCH_URL,
            params=params,
            headers={"Authorization": f"Bearer {self._access_token()}",
                     "X-EBAY-C-MARKETPLACE-ID": self.marketplace},
            timeout=30,
        )
        if resp.status_code != 200:
            self._last_error = f"eBay search failed ({resp.status_code}): {resp.text[:200]}"
            raise RuntimeError(self._last_error)
        self._last_error = ""
        return parse_results(resp.json())
