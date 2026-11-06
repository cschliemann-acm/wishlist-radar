"""Track whether each moving part works, and say so when one keeps failing."""

from __future__ import annotations

import logging
import time
from typing import Callable

from .db import DB

log = logging.getLogger("wishlist_radar")
ALERT_AFTER = 3  # consecutive failures before a push notification


class Health:
    def __init__(self, db: DB, alert: Callable[[str, str], None] | None = None):
        self.db = db
        self.alert = alert or (lambda title, message: None)

    def ok(self, name: str) -> None:
        self.db.run("INSERT INTO health (name, last_ok_at, failures, alerted) VALUES (?, ?, 0, 0) "
                    "ON CONFLICT(name) DO UPDATE SET last_ok_at = excluded.last_ok_at, "
                    "failures = 0, alerted = 0", name, time.time())

    def fail(self, name: str, error: str) -> None:
        error = str(error)[:300]
        self.db.run("INSERT INTO health (name, last_error, last_error_at, failures) VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(name) DO UPDATE SET last_error = excluded.last_error, "
                    "last_error_at = excluded.last_error_at, failures = failures + 1",
                    name, error, time.time())
        row = self.db.one("SELECT * FROM health WHERE name = ?", name)
        if row and row["failures"] >= ALERT_AFTER and not row["alerted"]:
            self.db.run("UPDATE health SET alerted = 1 WHERE name = ?", name)
            try:
                self.alert(f"⚠️ {name} keeps failing",
                           f"Failed {row['failures']} times in a row. Last error: {error}")
            except Exception:
                log.exception("health alert failed")

    def rows(self) -> list[dict]:
        return self.db.all("SELECT * FROM health ORDER BY failures DESC, name COLLATE NOCASE")

    def problems(self) -> list[dict]:
        return self.db.all("SELECT * FROM health WHERE failures > 0 ORDER BY failures DESC")
