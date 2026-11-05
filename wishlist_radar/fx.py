"""Currency conversion, so shops pricing in GBP/EUR/JPY can be compared with your budget.

Rates come from open.er-api.com (free, no key, updated daily) and are cached for 12 hours.
If rates can't be fetched, listings in another currency simply can't count as deals.
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger("wishlist_radar")
URL = "https://open.er-api.com/v6/latest/{base}"
TTL = 12 * 3600

_rates: dict[str, tuple[float, dict[str, float]]] = {}
_lock = threading.Lock()


def _fetch(base: str) -> dict[str, float] | None:
    try:
        r = httpx.get(URL.format(base=base), timeout=15)
        body = r.json()
        if body.get("result") == "success":
            return {k: float(v) for k, v in body["rates"].items()}
    except Exception as e:
        log.warning("currency rates unavailable: %s", e)
    return None


def rates(base: str, fetch: bool = True) -> dict[str, float] | None:
    base = base.upper()
    with _lock:
        cached = _rates.get(base)
        if cached and time.time() - cached[0] < TTL:
            return cached[1]
    if not fetch:
        return cached[1] if cached else None
    fresh = _fetch(base)
    if fresh:
        with _lock:
            _rates[base] = (time.time(), fresh)
        return fresh
    return cached[1] if cached else None


def convert(amount: float | None, frm: str, to: str, fetch: bool = True) -> float | None:
    """amount in `frm` expressed in `to`, or None if unknown. `fetch=False` uses cache only."""
    if amount is None or not frm or not to:
        return None
    frm, to = frm.upper(), to.upper()
    if frm == to:
        return amount
    table = rates(to, fetch=fetch)  # rates relative to `to`: 1 to = r frm
    if not table or frm not in table or table[frm] == 0:
        return None
    return amount / table[frm]


def set_rates(base: str, table: dict[str, float]) -> None:
    """For tests."""
    with _lock:
        _rates[base.upper()] = (time.time(), table)
