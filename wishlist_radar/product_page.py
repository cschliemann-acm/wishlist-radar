"""Read a product page (any shop, Grailed, eBay, brand site) for importing an item from a link.

Uses what most shop pages publish for search engines: JSON-LD "Product" data and Open Graph
tags, plus Shopify's product JSON when it's a Shopify store, plus the page text.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .sales import html_to_text

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
}


@dataclass
class Page:
    url: str
    site: str
    structured: str      # "Name: …\nBrand: …\nPrice: …" from the page's product data
    text: str            # readable page text (trimmed)
    image: Path | None   # product photo, downloaded


def _meta(page: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]*>', page, re.I)
    if not m:
        return ""
    c = re.search(r'content=["\']([^"\']*)["\']', m.group(0), re.I)
    return html.unescape(c.group(1)).strip() if c else ""


def _products_from_jsonld(page: str) -> list[dict]:
    found = []
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                types = node.get("@type")
                types = types if isinstance(types, list) else [types]
                if "Product" in types or "ProductGroup" in types:
                    found.append(node)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                stack.extend(node)
    return found


def structured_summary(page: str, url: str) -> tuple[str, str]:
    """(summary text, image url)."""
    lines: dict[str, str] = {}
    image = ""
    products = _products_from_jsonld(page)
    if products:
        p = products[0]
        brand = p.get("brand")
        brand = brand.get("name", "") if isinstance(brand, dict) else (brand or "")
        offers = p.get("offers") or {}
        offers = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offers, dict) and offers.get("@type") == "AggregateOffer":
            price = offers.get("lowPrice") or offers.get("price")
        else:
            price = offers.get("price") if isinstance(offers, dict) else None
        currency = offers.get("priceCurrency", "") if isinstance(offers, dict) else ""
        lines.update({"Name": str(p.get("name") or ""), "Brand": str(brand),
                      "Price": f"{price} {currency}".strip() if price else "",
                      "Colour": str(p.get("color") or ""), "SKU": str(p.get("sku") or "")})
        img = p.get("image")
        img = img[0] if isinstance(img, list) and img else img
        image = img.get("url", "") if isinstance(img, dict) else (img or "")
    lines.setdefault("Name", "")
    if not lines["Name"]:
        lines["Name"] = _meta(page, "og:title")
    if not lines.get("Price"):
        amount = _meta(page, "product:price:amount") or _meta(page, "og:price:amount")
        if amount:
            lines["Price"] = f"{amount} {_meta(page, 'product:price:currency') or _meta(page, 'og:price:currency')}".strip()
    if not lines.get("Brand"):
        lines["Brand"] = _meta(page, "product:brand")
    image = image or _meta(page, "og:image")
    summary = "\n".join(f"{k}: {v}" for k, v in lines.items() if v)
    return summary, urljoin(url, image) if image else ""


def fetch(url: str, folder: Path) -> Page:
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        r = client.get(url)
        if r.status_code >= 400:
            raise RuntimeError(f"the page returned {r.status_code}")
        final = str(r.url)
        summary, image_url = structured_summary(r.text, final)

        # Shopify product: its JSON has the exact title, vendor and price
        if "/products/" in urlparse(final).path:
            try:
                pj = client.get(final.split("?")[0].rstrip("/") + ".js")
                if pj.status_code == 200:
                    prod = pj.json()
                    summary = (f"Name: {prod.get('title')}\nBrand: {prod.get('vendor')}\n"
                               f"Price: {(prod.get('price') or 0) / 100:.2f}\n" + summary)
                    img = prod.get("featured_image") or ""
                    image_url = image_url or ("https:" + img if img.startswith("//") else img)
            except Exception:
                pass

        image_path = None
        if image_url:
            try:
                ir = client.get(image_url)
                ctype = ir.headers.get("content-type", "")
                if ir.status_code == 200 and ctype.startswith("image/") and len(ir.content) < 8_000_000:
                    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
                           "image/jpeg": ".jpg", "image/jpg": ".jpg"}.get(ctype.split(";")[0].strip())
                    if ext:
                        image_path = folder / f"link_image{ext}"
                        image_path.write_bytes(ir.content)
            except Exception:
                pass

    site = urlparse(final).netloc.removeprefix("www.")
    return Page(url=final, site=site, structured=summary, text=html_to_text(r.text, final)[:12000],
                image=image_path)
