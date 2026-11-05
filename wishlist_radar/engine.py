"""The part that does the work: importing posts, running searches, judging listings, alerting."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path

import anthropic

from . import ai, fx, instagram, media, notify
from .config import Config
from .db import DB
from .sources import Source, SourceStatus
from .sources.ebay import MARKETPLACES, EbaySource
from .sources.grailed import GrailedSource
from .sources.shops import ShopsSource, detect, host_key, normalize_url

log = logging.getLogger("wishlist_radar")

JUDGE_BATCH = 25
TICK_SECONDS = 30
FRAME_INTERVAL = 2.0   # seconds between regular frames (plus one per scene change)
MAX_FRAMES = 24
INTERVAL_CHOICES = [15, 30, 60, 180, 360, 720, 1440]
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
        self._stop = threading.Event()

    # ------------------------------------------------------------------ sources
    def sources(self) -> dict[str, Source]:
        if self._sources is not None:
            return self._sources
        marketplace = self.db.settings()["ebay_marketplace"]
        if self._ebay is None or self._ebay.marketplace != marketplace:
            self._ebay = EbaySource(self.c.ebay_client_id, self.c.ebay_client_secret, marketplace)
        return {"ebay": self._ebay, "stores": self._shops, "grailed": self._grailed}

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
            results = source.search(item["search_query"], max_price=item["max_price"],
                                    brand=item.get("brand") or "")
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
            for v in ai.judge(self.client, self.c.match_model, item, wanted_size, batch,
                              fit_notes=settings.get("size_notes", "")):
                l = batch[v.index]
                is_match = v.verdict == "match" and v.score >= min_score
                # Articles (no price) count: a post about the item is worth knowing about
                price = (l["price"] if l["currency"] in (currency, "") else
                         fx.convert(l["price"], l["currency"], currency))
                in_budget = (item["max_price"] is None or l["price"] is None or
                             (price is not None and price <= item["max_price"]))
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
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(TICK_SECONDS)
