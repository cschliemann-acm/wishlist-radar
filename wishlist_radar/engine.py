"""The part that does the work: importing posts, running searches, judging listings, alerting."""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path

import anthropic

from . import ai, instagram, media, notify
from .config import Config
from .db import DB
from .sources import Source, SourceStatus
from .sources.ebay import MARKETPLACES, EbaySource
from .sources.grailed import GrailedSource

log = logging.getLogger("wishlist_radar")

JUDGE_BATCH = 25
TICK_SECONDS = 30
INTERVAL_CHOICES = [15, 30, 60, 180, 360, 720, 1440]
SIZE_SETTING_BY_CATEGORY = {"tops": "size_tops", "outerwear": "size_tops",
                            "bottoms": "size_bottoms", "shoes": "size_shoes"}


class Engine:
    def __init__(self, config: Config, db: DB, client: anthropic.Anthropic | None = None,
                 sources: dict[str, Source] | None = None):
        self.c = config
        self.db = db
        self.client = client or anthropic.Anthropic()
        self._sources = sources
        self._ebay: EbaySource | None = None
        self._grailed = GrailedSource()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ sources
    def sources(self) -> dict[str, Source]:
        if self._sources is not None:
            return self._sources
        marketplace = self.db.settings()["ebay_marketplace"]
        if self._ebay is None or self._ebay.marketplace != marketplace:
            self._ebay = EbaySource(self.c.ebay_client_id, self.c.ebay_client_secret, marketplace)
        return {"ebay": self._ebay, "grailed": self._grailed}

    def source_statuses(self) -> dict[str, SourceStatus]:
        return {k: s.status() for k, s in self.sources().items()}

    def currency(self) -> str:
        return MARKETPLACES.get(self.db.settings()["ebay_marketplace"], ("", "USD"))[1]

    def wanted_size(self, item: dict, settings: dict | None = None) -> str:
        if item.get("size"):
            return item["size"]
        settings = settings or self.db.settings()
        key = SIZE_SETTING_BY_CATEGORY.get(item.get("category", ""))
        return settings.get(key, "") if key else ""

    # ------------------------------------------------------------------ importing
    def import_instagram(self, url: str, note: str = "") -> dict:
        with tempfile.TemporaryDirectory(dir=self.c.data_dir) as tmp:
            tmpdir = Path(tmp)
            try:
                post = instagram.download_post(url, tmpdir, self.c.instagram_cookies_file)
            except instagram.DownloadError as e:
                self.db.run("INSERT INTO posts (url, kind, error, created_at) VALUES (?,?,?,?)",
                            url, "instagram", str(e), time.time())
                raise
            frames: list[Path] = []
            if post.video_path:
                if not post.duration:
                    post.duration = media.probe_duration(post.video_path)
                frames = media.extract_smart_frames(post.video_path, tmpdir, post.duration, 2.5, 20)
            found = ai.identify(self.client, self.c.identify_model, caption=post.caption,
                                note=note, uploader=post.uploader, images=frames)
            return self._save_found(found, frames, url, "instagram")

    def import_images(self, images: list[Path], note: str = "") -> dict:
        found = ai.identify(self.client, self.c.identify_model, note=note, images=images)
        return self._save_found(found, images, "", "screenshot")

    def import_text(self, text: str) -> dict:
        found = ai.identify(self.client, self.c.identify_model, note=text)
        return self._save_found(found, [], "", "text")

    def _save_found(self, found: ai.Identification, images: list[Path], url: str, kind: str) -> dict:
        settings = self.db.settings()
        ids = []
        for f in found.items:
            item_id = self.db.create_item(
                status="draft", name=f.name, brand=f.brand, category=f.category, color=f.color,
                description=f.description, search_query=f.search_query,
                sources=settings["default_sources"],
                interval_minutes=int(settings["default_interval_minutes"]),
                source_url=url, confidence=f.confidence, reason=f.reason,
            )
            if 0 <= f.frame_index < len(images):
                dest = self.c.images_dir / f"item_{item_id}{images[f.frame_index].suffix or '.jpg'}"
                shutil.copy(images[f.frame_index], dest)
                self.db.update_item(item_id, image_path=dest.name)
            ids.append(item_id)
        self.db.run("INSERT INTO posts (url, kind, summary, item_count, created_at) VALUES (?,?,?,?,?)",
                    url, kind, found.summary, len(ids), time.time())
        names = [f"{f.brand} {f.name}".strip() for f in found.items]
        message = (f"Added to inbox: {'; '.join(names)}" if names
                   else "Couldn't spot any products in that post")
        return {"summary": found.summary, "item_ids": ids, "items": names, "message": message}

    # ------------------------------------------------------------------ checking
    def check_item(self, item_id: int, trigger: str = "schedule") -> None:
        item = self.db.item(item_id)
        if not item:
            return
        self.db.update_item(item_id, checking=1)
        try:
            settings = self.db.settings()
            sources = self.sources()
            for key in [s for s in item["sources"].split(",") if s]:
                if key in sources:
                    self._check_source(item, sources[key], settings, trigger)
        finally:
            now = time.time()
            self.db.update_item(item_id, checking=0, last_checked_at=now,
                                next_check_at=now + item["interval_minutes"] * 60)

    def _check_source(self, item: dict, source: Source, settings: dict, trigger: str) -> None:
        started = time.time()
        check = {"results": 0, "new_listings": 0, "matches": 0, "deals": 0, "error": ""}
        try:
            results = source.search(item["search_query"], max_price=item["max_price"])
            check["results"] = len(results)
            known = self.db.known_listing_ids(item["id"], source.key)
            for l in results:
                if l.listing_id in known:
                    continue
                self.db.add_listing(item["id"], source.key, listing_id=l.listing_id, title=l.title,
                                    price=l.price, currency=l.currency, size_text=l.size_text,
                                    condition=l.condition, url=l.url, image_url=l.image_url,
                                    listed_at=l.listed_at)
                check["new_listings"] += 1
            matches, deals = self._judge_pending(item, source.key, settings)
            check["matches"], check["deals"] = matches, deals
        except Exception as e:  # recorded on the check, shown in the UI
            log.exception("check failed: item %s on %s", item["id"], source.key)
            check["error"] = str(e)[:500]
        self.db.run(
            "INSERT INTO checks (item_id, source, query, trigger, started_at, duration_ms, results,"
            " new_listings, matches, deals, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            item["id"], source.key, item["search_query"], trigger, started,
            int((time.time() - started) * 1000), check["results"], check["new_listings"],
            check["matches"], check["deals"], check["error"])

    def _judge_pending(self, item: dict, source_key: str, settings: dict) -> tuple[int, int]:
        pending = self.db.all("SELECT * FROM listings WHERE item_id = ? AND source = ? AND "
                              "verdict = 'pending' ORDER BY id", item["id"], source_key)
        wanted_size = self.wanted_size(item, settings)
        min_score = float(settings["min_match_score"])
        currency = self.currency()
        matches = deals = 0
        for start in range(0, len(pending), JUDGE_BATCH):
            batch = pending[start:start + JUDGE_BATCH]
            for v in ai.judge(self.client, self.c.match_model, item, wanted_size, batch):
                l = batch[v.index]
                is_match = v.verdict == "match" and v.score >= min_score
                in_budget = (item["max_price"] is None or
                             (l["price"] is not None and l["currency"] == currency
                              and l["price"] <= item["max_price"]))
                is_deal = is_match and v.size_ok != "no" and in_budget
                verdict = v.verdict if v.verdict != "match" or is_match else "maybe"
                self.db.run("UPDATE listings SET verdict=?, score=?, size_ok=?, reason=?, is_deal=? "
                            "WHERE id=?", verdict, v.score, v.size_ok, v.reason, int(is_deal), l["id"])
                matches += is_match
                if is_deal:
                    deals += 1
                    self._alert(item, l, settings)
        return matches, deals

    def _alert(self, item: dict, listing: dict, settings: dict) -> None:
        if not settings.get("ntfy_topic"):
            return
        price = f"{listing['price']:.0f} {listing['currency']}" if listing["price"] is not None else ""
        try:
            notify.send(settings["ntfy_topic"],
                        title=f"{item['brand']} {item['name']}".strip() + (f" · {price}" if price else ""),
                        message=f"{listing['title']}\n{listing['source'].title()}"
                                + (f" · size {listing['size_text']}" if listing["size_text"] else ""),
                        url=listing["url"], image_url=listing["image_url"])
            self.db.run("UPDATE listings SET notified_at = ? WHERE id = ?", time.time(), listing["id"])
        except Exception:
            log.exception("notification failed")

    def check_now(self, item_id: int) -> None:
        threading.Thread(target=self.check_item, args=(item_id, "manual"), daemon=True).start()

    # ------------------------------------------------------------------ scheduler
    def start_scheduler(self) -> None:
        self.db.run("UPDATE items SET checking = 0 WHERE checking = 1")  # left over from a restart
        threading.Thread(target=self._loop, daemon=True, name="scheduler").start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for item in self.db.due_items(time.time()):
                    if self._stop.is_set():
                        break
                    self.check_item(item["id"])
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(TICK_SECONDS)
