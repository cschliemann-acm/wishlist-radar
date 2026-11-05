"""Fill a database with realistic fake items, listings and checks, to try the interface
without any API keys:

    DATA_DIR=./demo-data python scripts/demo_data.py
    APP_PASSWORD=demo INGEST_TOKEN=demo DATA_DIR=./demo-data SCHEDULER=off \\
        uvicorn wishlist_radar.web:app --port 8000
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wishlist_radar.db import DB  # noqa: E402

data_dir = Path(os.getenv("DATA_DIR", "./demo-data"))
data_dir.mkdir(parents=True, exist_ok=True)
(data_dir / "images").mkdir(exist_ok=True)
db_path = data_dir / "wishlist.db"
if db_path.exists():
    db_path.unlink()
db = DB(db_path)
db.save_settings({"size_tops": "M / 48", "size_bottoms": "32", "size_shoes": "US 10 / UK 9 / EU 44",
                  "default_sources": "ebay,grailed", "ntfy_topic": "caspar-deals-8f3k2"})
now = time.time()
random.seed(4)


PALETTE = ["#2b2b2b", "#c8b89a", "#8a3b2e", "#d9d4c7", "#4a5a3c", "#5b4a7a", "#7a5232", "#e6e1d6"]


def img(seed: str, w: int = 600, h: int = 450) -> str:
    """Local placeholder image (a soft gradient), served from /images/."""
    import subprocess
    name = f"demo_{seed}.jpg"
    path = data_dir / "images" / name
    if not path.exists():
        a, b = random.sample(PALETTE, 2)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"gradients=s={w}x{h}:c0={a}:c1={b}:nb_colors=2:speed=0.00001", "-frames:v", "1",
                        str(path)], check=True)
    return f"/images/{name}"


items = [
    dict(status="active", name="Box Shirt", brand="Our Legacy", category="tops", color="Black silk",
         description="Boxy fit, short sleeve, black silk", search_query="Our Legacy box shirt black silk",
         max_price=180, sources="ebay,grailed", interval_minutes=60),
    dict(status="active", name="Air Max 1 '86 OG Big Bubble", brand="Nike", category="shoes",
         color="White / University Red", description="2023 release, big bubble",
         search_query="Nike Air Max 1 86 OG big bubble red", max_price=140, sources="ebay",
         interval_minutes=30),
    dict(status="active", name="Theta AR Jacket", brand="Arc'teryx", category="outerwear",
         color="Purple", description="Late 90s / early 2000s Gore-Tex shell",
         search_query="vintage Arcteryx Theta AR purple", max_price=None, sources="ebay,grailed",
         interval_minutes=180),
    dict(status="paused", name="Paraboot Michael", brand="Paraboot", category="shoes",
         color="Marron lisse", description="Norwegian welt derby", search_query="Paraboot Michael marron",
         max_price=250, sources="ebay", interval_minutes=720),
]
listing_titles = {
    0: [("Our Legacy Box Shirt Short Sleeve Black Silk Size 48", 145, "48", "match", 0.94, "yes", 1),
        ("OUR LEGACY box shirt silk black M", 210, "M", "match", 0.9, "yes", 0),
        ("Our Legacy Box Shirt Black 50", 120, "50", "match", 0.88, "no", 0),
        ("Our Legacy initial shirt black", 95, "48", "maybe", 0.45, "yes", 0)],
    1: [("Nike Air Max 1 '86 OG Big Bubble Sport Red US 10 DS", 132, "US 10", "match", 0.97, "yes", 1),
        ("Air Max 1 86 Big Bubble Red sz 10 worn twice", 118, "US 10", "match", 0.93, "yes", 1),
        ("Nike Air Max 1 86 OG Big Bubble Red 11.5", 125, "US 11.5", "match", 0.95, "no", 0)],
    2: [("Vintage 90s Arc'teryx Theta AR Gore-Tex jacket purple L", 340, "L", "match", 0.86, "unknown", 0)],
}
for idx, it in enumerate(items):
    last = now - random.randint(4, 50) * 60
    item_id = db.create_item(**it, image_path=img(f"item{idx}").removeprefix("/images/"),
                             created_at=now - 86400 * (6 - idx), last_checked_at=last,
                             next_check_at=last + it["interval_minutes"] * 60,
                             reason="Tagged in caption", confidence=0.9)
    for j, (title, price, size, verdict, score, size_ok, deal) in enumerate(listing_titles.get(idx, [])):
        db.add_listing(item_id, "grailed" if j % 2 and "grailed" in it["sources"] else "ebay",
                       listing_id=f"{item_id}-{j}", title=title, price=price, currency="USD",
                       size_text=size, condition="Used - Excellent" if j else "New",
                       url="https://www.ebay.com/", image_url=img(f"l{item_id}{j}", 300, 300))
        db.run("UPDATE listings SET verdict=?, score=?, size_ok=?, is_deal=?, reason=?, "
               "first_seen_at=? WHERE item_id=? AND listing_id=?", verdict, score, size_ok, deal,
               "Same model and colour" if verdict == "match" else "Different model",
               now - j * 5400 - 600, item_id, f"{item_id}-{j}")
    for k in range(8):
        for src in it["sources"].split(","):
            err = "Grailed search failed (403): forbidden" if (src == "grailed" and k == 2) else ""
            db.run("INSERT INTO checks (item_id, source, query, trigger, started_at, duration_ms, "
                   "results, new_listings, matches, deals, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   item_id, src, it["search_query"], "manual" if k == 3 else "schedule",
                   last - k * it["interval_minutes"] * 60, random.randint(600, 2400),
                   0 if err else random.randint(20, 50), 0 if err else random.randint(0, 6),
                   1 if k == 0 and idx < 2 else 0, 1 if k == 0 and idx < 2 else 0, err)

db.create_item(status="draft", name="Suede Work Jacket", brand="Margaret Howell", category="outerwear",
               color="Tobacco suede", description="Boxy chore jacket, four pockets",
               search_query="Margaret Howell suede work jacket", sources="ebay,grailed",
               interval_minutes=60, confidence=0.72, reason="Worn in frames 3–6, brand tagged in caption",
               source_url="https://www.instagram.com/reel/EXAMPLE/",
               image_path=img("draft").removeprefix("/images/"))
db.run("INSERT INTO posts (url, kind, summary, item_count, created_at) VALUES (?,?,?,?,?)",
       "https://www.instagram.com/reel/EXAMPLE/", "instagram",
       "Autumn outfit with a suede chore jacket and pleated trousers", 1, now - 3600)
for name, url, kind, status in [
    ("No Man Walks Alone", "https://www.nomanwalksalone.com", "shopify", "Shopify store · prices in USD"),
    ("Haven", "https://havenshop.com", "shopify", "Shopify store · prices in USD"),
    ("Story mfg.", "https://storymfg.com", "shopify", "Shopify store · prices in USD"),
    ("Heddels", "https://www.heddels.com", "feed", "News/deals site · reading its RSS feed"),
    ("Clutch Cafe", "https://clutch-cafe.com", "shopify", "Shopify store · prices in GBP"),
    ("SSENSE", "https://www.ssense.com", "unsupported", "The site blocks automated access (403)"),
    ("Union Los Angeles", "https://unionlosangeles.com", "pending", "Checking…"),
]:
    db.run("INSERT INTO stores (name, url, kind, status, currency, last_ok_at, created_at) VALUES (?,?,?,?,?,?,?)",
           name, url, kind, status, "USD", now - 1500 if kind in ("shopify", "feed") else None, now)
db.save_settings({"size_brands": "Our Legacy: 48\nNike: US 10.5", "default_sources": "ebay,stores"})
db.run("UPDATE items SET sources = sources || ',stores' WHERE status != 'draft'")
db.add_listing(1, "stores", listing_id="nmwa:box-shirt", title="Our Legacy · Box Shirt Black Silk",
               price=156, currency="USD", size_text="In stock: 48, 50", condition="On sale (was 260)",
               url="https://www.nomanwalksalone.com/products/box-shirt", image_url=img("shop1", 300, 300),
               merchant="No Man Walks Alone")
db.run("UPDATE listings SET verdict='match', score=0.97, size_ok='yes', is_deal=1, reason='Same shirt, 48 in stock', "
       "first_seen_at=? WHERE listing_id='nmwa:box-shirt'", now - 300)
print(f"Demo data written to {db_path}")
