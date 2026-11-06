"""Price history and size-availability helpers."""

from __future__ import annotations

import re
from statistics import median

from . import fx


def to_currency(price: float | None, frm: str, to: str, fetch: bool = False) -> float | None:
    if price is None:
        return None
    if not frm or frm == to:
        return price
    return fx.convert(price, frm, to, fetch=fetch)


def stats(listings: list[dict], currency: str, fetch: bool = False) -> dict:
    """Typical / low / high price of matched listings, in your currency."""
    prices = [p for p in (to_currency(l["price"], l["currency"], currency, fetch) for l in listings)
              if p is not None and p > 0]
    if not prices:
        return {"n": 0, "median": None, "low": None, "high": None}
    return {"n": len(prices), "median": median(prices), "low": min(prices), "high": max(prices)}


def below_typical(price: float | None, typical: float | None, n: int) -> int | None:
    """Whole percent below the typical price, if meaningful (3+ data points, 10%+ below)."""
    if price is None or not typical or n < 3:
        return None
    pct = round((typical - price) / typical * 100)
    return pct if pct >= 10 else None


def suggested_budget(typical: float | None, n: int) -> int | None:
    """A budget that catches good-but-real deals: ~15% under typical, rounded to $5."""
    if not typical or n < 3:
        return None
    return int(round(typical * 0.85 / 5) * 5)


def _size_tokens(wanted: str) -> list[str]:
    parts = [p.strip().lower() for p in re.split(r"[/,]| or ", wanted) if p.strip()]
    out = []
    for p in parts:
        out.append(p)
        bare = re.sub(r"^(us|uk|eu|it|jp|fr)\s*", "", p)
        if bare != p:
            out.append(bare)
    return out


def size_in_stock(wanted: str, size_text: str | None) -> bool | None:
    """Is your size in a shop's "In stock: S, M, L" list? None if it can't be told."""
    if not wanted or not size_text:
        return None
    if size_text.startswith("Sold out"):
        return False
    if not size_text.startswith("In stock:"):
        return None
    stock = [s.strip().lower() for s in size_text.split(":", 1)[1].split(",") if s.strip()]
    for want in _size_tokens(wanted):
        for s in stock:
            if s == want or re.search(rf"(?<![\w.]){re.escape(want)}(?![\w.])", s):
                return True
    return False
