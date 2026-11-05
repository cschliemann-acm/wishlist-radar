"""SQLite storage. One short-lived connection per call keeps it thread-safe for the
web server and the background checker."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'draft',          -- draft | active | paused | archived
    name TEXT NOT NULL,
    brand TEXT DEFAULT '',
    category TEXT DEFAULT 'other',
    color TEXT DEFAULT '',
    description TEXT DEFAULT '',
    search_query TEXT NOT NULL,
    size TEXT DEFAULT '',                          -- blank = use default size for the category
    max_price REAL,                                -- blank = no budget, just track
    sources TEXT NOT NULL DEFAULT 'ebay',          -- comma-separated
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    image_path TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    confidence REAL,
    reason TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_checked_at REAL,
    next_check_at REAL,
    checking INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price REAL,
    currency TEXT DEFAULT '',
    size_text TEXT DEFAULT '',
    condition TEXT DEFAULT '',
    url TEXT DEFAULT '',
    image_url TEXT DEFAULT '',
    listed_at REAL,
    first_seen_at REAL NOT NULL,
    verdict TEXT NOT NULL DEFAULT 'pending',       -- pending | match | maybe | no
    score REAL,
    size_ok TEXT DEFAULT 'unknown',                -- yes | no | unknown
    reason TEXT DEFAULT '',
    is_deal INTEGER NOT NULL DEFAULT 0,
    notified_at REAL,
    dismissed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(item_id, source, listing_id)
);
CREATE INDEX IF NOT EXISTS listings_item ON listings(item_id, verdict);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    query TEXT DEFAULT '',
    trigger TEXT DEFAULT 'schedule',               -- schedule | manual
    started_at REAL NOT NULL,
    duration_ms INTEGER,
    results INTEGER DEFAULT 0,
    new_listings INTEGER DEFAULT 0,
    matches INTEGER DEFAULT 0,
    deals INTEGER DEFAULT 0,
    error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS checks_item ON checks(item_id, started_at);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT DEFAULT '',
    kind TEXT DEFAULT 'instagram',                 -- instagram | screenshot | text
    summary TEXT DEFAULT '',
    item_count INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,                      -- homepage, e.g. https://www.nomanwalksalone.com
    kind TEXT NOT NULL DEFAULT 'pending',          -- pending | shopify | feed | unsupported
    feed_url TEXT DEFAULT '',
    currency TEXT DEFAULT '',
    status TEXT DEFAULT '',                        -- last detection / search message
    enabled INTEGER NOT NULL DEFAULT 1,
    last_ok_at REAL,
    created_at REAL NOT NULL
);
"""

# Columns added after the first release (added to existing databases on startup)
MIGRATIONS = [
    "ALTER TABLE listings ADD COLUMN merchant TEXT DEFAULT ''",
]

DEFAULT_SETTINGS: dict[str, Any] = {
    "size_tops": "",
    "size_outerwear": "",
    "size_bottoms": "",
    "size_inseam": "",
    "size_shoes": "",
    "size_brands": "",          # one per line: "Our Legacy: 48"
    "size_notes": "",           # free text: fit preferences

    "ebay_marketplace": "EBAY_US",
    "default_interval_minutes": 60,
    "default_sources": "ebay",
    "ntfy_topic": "",
    "min_match_score": 0.7,
}

# Fields a user may edit on an item
ITEM_FIELDS = ("status", "name", "brand", "category", "color", "description", "search_query",
               "size", "max_price", "sources", "interval_minutes", "notes")


class DB:
    def __init__(self, path: Path):
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")
            for sql in MIGRATIONS:
                try:
                    c.execute(sql)
                except sqlite3.OperationalError:
                    pass  # already applied

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def all(self, sql: str, *args) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def one(self, sql: str, *args) -> dict | None:
        with self.conn() as c:
            r = c.execute(sql, args).fetchone()
            return dict(r) if r else None

    def run(self, sql: str, *args) -> int:
        with self.conn() as c:
            return c.execute(sql, args).lastrowid

    # --- settings ---------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        out = dict(DEFAULT_SETTINGS)
        for r in self.all("SELECT key, value FROM settings"):
            out[r["key"]] = json.loads(r["value"])
        return out

    def save_settings(self, values: dict[str, Any]) -> None:
        with self.conn() as c:
            for k, v in values.items():
                if k in DEFAULT_SETTINGS:
                    c.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                              "SET value = excluded.value", (k, json.dumps(v)))

    # --- items ------------------------------------------------------------
    def create_item(self, **fields) -> int:
        now = time.time()
        fields = {k: v for k, v in fields.items() if v is not None}
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self.run(f"INSERT INTO items ({cols}) VALUES ({marks})", *fields.values())

    def update_item(self, item_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE items SET {sets} WHERE id = ?", *fields.values(), item_id)

    def item(self, item_id: int) -> dict | None:
        return self.one("SELECT * FROM items WHERE id = ?", item_id)

    def items(self, statuses: tuple[str, ...]) -> list[dict]:
        marks = ", ".join("?" for _ in statuses)
        return self.all(f"""
            SELECT i.*,
              (SELECT COUNT(*) FROM listings l WHERE l.item_id = i.id AND l.verdict = 'match'
                 AND l.dismissed = 0) AS match_count,
              (SELECT COUNT(*) FROM listings l WHERE l.item_id = i.id AND l.is_deal = 1
                 AND l.dismissed = 0) AS deal_count,
              (SELECT MIN(price) FROM listings l WHERE l.item_id = i.id AND l.verdict = 'match'
                 AND l.dismissed = 0) AS best_price,
              (SELECT currency FROM listings l WHERE l.item_id = i.id AND l.verdict = 'match'
                 AND l.dismissed = 0 ORDER BY price LIMIT 1) AS best_currency,
              (SELECT image_url FROM listings l WHERE l.item_id = i.id AND l.verdict = 'match'
                 ORDER BY score DESC LIMIT 1) AS fallback_image
            FROM items i WHERE i.status IN ({marks})
            ORDER BY CASE i.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                     i.created_at DESC""", *statuses)

    def due_items(self, now: float) -> list[dict]:
        return self.all("SELECT * FROM items WHERE status = 'active' AND checking = 0 "
                        "AND (next_check_at IS NULL OR next_check_at <= ?) "
                        "ORDER BY next_check_at", now)

    # --- listings ---------------------------------------------------------
    def known_listing_ids(self, item_id: int, source: str) -> set[str]:
        rows = self.all("SELECT listing_id FROM listings WHERE item_id = ? AND source = ?",
                        item_id, source)
        return {r["listing_id"] for r in rows}

    def add_listing(self, item_id: int, source: str, **f) -> int:
        f.update(item_id=item_id, source=source, first_seen_at=time.time())
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        return self.run(f"INSERT OR IGNORE INTO listings ({cols}) VALUES ({marks})", *f.values())

    def item_listings(self, item_id: int, verdicts: tuple[str, ...], limit: int = 200) -> list[dict]:
        marks = ", ".join("?" for _ in verdicts)
        return self.all(f"SELECT * FROM listings WHERE item_id = ? AND verdict IN ({marks}) "
                        f"AND dismissed = 0 ORDER BY is_deal DESC, price ASC LIMIT ?",
                        item_id, *verdicts, limit)

    # --- stores -----------------------------------------------------------
    def stores(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled = 1" if enabled_only else ""
        return self.all(f"SELECT * FROM stores {where} ORDER BY name COLLATE NOCASE")

    def update_store(self, store_id: int, **fields) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE stores SET {sets} WHERE id = ?", *fields.values(), store_id)

    # --- checks -----------------------------------------------------------
    def checks(self, item_id: int | None = None, limit: int = 50) -> list[dict]:
        if item_id is None:
            return self.all("SELECT c.*, i.name AS item_name FROM checks c JOIN items i "
                            "ON i.id = c.item_id ORDER BY c.started_at DESC LIMIT ?", limit)
        return self.all("SELECT * FROM checks WHERE item_id = ? ORDER BY started_at DESC LIMIT ?",
                        item_id, limit)

    def last_check_by_source(self, item_id: int) -> dict[str, dict]:
        rows = self.all("""SELECT c.* FROM checks c JOIN (
                             SELECT source, MAX(started_at) m FROM checks WHERE item_id = ?
                             GROUP BY source) x ON x.source = c.source AND x.m = c.started_at
                           WHERE c.item_id = ?""", item_id, item_id)
        return {r["source"]: r for r in rows}
