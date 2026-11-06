"""Your favourite shops and sites.

- Shopify stores (most independent menswear shops): searched with the store's own public
  product search (/search/suggest.json), then each new product's page data
  (/products/<handle>.js) tells us which sizes are in stock and whether it's on sale.
- News / deal sites (Heddels and similar): their RSS feed is read, and posts
  that mention the item are passed on to be judged.
- Anything else is marked unsupported.

Requests are sequential and only new products are looked up, to keep the load on each
shop tiny.
"""

from __future__ import annotations

import html
import re
import time
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx

from . import Listing, ListingUpdate, SourceStatus

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Accept": "application/json, text/html, application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}
RESULTS_PER_SHOP = 8

# Offered on the Shops page for one-click adding. (name, address, group)
SUGGESTED_SHOPS = [
    # From your bookmarks
    ("No Man Walks Alone", "nomanwalksalone.com", "yours"),
    ("Yuketen", "yuketen.com", "yours"),
    ("Story mfg.", "storymfg.com", "yours"),
    ("18 East", "18east.co", "yours"),
    ("mfpen", "mfpen.com", "yours"),
    ("Thunders Love", "thunderslove.com", "yours"),
    ("Notre", "notre-shop.com", "yours"),
    ("Union Los Angeles", "store.unionlosangeles.com", "yours"),
    ("Haven", "havenshop.com", "yours"),
    ("Neighbour", "neighbourshop.com", "yours"),
    ("Lost & Found", "shoplostfound.com", "yours"),
    ("Corlection", "corlection.com", "yours"),
    ("Cueva", "cuevashop.com", "yours"),
    ("Canoe Club", "shopcanoeclub.com", "yours"),
    ("MoMA Design Store", "store.moma.org", "yours"),
    ("Très Bien", "tresbienshop.com", "yours"),
    ("BSTN", "bstn.com", "yours"),
    ("SSENSE", "ssense.com", "yours"),
    ("Huckberry", "huckberry.com", "yours"),
    ("Heddels", "heddels.com", "yours"),
    # Similar shops worth adding
    ("C'H'C'M'", "chcmshop.com", "more"),
    ("Stag Provisions", "stagprovisions.com", "more"),
    ("Clutch Cafe", "clutch-cafe.com", "more"),
    ("Goodhood", "goodhoodstore.com", "more"),
    ("Oi Polloi", "oipolloi.com", "more"),
    ("Nubian", "nubiantokyo.com", "more"),
    # Sample-sale and archive-sale shops: wishlist items get searched there too
    ("260 Sample Sale (online)", "260samplesale.com", "sales"),
    ("Unionmade (archive sale)", "unionmadegoods.com", "sales"),
]
FEED_MAX_AGE_DAYS = 14
_STOPWORDS = {"the", "and", "with", "for", "in", "of", "a", "an", "og", "new", "men", "mens"}


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower()}"


def host_key(url: str) -> str:
    """Same shop whether or not the address has www."""
    return urlparse(normalize_url(url)).netloc.removeprefix("www.")


def _client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True)


# --------------------------------------------------------------------- detection
def detect(url: str, client: httpx.Client | None = None) -> dict:
    """Work out what kind of site this is. Returns fields for the stores table."""
    client = client or _client()
    base = normalize_url(url)
    try:
        r = client.get(f"{base}/products.json", params={"limit": 1})
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json") \
                and "products" in r.json():
            currency = ""
            try:
                currency = client.get(f"{base}/cart.js").json().get("currency", "")
            except Exception:
                pass
            return {"kind": "shopify", "currency": currency, "feed_url": "",
                    "status": f"Shopify store{f' · prices in {currency}' if currency else ''}"}
    except Exception:
        pass

    try:
        r = client.get(base)
    except Exception as e:
        return {"kind": "unsupported", "status": f"Couldn't reach the site ({type(e).__name__})"}
    if r.status_code in (401, 403, 429) or "cf-chl" in r.text[:5000]:
        return {"kind": "unsupported", "status": f"The site blocks automated access ({r.status_code})"}
    if r.status_code >= 400:
        return {"kind": "unsupported", "status": f"Site returned {r.status_code}. Check the address"}
    feed = find_feed_url(r.text, str(r.url))
    if feed:
        return {"kind": "feed", "feed_url": feed, "status": "News/deals site · reading its RSS feed"}
    return {"kind": "unsupported",
            "status": "Not a Shopify store and no RSS feed found, so it can't be searched"}


def find_feed_url(page_html: str, page_url: str) -> str:
    for m in re.finditer(r"<link[^>]+>", page_html, re.I):
        tag = m.group(0)
        if re.search(r'type=["\']application/(rss|atom)\+xml["\']', tag, re.I):
            href = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
            if href:
                return urljoin(page_url, html.unescape(href.group(1)))
    return ""


# --------------------------------------------------------------------- parsing
def _price(v) -> float | None:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


def parse_suggest(body: dict, base: str, store: dict) -> list[Listing]:
    products = ((body.get("resources") or {}).get("results") or {}).get("products") or []
    out = []
    for p in products:
        handle = p.get("handle")
        if not handle:
            continue
        image = p.get("image") or p.get("featured_image") or ""
        if isinstance(image, dict):
            image = image.get("url") or image.get("src") or ""
        if image.startswith("//"):
            image = "https:" + image
        price = _price(p.get("price") or p.get("price_min"))
        was = _price(p.get("compare_at_price_max") or p.get("compare_at_price"))
        out.append(Listing(
            listing_id=f"{urlparse(base).netloc}:{handle}",
            title=f"{p.get('vendor')} · {p.get('title')}" if p.get("vendor") and
            p.get("vendor", "").lower() not in p.get("title", "").lower() else p.get("title", ""),
            price=price,
            currency=store.get("currency", ""),
            url=f"{base}/products/{handle}",
            image_url=image,
            condition=(f"On sale (was {was:.0f})" if was and price and was > price else "New")
            + ("" if p.get("available", True) else " · sold out"),
        ))
    return out


def apply_product_js(listing: Listing, product: dict) -> Listing:
    """Fill in sizes in stock and the real price from /products/<handle>.js."""
    variants = product.get("variants") or []
    sizes = [v.get("title") or v.get("option1") for v in variants if v.get("available")]
    if variants and len(variants) > 1:
        listing.size_text = ("In stock: " + ", ".join(s for s in sizes if s)) if sizes else "Sold out"
    elif variants and not variants[0].get("available"):
        listing.size_text = "Sold out"
    cents = product.get("price")
    was = product.get("compare_at_price")
    if isinstance(cents, int) and cents > 0:
        listing.price = cents / 100
        if isinstance(was, int) and was > cents:
            listing.condition = f"On sale (was {was / 100:.0f})"
        elif listing.condition.startswith("On sale"):
            listing.condition = "New"
    return listing


def _text(el, tag: str, ns: dict | None = None) -> str:
    found = el.find(tag, ns or {})
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(xml_text: str, store: dict) -> list[Listing]:
    try:
        root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    atom = {"a": "http://www.w3.org/2005/Atom"}
    media = "{http://search.yahoo.com/mrss/}"
    entries = root.findall(".//item") or root.findall(".//a:entry", atom)
    out = []
    cutoff = time.time() - FEED_MAX_AGE_DAYS * 86400
    for e in entries[:60]:
        title = html.unescape(_text(e, "title") or _text(e, "a:title", atom))
        link = _text(e, "link")
        if not link:
            le = e.find("a:link", atom)
            link = le.get("href", "") if le is not None else ""
        desc = _text(e, "description") or _text(e, "a:summary", atom) or _text(e, "a:content", atom)
        published = None
        date = _text(e, "pubDate") or _text(e, "a:published", atom) or _text(e, "a:updated", atom)
        if date:
            try:
                published = parsedate_to_datetime(date).timestamp()
            except (TypeError, ValueError):
                from datetime import datetime
                try:
                    published = datetime.fromisoformat(date.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    published = None
        if published and published < cutoff:
            continue
        image = ""
        for tag in (f"{media}content", f"{media}thumbnail", "enclosure"):
            m = e.find(tag)
            if m is not None and m.get("url"):
                image = m.get("url")
                break
        if not image:
            img = re.search(r'<img[^>]+src=["\']([^"\']+)', html.unescape(desc))
            image = img.group(1) if img else ""
        summary = re.sub(r"<[^>]+>", " ", html.unescape(desc))
        summary = re.sub(r"\s+", " ", summary).strip()[:300]
        out.append(Listing(listing_id=f"{urlparse(link).netloc}:{urlparse(link).path}",
                           title=title + (f" — {summary}" if summary else ""),
                           price=None, currency="", url=link, image_url=image,
                           condition="Article", listed_at=published))
    return out


def mentions(listing: Listing, query: str, brand: str = "") -> bool:
    """Cheap pre-filter for feed posts: does the post plausibly mention the item?"""
    text = listing.title.lower()
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 2 and w not in _STOPWORDS]
    hits = sum(1 for w in words if w in text)
    if brand and brand.lower() in text:
        hits += 1
    return hits >= min(2, max(1, len(words)))


# --------------------------------------------------------------------- the source
class ShopsSource:
    key = "stores"
    label = "Shops"
    experimental = False

    def __init__(self, get_stores: Callable[[], list[dict]],
                 report: Callable[[int, dict], None] | None = None):
        self.get_stores = get_stores
        self.report = report or (lambda store_id, fields: None)
        self.partial_errors: list[str] = []
        self._feed_cache: dict[str, tuple[float, str]] = {}

    def status(self) -> SourceStatus:
        stores = self.get_stores()
        usable = [s for s in stores if s["enabled"] and s["kind"] in ("shopify", "feed")]
        if not stores:
            return SourceStatus(False, "No shops added yet. Add some on the Shops page")
        return SourceStatus(bool(usable), f"{len(usable)} of {len(stores)} shops searchable")

    def search(self, query: str, *, max_price: float | None, limit: int = RESULTS_PER_SHOP,
               brand: str = "") -> list[Listing]:
        self.partial_errors = []
        stores = [s for s in self.get_stores() if s["enabled"] and s["kind"] in ("shopify", "feed")]
        if not stores:
            raise RuntimeError("No searchable shops. Add some on the Shops page")
        out: list[Listing] = []
        with _client() as client:
            for s in stores:
                try:
                    found = (self._search_shopify(client, s, query, limit) if s["kind"] == "shopify"
                             else [l for l in self._read_feed(client, s) if mentions(l, query, brand)])
                    for l in found:
                        l.merchant = s["name"]
                    out += found
                    self.report(s["id"], {"last_ok_at": time.time()})
                except Exception as e:
                    msg = f"{s['name']}: {e}"[:160]
                    self.partial_errors.append(msg)
                    self.report(s["id"], {"status": f"Last search failed: {e}"[:200]})
        if len(self.partial_errors) == len(stores):
            raise RuntimeError("All shops failed. " + "; ".join(self.partial_errors[:3]))
        return out

    def _search_shopify(self, client: httpx.Client, store: dict, query: str, limit: int) -> list[Listing]:
        base = store["url"]
        r = client.get(f"{base}/search/suggest.json", params={
            "q": query, "resources[type]": "product", "resources[limit]": str(limit),
            "resources[options][unavailable_products]": "last"})
        if r.status_code != 200:
            raise RuntimeError(f"search returned {r.status_code}")
        return parse_suggest(r.json(), base, store)

    def _read_feed(self, client: httpx.Client, store: dict) -> list[Listing]:
        # One feed download serves every wishlist item for 30 minutes
        cached = self._feed_cache.get(store["feed_url"])
        if cached and time.time() - cached[0] < 1800:
            body = cached[1]
        else:
            r = client.get(store["feed_url"])
            if r.status_code != 200:
                raise RuntimeError(f"feed returned {r.status_code}")
            body = r.text
            self._feed_cache[store["feed_url"]] = (time.time(), body)
        return parse_feed(body, store)

    def _product_js(self, url: str) -> dict | None:
        r = httpx.get(url + ".js", headers=HEADERS, timeout=20, follow_redirects=True)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise RuntimeError(f"product page returned {r.status_code}")
        return r.json()

    def refresh(self, listing: dict) -> ListingUpdate | None:
        if listing.get("condition") == "Article" or "/products/" not in listing["url"]:
            return None
        product = self._product_js(listing["url"])
        if product is None:
            return ListingUpdate(available=False)
        tmp = apply_product_js(Listing(listing_id="", title="", price=listing["price"],
                                       currency="", url=listing["url"]), product)
        any_available = any(v.get("available") for v in product.get("variants") or [])
        return ListingUpdate(available=any_available, price=tmp.price, size_text=tmp.size_text)

    def details(self, listing: dict) -> str:
        if "/products/" not in listing["url"]:
            return ""
        product = self._product_js(listing["url"]) or {}
        text = re.sub(r"<[^>]+>", " ", html.unescape(product.get("description") or ""))
        return re.sub(r"\s+", " ", text).strip()[:6000]

    def enrich(self, listing: Listing) -> Listing:
        """For new Shopify products: sizes in stock and accurate price."""
        if listing.condition == "Article" or "/products/" not in listing.url:
            return listing
        try:
            r = httpx.get(listing.url + ".js", headers=HEADERS, timeout=20, follow_redirects=True)
            if r.status_code == 200:
                return apply_product_js(listing, r.json())
        except Exception:
            pass
        return listing
