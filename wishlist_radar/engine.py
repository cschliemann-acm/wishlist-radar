"""The part that does the work: importing posts, running searches, judging listings, alerting."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import shutil
import tempfile
import threading
import time
from pathlib import Path

import anthropic

from . import ai, fx, instagram, media, notify, prices, sales
from .config import Config
from .db import DB
from .health import Health
from .sources import ListingUpdate, Source, SourceStatus
from .sources.ebay import MARKETPLACES, EbaySource
from .sources.grailed import GrailedSource
from .sources.shops import ShopsSource, detect, host_key, normalize_url
from .sources.yahoojp import YahooJapanSource

log = logging.getLogger("wishlist_radar")

JUDGE_BATCH = 25
TICK_SECONDS = 30
FRAME_INTERVAL = 2.0   # seconds between regular frames (plus one per scene change)
MAX_FRAMES = 24
INTERVAL_CHOICES = [15, 30, 60, 180, 360, 720, 1440]
RECHECK_HOURS = 6          # how often matched listings are re-checked for price/stock changes
RECHECK_PER_CHECK = 30
FIT_CHECKS_PER_CHECK = 10
PRICE_DROP_MIN = 0.03      # ignore changes under 3%
BACKUP_KEEP = 7
SOURCE_NAMES = {"ebay": "eBay", "stores": "Shops", "grailed": "Grailed", "yahoojp": "Yahoo! Japan"}
SIZE_SETTING_BY_CATEGORY = {"tops": "size_tops", "outerwear": "size_outerwear",
                            "bottoms": "size_bottoms", "shoes": "size_shoes"}


def brand_sizes(text: str) -> dict[str, str]:
    """Parse "Brand: size" lines from the Sizes page."""
    out = {}
    for line in (text or "").splitlines():
        if ":" in line:
            brand, size = line.split(":", 1)
            if brand.strip() and size.strip():
                out[brand.strip().lower()] = size.strip()
    return out


class Engine:
    def __init__(self, config: Config, db: DB, client: anthropic.Anthropic | None = None,
                 sources: dict[str, Source] | None = None):
        self.c = config
        self.db = db
        self.client = client or anthropic.Anthropic()
        self._sources = sources
        self._ebay: EbaySource | None = None
        self._grailed = GrailedSource()
        self._shops = ShopsSource(lambda: self.db.stores(), report=lambda sid, f: self.db.update_store(sid, **f))
        self._yahoojp = YahooJapanSource()
        self._stop = threading.Event()
        self.health = Health(db, alert=self._push)

    # ------------------------------------------------------------------ sources
    def sources(self) -> dict[str, Source]:
        if self._sources is not None:
            return self._sources
        marketplace = self.db.settings()["ebay_marketplace"]
        if self._ebay is None or self._ebay.marketplace != marketplace:
            self._ebay = EbaySource(self.c.ebay_client_id, self.c.ebay_client_secret, marketplace)
        return {"ebay": self._ebay, "stores": self._shops, "grailed": self._grailed,
                "yahoojp": self._yahoojp}

    def source_statuses(self) -> dict[str, SourceStatus]:
        return {k: s.status() for k, s in self.sources().items()}

    def currency(self) -> str:
        return MARKETPLACES.get(self.db.settings()["ebay_marketplace"], ("", "USD"))[1]

    def wanted_size(self, item: dict, settings: dict | None = None) -> str:
        """Item's own size, else your size for that brand, else your size for the category."""
        if item.get("size"):
            return item["size"]
        settings = settings or self.db.settings()
        brand = (item.get("brand") or "").strip().lower()
        if brand:
            for b, size in brand_sizes(settings.get("size_brands", "")).items():
                if b == brand or b in brand or brand in b:
                    return size
        category = item.get("category", "")
        if category == "outerwear":
            return settings.get("size_outerwear") or settings.get("size_tops", "")
        if category == "bottoms":
            waist, inseam = settings.get("size_bottoms", ""), settings.get("size_inseam", "")
            return f"W{waist} L{inseam}" if waist and inseam else waist
        key = SIZE_SETTING_BY_CATEGORY.get(category)
        return settings.get(key, "") if key else ""

    def has_sizes(self) -> bool:
        s = self.db.settings()
        return any(s.get(k) for k in ("size_tops", "size_outerwear", "size_bottoms", "size_shoes",
                                      "size_brands"))

    # ------------------------------------------------------------------ shops
    def add_stores(self, text: str) -> int:
        """Lines of "url" or "Name | url". Detection runs in the background."""
        added = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            name, _, url = line.rpartition("|") if "|" in line else ("", "", line)
            url = url.strip()
            if " " in url or "." not in url:
                continue
            url = normalize_url(url)
            name = name.strip() or url.split("//", 1)[1].removeprefix("www.").split(".")[0].title()
            if any(host_key(s["url"]) == host_key(url) for s in self.db.stores()):
                continue
            self.db.run("INSERT INTO stores (name, url, kind, status, created_at) VALUES (?,?,?,?,?)",
                        name, url, "pending", "Checking…", time.time())
            added += 1
        if added:
            self.detect_stores_async(only_pending=True)
        return added

    def detect_stores_async(self, only_pending: bool = False) -> None:
        def run():
            for s in self.db.stores():
                if only_pending and s["kind"] != "pending":
                    continue
                try:
                    fields = detect(s["url"])
                except Exception as e:
                    fields = {"kind": "unsupported", "status": f"Check failed: {e}"[:200]}
                self.db.update_store(s["id"], **fields)
        if not only_pending:
            self.db.run("UPDATE stores SET kind = 'pending', status = 'Checking…'")
        threading.Thread(target=run, daemon=True, name="detect-stores").start()

    # ------------------------------------------------------------------ importing
    def import_dir(self, post_id: int) -> Path:
        return self.c.imports_dir / str(post_id)

    def _new_post(self, url: str, kind: str, note: str) -> int:
        post_id = self.db.run("INSERT INTO posts (url, kind, note, created_at) VALUES (?,?,?,?)",
                              url, kind, note, time.time())
        self.import_dir(post_id).mkdir(parents=True, exist_ok=True)
        return post_id

    def _save_info(self, post_id: int, **info) -> None:
        (self.import_dir(post_id) / "info.json").write_text(json.dumps(info, indent=2))

    def load_info(self, post_id: int) -> dict:
        path = self.import_dir(post_id) / "info.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def import_frames(self, post_id: int) -> list[Path]:
        d = self.import_dir(post_id)
        return sorted(p for p in d.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png")) \
            if d.exists() else []

    def import_instagram(self, url: str, note: str = "") -> dict:
        post_id = self._new_post(url, "instagram", note)
        out_dir = self.import_dir(post_id)
        with tempfile.TemporaryDirectory(dir=self.c.data_dir) as tmp:
            tmpdir = Path(tmp)
            try:
                post = instagram.download_post(url, tmpdir, self.c.instagram_cookies_file)
            except instagram.DownloadError as e:
                self.db.run("UPDATE posts SET error = ? WHERE id = ?", str(e), post_id)
                raise
            frames: list[Path] = []
            transcript, warnings = "", []
            if post.video_path:
                if not post.duration:
                    post.duration = media.probe_duration(post.video_path)
                frames = media.extract_smart_frames(post.video_path, out_dir, post.duration,
                                                    FRAME_INTERVAL, MAX_FRAMES)
                transcript, warning = self._transcribe(post.video_path, tmpdir)
                if warning:
                    warnings.append(warning)
            self._save_info(post_id, caption=post.caption, uploader=post.uploader,
                            transcript=transcript, duration=post.duration, warnings=warnings)
        found = ai.identify(self.client, self.c.identify_model, caption=post.caption, note=note,
                            uploader=post.uploader, images=frames, transcript=transcript)
        return self._save_found(found, frames, url, post_id)

    def import_link(self, url: str, note: str = "") -> dict:
        """Instagram posts, or any product page (shop, Grailed, eBay, brand site…)."""
        if re.search(r"(^|//|\.)instagram\.com/", url):
            return self.import_instagram(url, note)
        from . import product_page
        post_id = self._new_post(url, "link", note)
        try:
            page = product_page.fetch(url, self.import_dir(post_id))
        except Exception as e:
            self.db.run("UPDATE posts SET error = ? WHERE id = ?", f"Couldn't open the link: {e}"[:300], post_id)
            raise RuntimeError(f"Couldn't open that link: {e}") from e
        self._save_info(post_id, caption=page.text[:4000], uploader=page.site, transcript="",
                        warnings=[], structured=page.structured)
        images = [page.image] if page.image else []
        found = ai.identify(self.client, self.c.identify_model,
                            caption=f"Product page: {url}\n{page.structured}\n\n{page.text}",
                            uploader=page.site, images=images,
                            note=(note + "\n" if note else "") + "This is a product page for one item: "
                            "return just that product (exact name, colourway and brand from the page).")
        return self._save_found(found, images, url, post_id)

    def _transcribe(self, video: Path, tmpdir: Path) -> tuple[str, str]:
        """(transcript, warning). Best-effort: a failure shouldn't stop the import."""
        if self.c.transcribe == "off":
            return "", ""
        try:
            if not media.has_audio(video):
                return "", ""
            from .transcribe import transcribe
            audio = media.extract_audio(video, tmpdir / "speech.mp3", 0, 300)
            return transcribe(audio, self.c.transcribe, self.c.transcribe_api_key), ""
        except Exception as e:
            log.warning("transcription failed: %s", e)
            return "", f"Speech-to-text failed: {e}"[:300]

    def import_images(self, images: list[Path], note: str = "") -> dict:
        post_id = self._new_post("", "screenshot", note)
        kept = []
        for i, p in enumerate(images):
            dest = self.import_dir(post_id) / f"upload_{i:02d}{p.suffix or '.jpg'}"
            shutil.copy(p, dest)
            kept.append(dest)
        self._save_info(post_id, caption="", uploader="", transcript="", warnings=[])
        found = ai.identify(self.client, self.c.identify_model, note=note, images=kept)
        return self._save_found(found, kept, "", post_id)

    def import_text(self, text: str) -> dict:
        post_id = self._new_post("", "text", text)
        self._save_info(post_id, caption="", uploader="", transcript="", warnings=[])
        found = ai.identify(self.client, self.c.identify_model, note=text)
        return self._save_found(found, [], "", post_id)

    def rescan(self, post_id: int, note: str) -> dict:
        """Look at a post again for things the first pass missed (no re-download)."""
        post = self.db.one("SELECT * FROM posts WHERE id = ?", post_id)
        if not post:
            raise KeyError(post_id)
        info = self.load_info(post_id)
        frames = self.import_frames(post_id)
        already = [f"{i['brand']} {i['name']}".strip()
                   for i in self.db.all("SELECT brand, name FROM items WHERE post_id = ?", post_id)]
        found = ai.identify(self.client, self.c.identify_model, caption=info.get("caption", ""),
                            uploader=info.get("uploader", ""), transcript=info.get("transcript", ""),
                            images=frames, note=note or post.get("note", ""), already_found=already)
        return self._save_found(found, frames, post["url"], post_id)

    def _save_found(self, found: ai.Identification, images: list[Path], url: str,
                    post_id: int) -> dict:
        settings = self.db.settings()
        ids = []
        for f in found.items:
            item_id = self.db.create_item(
                status="draft", name=f.name, brand=f.brand, category=f.category, color=f.color,
                description=f.description, search_query=f.search_query,
                sources=settings["default_sources"],
                interval_minutes=int(settings["default_interval_minutes"]),
                source_url=url, confidence=f.confidence, reason=f.reason, post_id=post_id,
            )
            if 0 <= f.frame_index < len(images):
                dest = self.c.images_dir / f"item_{item_id}{images[f.frame_index].suffix or '.jpg'}"
                shutil.copy(images[f.frame_index], dest)
                self.db.update_item(item_id, image_path=dest.name)
            ids.append(item_id)
        self.db.run("UPDATE posts SET summary = CASE WHEN summary = '' THEN ? ELSE summary END, "
                    "item_count = item_count + ? WHERE id = ?", found.summary, len(ids), post_id)
        names = [f"{f.brand} {f.name}".strip() for f in found.items]
        message = (f"Added to inbox: {'; '.join(names)}" if names
                   else "Couldn't spot any (new) products in that post")
        return {"summary": found.summary, "item_ids": ids, "items": names, "message": message,
                "post_id": post_id}

    # ------------------------------------------------------------------ notifications
    def _push(self, title: str, message: str, url: str = "", image_url: str = "") -> bool:
        """Send a push notification. Returns False if not configured or it failed."""
        topic = self.db.settings().get("ntfy_topic")
        if not topic:
            return False
        try:
            notify.send(topic, title=title, message=message, url=url, image_url=image_url)
            self.health.ok("Notifications")
            return True
        except Exception as e:
            log.warning("notification failed: %s", e)
            self.health.db.run("INSERT INTO health (name, last_error, last_error_at, failures) "
                               "VALUES ('Notifications', ?, ?, 1) ON CONFLICT(name) DO UPDATE SET "
                               "last_error = excluded.last_error, last_error_at = excluded.last_error_at, "
                               "failures = failures + 1", str(e)[:300], time.time())
            return False

    def _listing_alert(self, item: dict, listing: dict, headline: str = "") -> None:
        price = f"{listing['price']:.0f} {listing['currency']}" if listing["price"] is not None else ""
        name = f"{item['brand']} {item['name']}".strip()
        where = listing.get("merchant") or SOURCE_NAMES.get(listing["source"], listing["source"])
        if self._push(title=(f"{headline}: " if headline else "") + name + (f" · {price}" if price else ""),
                      message=f"{listing['title']}\n{where}"
                              + (f" · size {listing['size_text']}" if listing["size_text"] else ""),
                      url=listing["url"], image_url=listing["image_url"]):
            self.db.run("UPDATE listings SET notified_at = ? WHERE id = ?", time.time(), listing["id"])

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
            self.recheck_listings(item, settings, force=trigger == "manual")
        finally:
            now = time.time()
            self.db.update_item(item_id, checking=0, last_checked_at=now,
                                next_check_at=now + item["interval_minutes"] * 60)

    def _query_for(self, item: dict, source: Source) -> str:
        if source.key != "yahoojp":
            return item["search_query"]
        if not item.get("search_query_jp"):
            jp = ai.japanese_query(self.client, self.c.match_model, item)
            self.db.update_item(item["id"], search_query_jp=jp)
            item["search_query_jp"] = jp
        return item["search_query_jp"]

    def _check_source(self, item: dict, source: Source, settings: dict, trigger: str) -> None:
        started = time.time()
        check = {"results": 0, "new_listings": 0, "matches": 0, "deals": 0, "error": ""}
        name = SOURCE_NAMES.get(source.key, source.key)
        query = item["search_query"]
        try:
            query = self._query_for(item, source)
            results = source.search(query, max_price=item["max_price"], brand=item.get("brand") or "")
            self.health.ok(name)
            check["results"] = len(results)
            known = self.db.known_listing_ids(item["id"], source.key)
            for l in results:
                if l.listing_id in known:
                    continue
                if hasattr(source, "enrich"):
                    l = source.enrich(l)
                self.db.add_listing(item["id"], source.key, listing_id=l.listing_id, title=l.title,
                                    price=l.price, currency=l.currency, size_text=l.size_text,
                                    condition=l.condition, url=l.url, image_url=l.image_url,
                                    listed_at=l.listed_at, merchant=l.merchant)
                check["new_listings"] += 1
            if getattr(source, "partial_errors", None):
                check["error"] = f"{len(source.partial_errors)} failed: " + "; ".join(
                    source.partial_errors[:3])
            matches, deals = self._judge_pending(item, source, settings)
            check["matches"], check["deals"] = matches, deals
        except anthropic.APIError as e:
            log.exception("Claude failed while checking item %s", item["id"])
            self.health.fail("Claude", e)
            check["error"] = f"Claude: {e}"[:500]
        except Exception as e:  # recorded on the check, shown in the UI
            log.exception("check failed: item %s on %s", item["id"], source.key)
            self.health.fail(name, e)
            check["error"] = str(e)[:500]
        self.db.run(
            "INSERT INTO checks (item_id, source, query, trigger, started_at, duration_ms, results,"
            " new_listings, matches, deals, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            item["id"], source.key, query, trigger, started,
            int((time.time() - started) * 1000), check["results"], check["new_listings"],
            check["matches"], check["deals"], check["error"])

    def feedback_text(self, item_id: int) -> str:
        rows = self.db.all("SELECT title, feedback FROM listings WHERE item_id = ? AND feedback != '' "
                           "ORDER BY id DESC LIMIT 16", item_id)
        yes = [r["title"] for r in rows if r["feedback"] == "up"][:8]
        no = [r["title"] for r in rows if r["feedback"] == "down"][:8]
        parts = []
        if yes:
            parts.append("Confirmed as the item:\n" + "\n".join(f"- {t}" for t in yes))
        if no:
            parts.append("Rejected as not what they want:\n" + "\n".join(f"- {t}" for t in no))
        return "\n".join(parts)

    def measurements(self, item: dict, settings: dict) -> str:
        unit = settings.get("meas_unit", "in")
        fields = (("meas_waist", "waist (flat)"), ("meas_inseam", "inseam")) if item.get("category") == "bottoms" \
            else (("meas_chest", "pit to pit"), ("meas_length", "length"), ("meas_shoulder", "shoulders"),
                  ("meas_sleeve", "sleeve"))
        if item.get("category") not in ("tops", "outerwear", "bottoms"):
            return ""
        vals = [f"{label}: {settings.get(k)} {unit}" for k, label in fields if settings.get(k)]
        return "; ".join(vals)

    def evaluate(self, item: dict, l: dict, settings: dict | None = None) -> bool:
        """Is this listing a deal right now? The one place the rules live."""
        settings = settings or self.db.settings()
        if l["verdict"] != "match" or l.get("dismissed") or l.get("feedback") == "down":
            return False
        if l.get("availability") == "sold" or l.get("size_ok") == "no" or l.get("fit") == "bad":
            return False
        if l.get("feedback") != "up" and (l.get("score") or 0) < float(settings["min_match_score"]):
            return False
        if item["max_price"] is None or l["price"] is None:
            return True  # no budget, or an article about the item
        price = prices.to_currency(l["price"], l["currency"], self.currency(), fetch=True)
        return price is not None and price <= item["max_price"]

    def _judge_pending(self, item: dict, source: Source, settings: dict) -> tuple[int, int]:
        pending = self.db.all("SELECT * FROM listings WHERE item_id = ? AND source = ? AND "
                              "verdict = 'pending' ORDER BY id", item["id"], source.key)
        wanted_size = self.wanted_size(item, settings)
        min_score = float(settings["min_match_score"])
        feedback = self.feedback_text(item["id"])
        measurements = self.measurements(item, settings)
        fit_budget = FIT_CHECKS_PER_CHECK
        matches = deals = 0
        for start in range(0, len(pending), JUDGE_BATCH):
            batch = pending[start:start + JUDGE_BATCH]
            for v in ai.judge(self.client, self.c.match_model, item, wanted_size, batch,
                              fit_notes=settings.get("size_notes", ""), feedback=feedback):
                l = dict(batch[v.index])
                is_match = v.verdict == "match" and v.score >= min_score
                l.update(verdict=v.verdict if v.verdict != "match" or is_match else "maybe",
                         score=v.score, size_ok=v.size_ok, reason=v.reason)
                # Resale listings: compare the seller's measurements with yours
                if is_match and measurements and fit_budget and hasattr(source, "details") \
                        and source.key != "stores":
                    fit_budget -= 1
                    try:
                        details = source.details(l)
                        if details:
                            fc = ai.fit_check(self.client, self.c.match_model, measurements, item, l, details)
                            l.update(fit=fc.fit, fit_note=fc.note)
                    except Exception as e:
                        log.warning("fit check failed: %s", e)
                l["is_deal"] = int(self.evaluate(item, l, settings))
                self.db.run("UPDATE listings SET verdict=?, score=?, size_ok=?, reason=?, is_deal=?, "
                            "fit=?, fit_note=? WHERE id=?", l["verdict"], l["score"], l["size_ok"],
                            l["reason"], l["is_deal"], l.get("fit", ""), l.get("fit_note", ""), l["id"])
                matches += is_match
                if l["is_deal"]:
                    deals += 1
                    self._listing_alert(item, l, "Deal")
        if pending:
            self.health.ok("Claude")
        return matches, deals

    # ------------------------------------------------------------------ price & stock tracking
    def recheck_listings(self, item: dict, settings: dict | None = None, force: bool = False) -> int:
        """Re-check matched listings: price drops, back in stock in your size, sold."""
        settings = settings or self.db.settings()
        sources = self.sources()
        cutoff = time.time() - (0 if force else RECHECK_HOURS * 3600)
        rows = self.db.all("SELECT * FROM listings WHERE item_id = ? AND verdict = 'match' AND dismissed = 0 "
                           "AND availability != 'sold' AND COALESCE(last_checked_at, 0) <= ? "
                           "ORDER BY last_checked_at LIMIT ?", item["id"], cutoff, RECHECK_PER_CHECK)
        wanted = self.wanted_size(item, settings)
        changed = 0
        for l in rows:
            source = sources.get(l["source"])
            if not source or not hasattr(source, "refresh"):
                continue
            try:
                update = source.refresh(l)
            except Exception as e:
                log.warning("re-check failed for listing %s: %s", l["id"], e)
                self.db.run("UPDATE listings SET last_checked_at = ? WHERE id = ?", time.time(), l["id"])
                continue
            changed += self._apply_update(item, l, update, wanted, settings)
        return changed

    def _event(self, item: dict, l: dict, kind: str, old: str = "", new: str = "") -> int:
        return self.db.run("INSERT INTO events (item_id, listing_id, kind, old_value, new_value, created_at) "
                           "VALUES (?,?,?,?,?,?)", item["id"], l["id"], kind, old, new, time.time())

    def _apply_update(self, item: dict, l: dict, update: ListingUpdate | None, wanted: str,
                      settings: dict) -> int:
        now = time.time()
        if update is None:
            self.db.run("UPDATE listings SET last_checked_at = ? WHERE id = ?", now, l["id"])
            return 0
        was_deal = bool(l["is_deal"])
        new = dict(l)
        headline = ""
        events = 0
        if not update.available:
            new["availability"] = "sold"
            self._event(item, l, "sold")
            events += 1
        else:
            if update.price is not None and l["price"] is not None:
                if update.price < l["price"] * (1 - PRICE_DROP_MIN):
                    new.update(price=update.price, prev_price=l["price"])
                    eid = self._event(item, l, "price_drop", f"{l['price']:.2f}", f"{update.price:.2f}")
                    events += 1
                    headline = f"Price drop ({l['price']:.0f} → {update.price:.0f} {l['currency']})"
                    self.db.run("UPDATE events SET notified = 1 WHERE id = ?", eid)
                elif update.price > l["price"] * (1 + PRICE_DROP_MIN):
                    new["price"] = update.price
                    self._event(item, l, "price_up", f"{l['price']:.2f}", f"{update.price:.2f}")
            if update.size_text is not None and update.size_text != l["size_text"]:
                new["size_text"] = update.size_text
                before = prices.size_in_stock(wanted, l["size_text"])
                after = prices.size_in_stock(wanted, update.size_text)
                if after is True and before is not True:
                    new["size_ok"] = "yes"
                    eid = self._event(item, l, "restock", l["size_text"] or "", update.size_text)
                    events += 1
                    headline = headline or "Back in your size"
                    self.db.run("UPDATE events SET notified = 1 WHERE id = ?", eid)
                elif after is False and before is True:
                    new["size_ok"] = "no"
                    self._event(item, l, "size_gone", l["size_text"] or "", update.size_text)
                    events += 1
        new["is_deal"] = int(self.evaluate(item, new, settings))
        self.db.run("UPDATE listings SET price = ?, prev_price = ?, size_text = ?, size_ok = ?, availability = ?, "
                    "is_deal = ?, last_checked_at = ? WHERE id = ?", new["price"], new.get("prev_price"),
                    new["size_text"], new["size_ok"], new["availability"], new["is_deal"], now, l["id"])
        if new["is_deal"] and (headline or not was_deal):
            self._listing_alert(item, new, headline or "Now a deal")
        elif headline and new["availability"] != "sold":
            self._listing_alert(item, new, headline)
        return events

    def check_now(self, item_id: int) -> None:
        threading.Thread(target=self.check_item, args=(item_id, "manual"), daemon=True).start()

    # ------------------------------------------------------------------ sample sales
    def ensure_sale_sources(self) -> None:
        for name, url, region in sales.DEFAULT_SOURCES:
            self.db.run("INSERT OR IGNORE INTO sale_sources (name, url, region) VALUES (?,?,?)",
                        name, url, region)

    def followed_brands(self) -> list[str]:
        """Brands to flag sales for: your wishlist, your brand sizes, and your follow list."""
        settings = self.db.settings()
        brands = {i["brand"].strip() for i in self.db.items(("active", "paused", "draft"))
                  if i["brand"].strip()}
        brands |= {b.title() for b in brand_sizes(settings.get("size_brands", ""))}
        brands |= {l.strip() for l in settings.get("sale_brands", "").splitlines() if l.strip()}
        return sorted(brands, key=str.lower)

    def refresh_sales(self, force: bool = False) -> None:
        self.ensure_sale_sources()
        today = time.strftime("%Y-%m-%d")
        for src in self.db.all("SELECT * FROM sale_sources WHERE enabled = 1"):
            fresh = src["last_fetched_at"] and time.time() - src["last_fetched_at"] < sales.REFRESH_HOURS * 3600
            if fresh and not force:
                continue
            try:
                text = sales.fetch_page_text(src["url"])
                found = ai.extract_sales(self.client, self.c.match_model, text, src["name"], today)
                for f in found:
                    self._upsert_sale(f, src)
                self.db.run("UPDATE sale_sources SET status = ?, last_fetched_at = ?, last_count = ? "
                            "WHERE id = ?", f"{len(found)} sales found", time.time(), len(found), src["id"])
                self.health.ok(f"Sales: {src['name']}")
            except Exception as e:
                log.warning("sale source %s failed: %s", src["name"], e)
                self.health.fail(f"Sales: {src['name']}", e)
                self.db.run("UPDATE sale_sources SET status = ?, last_fetched_at = ? WHERE id = ?",
                            f"Couldn't read: {e}"[:200], time.time(), src["id"])
        self.match_sales()

    def _upsert_sale(self, f: ai.FoundSale, src: dict) -> None:
        if not f.brand.strip():
            return
        key = sales.sale_key(f.brand, f.start_date)
        now = time.time()
        existing = self.db.one("SELECT * FROM sales WHERE key = ?", key)
        if existing:
            names = {n for n in existing["sources"].split(", ") if n} | {src["name"]}
            self.db.run("UPDATE sales SET last_seen_at = ?, sources = ?, "
                        "end_date = COALESCE(NULLIF(?, ''), end_date), "
                        "url = CASE WHEN url = '' THEN ? ELSE url END, "
                        "discount = CASE WHEN discount = '' THEN ? ELSE discount END WHERE id = ?",
                        now, ", ".join(sorted(names)), f.end_date, f.url, f.discount, existing["id"])
            return
        self.db.run("INSERT INTO sales (key, brand, title, start_date, end_date, location, online, "
                    "menswear, discount, details, url, sources, first_seen_at, last_seen_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    key, f.brand.strip(), f.title, f.start_date, f.end_date, f.location,
                    int(f.online), f.menswear, f.discount, f.details, f.url or src["url"],
                    src["name"], now, now)

    def match_sales(self) -> None:
        brands = self.followed_brands()
        settings = self.db.settings()
        for s in self.db.all("SELECT * FROM sales WHERE hidden = 0"):
            hits = sales.brand_matches(f"{s['brand']} {s['title']}", brands)
            matched = ", ".join(hits)
            if matched != s["matched_brands"]:
                self.db.run("UPDATE sales SET matched_brands = ? WHERE id = ?", matched, s["id"])
            if hits and not s["notified_at"] and sales.status_of(s) != "past":
                when = " – ".join(d for d in (s["start_date"], s["end_date"]) if d) or "dates TBA"
                where = f" · {s['location']}" if s["location"] else (" · online" if s["online"] else "")
                if self._push(f"Sample sale: {s['brand']}",
                              f"{s['title']}\n{when}{where}" + (f"\n{s['discount']}" if s["discount"] else ""),
                              url=s["url"]):
                    self.db.run("UPDATE sales SET notified_at = ? WHERE id = ?", time.time(), s["id"])

    def refresh_sales_async(self, force: bool = True) -> None:
        threading.Thread(target=self.refresh_sales, args=(force,), daemon=True,
                         name="sales").start()

    # ------------------------------------------------------------------ daily digest
    def digest_text(self, since: float) -> tuple[str, str] | None:
        """(title, message) summarising what happened since `since`, or None if nothing did."""
        maybes = self.db.one("SELECT COUNT(*) n FROM listings WHERE verdict = 'maybe' AND first_seen_at > ?",
                             since)["n"]
        matches = self.db.one("SELECT COUNT(*) n FROM listings WHERE verdict = 'match' AND is_deal = 0 "
                              "AND dismissed = 0 AND first_seen_at > ?", since)["n"]
        deals = self.db.one("SELECT COUNT(*) n FROM listings WHERE is_deal = 1 AND first_seen_at > ?", since)["n"]
        changes = self.db.all("SELECT kind, COUNT(*) n FROM events WHERE created_at > ? GROUP BY kind", since)
        new_sales = self.db.one("SELECT COUNT(*) n FROM sales WHERE first_seen_at > ? AND hidden = 0", since)["n"]
        problems = self.health.problems()
        lines = []
        if deals:
            lines.append(f"{deals} deal{'s' if deals != 1 else ''} (already sent)")
        if matches:
            lines.append(f"{matches} new match{'es' if matches != 1 else ''} over budget or not your size")
        if maybes:
            lines.append(f"{maybes} possible match{'es' if maybes != 1 else ''} to look at")
        labels = {"price_drop": "price drop", "sold": "sold", "restock": "back in your size",
                  "price_up": "price rise", "size_gone": "your size sold out"}
        for c in changes:
            lines.append(f"{c['n']} {labels.get(c['kind'], c['kind'])}{'s' if c['n'] != 1 and c['kind'] in ('price_drop', 'price_up') else ''}")
        if new_sales:
            lines.append(f"{new_sales} new sample sale{'s' if new_sales != 1 else ''}")
        if problems:
            lines.append("Needs attention: " + ", ".join(p["name"] for p in problems[:4]))
        if not lines:
            return None
        return "Your wishlist today", "\n".join(f"• {l}" for l in lines)

    def maybe_send_digest(self, now: float | None = None) -> bool:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        settings = self.db.settings()
        if not settings.get("digest_enabled") or not settings.get("ntfy_topic"):
            return False
        now = now or time.time()
        try:
            tz = ZoneInfo(settings.get("timezone") or "America/New_York")
        except Exception:
            tz = ZoneInfo("UTC")
        local = datetime.fromtimestamp(now, tz)
        last = settings.get("last_digest_at") or 0
        last_day = datetime.fromtimestamp(last, tz).date() if last else None
        if local.hour < int(settings.get("digest_hour", 9)) or last_day == local.date():
            return False
        self.db.save_settings({"last_digest_at": now})
        summary = self.digest_text(last or now - 86400)
        if not summary:
            return False
        return self._push(summary[0], summary[1], url=settings.get("app_url", ""))

    # ------------------------------------------------------------------ backups
    def backup(self) -> Path:
        """Copy the database to data/backups (keeps the last few days)."""
        folder = self.c.data_dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"wishlist-{time.strftime('%Y-%m-%d')}.db"
        src = sqlite3.connect(self.c.db_path)
        out = sqlite3.connect(dest)
        with out:
            src.backup(out)
        src.close()
        out.close()
        for old in sorted(folder.glob("wishlist-*.db"))[:-BACKUP_KEEP]:
            old.unlink(missing_ok=True)
        self.db.save_settings({"last_backup_at": time.time()})
        return dest

    def backups(self) -> list[Path]:
        folder = self.c.data_dir / "backups"
        return sorted(folder.glob("wishlist-*.db"), reverse=True) if folder.exists() else []

    # ------------------------------------------------------------------ self-test
    def self_test(self) -> None:
        """Try every moving part once and record the results on the Status page."""
        def attempt(name: str, fn) -> None:
            try:
                fn()
                self.health.ok(name)
            except Exception as e:
                self.health.fail(name, e)

        def claude():
            r = self.client.messages.create(model=self.c.match_model, max_tokens=200,
                                            messages=[{"role": "user", "content": "Reply with: ok"}],
                                            output_config={"effort": "low"})
            if r.stop_reason == "refusal":
                raise RuntimeError("declined")

        attempt("Claude", claude)
        sources = self.sources()
        if self.c.ebay_client_id:
            attempt("eBay", lambda: sources["ebay"].search("vintage jacket", max_price=None))
        else:
            self.health.fail("eBay", "No eBay keys yet: add EBAY_CLIENT_ID and EBAY_CLIENT_SECRET")
        in_use = " ".join(i["sources"] for i in self.db.items(("active", "paused")))
        if "grailed" in in_use:
            attempt("Grailed", lambda: sources["grailed"].search("jacket", max_price=None))
        if "yahoojp" in in_use:
            attempt("Yahoo! Japan", lambda: sources["yahoojp"].search("ジャケット", max_price=None))
        attempt("Exchange rates", lambda: fx.rates("USD") or (_ for _ in ()).throw(
            RuntimeError("couldn't fetch exchange rates")))
        if self.c.transcribe != "off":
            def stt():
                import httpx
                from .transcribe import PROVIDERS
                base = PROVIDERS[self.c.transcribe][0]
                r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {self.c.transcribe_api_key}"},
                              timeout=20)
                if r.status_code != 200:
                    raise RuntimeError(f"{self.c.transcribe} rejected the key ({r.status_code})")
            attempt("Speech-to-text", stt)
        self.refresh_sales(force=True)

    def self_test_async(self) -> None:
        threading.Thread(target=self.self_test, daemon=True, name="self-test").start()

    # ------------------------------------------------------------------ scheduler
    def start_scheduler(self) -> None:
        self.db.run("UPDATE items SET checking = 0 WHERE checking = 1")  # left over from a restart
        if any(s["kind"] == "pending" for s in self.db.stores()):
            self.detect_stores_async(only_pending=True)  # detection interrupted by a restart
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
                self.refresh_sales()  # no-op unless a source is due (every few hours)
                self.maybe_send_digest()
                if time.time() - (self.db.settings().get("last_backup_at") or 0) > 86400:
                    self.backup()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(TICK_SECONDS)
