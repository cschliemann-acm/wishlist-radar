"""Offline tests: marketplaces and Claude are faked."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from wishlist_radar import ai
from wishlist_radar.config import Config
from wishlist_radar.db import DB
from wishlist_radar.engine import Engine
from wishlist_radar.sources import Listing, SourceStatus
from wishlist_radar.sources.ebay import parse_results
from wishlist_radar.sources.grailed import find_algolia_credentials, parse_hits


class FakeSource:
    def __init__(self, key, listings, error=None):
        self.key, self.label, self.experimental = key, key.title(), False
        self.listings, self.error, self.calls = listings, error, 0

    def status(self):
        return SourceStatus(True, "fake")

    def search(self, query, *, max_price, brand="", limit=50):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.listings


def make_engine(tmp_path, sources):
    config = Config(app_password="pw", ingest_token="tok", secret_key="s", data_dir=tmp_path)
    config.images_dir.mkdir(parents=True, exist_ok=True)
    return Engine(config, DB(config.db_path), client=object(), sources=sources)


def listing(i, title, price, size=""):
    return Listing(listing_id=str(i), title=title, price=price, currency="USD",
                   url=f"https://x/{i}", size_text=size)


@pytest.fixture
def fake_judge(monkeypatch):
    """Judge: titles containing 'Box Shirt' match; size 50 is the wrong size."""
    calls = []

    def judge(client, model, item, wanted_size, listings, **kw):
        calls.append((wanted_size, [l["title"] for l in listings]))
        out = []
        for i, l in enumerate(listings):
            is_match = "box shirt" in l["title"].lower()
            out.append(ai.Verdict(index=i, verdict="match" if is_match else "no",
                                  score=0.95 if is_match else 0.1,
                                  size_ok="no" if l["size_text"] == "50" else "yes",
                                  reason="test"))
        return out

    monkeypatch.setattr(ai, "judge", judge)
    return calls


def test_check_finds_deals_and_skips_seen_listings(tmp_path, fake_judge):
    ebay = FakeSource("ebay", [listing(1, "Our Legacy Box Shirt black 48", 150, "48"),
                               listing(2, "Our Legacy Box Shirt black 50", 120, "50"),
                               listing(3, "Our Legacy Box Shirt black 48", 260, "48"),
                               listing(4, "Our Legacy cardigan", 90)])
    eng = make_engine(tmp_path, {"ebay": ebay})
    eng.db.save_settings({"size_tops": "48"})
    item_id = eng.db.create_item(status="active", name="Box Shirt", brand="Our Legacy",
                                 category="tops", search_query="our legacy box shirt",
                                 max_price=200, sources="ebay", interval_minutes=60)
    eng.check_item(item_id)

    rows = {l["listing_id"]: l for l in eng.db.all("SELECT * FROM listings")}
    assert rows["1"]["is_deal"] == 1                     # match, right size, in budget
    assert rows["2"]["is_deal"] == 0                     # wrong size
    assert rows["3"]["is_deal"] == 0                     # over budget
    assert rows["4"]["verdict"] == "no"
    assert fake_judge[0][0] == "48"                      # default size used for tops
    check = eng.db.checks(item_id)[0]
    assert (check["results"], check["new_listings"], check["matches"], check["deals"]) == (4, 4, 3, 1)

    item = eng.db.item(item_id)
    assert item["checking"] == 0 and item["next_check_at"] > time.time() + 3500

    # Second run: nothing new, Claude not called again
    eng.check_item(item_id)
    assert len(fake_judge) == 1
    assert eng.db.checks(item_id)[0]["new_listings"] == 0


def test_failed_source_is_recorded_and_others_still_run(tmp_path, fake_judge):
    ebay = FakeSource("ebay", [listing(1, "Box Shirt", 100)])
    grailed = FakeSource("grailed", [], error="Grailed search failed (403)")
    eng = make_engine(tmp_path, {"ebay": ebay, "grailed": grailed})
    item_id = eng.db.create_item(status="active", name="Box Shirt", search_query="box shirt",
                                 sources="ebay,grailed")
    eng.check_item(item_id)
    by_source = eng.db.last_check_by_source(item_id)
    assert by_source["grailed"]["error"].startswith("Grailed search failed")
    assert by_source["ebay"]["error"] == "" and by_source["ebay"]["matches"] == 1


def test_judge_failure_leaves_listings_pending_for_next_time(tmp_path, monkeypatch):
    ebay = FakeSource("ebay", [listing(1, "Box Shirt", 100)])
    eng = make_engine(tmp_path, {"ebay": ebay})
    item_id = eng.db.create_item(status="active", name="Box Shirt", search_query="box shirt",
                                 sources="ebay")

    def broken(*a, **k):
        raise RuntimeError("API overloaded")

    monkeypatch.setattr(ai, "judge", broken)
    eng.check_item(item_id)
    assert eng.db.one("SELECT verdict FROM listings")["verdict"] == "pending"
    assert "overloaded" in eng.db.checks(item_id)[0]["error"]

    monkeypatch.setattr(ai, "judge", lambda c, m, item, size, ls, **kw: [
        ai.Verdict(index=0, verdict="match", score=0.9, size_ok="unknown", reason="ok")])
    eng.check_item(item_id)
    assert eng.db.one("SELECT verdict, is_deal FROM listings") == {"verdict": "match", "is_deal": 1}


def test_scheduler_only_picks_due_active_items(tmp_path):
    eng = make_engine(tmp_path, {})
    now = time.time()
    due = eng.db.create_item(status="active", name="a", search_query="a", next_check_at=now - 1)
    eng.db.create_item(status="active", name="b", search_query="b", next_check_at=now + 600)
    eng.db.create_item(status="paused", name="c", search_query="c", next_check_at=now - 1)
    eng.db.create_item(status="draft", name="d", search_query="d")
    assert [i["id"] for i in eng.db.due_items(now)] == [due]


def test_import_saves_drafts_with_frame_image(tmp_path, monkeypatch):
    eng = make_engine(tmp_path, {})
    frame = tmp_path / "f0.jpg"
    frame.write_bytes(b"jpg")
    found = ai.Identification(summary="fit pic", items=[ai.FoundItem(
        name="Box Shirt", brand="Our Legacy", category="tops", color="black", description="silk",
        search_query="our legacy box shirt", frame_index=0, confidence=0.9, reason="tagged")])
    monkeypatch.setattr(ai, "identify", lambda *a, **k: found)
    result = eng.import_images([frame], note="the shirt")
    item = eng.db.item(result["item_ids"][0])
    assert item["status"] == "draft" and item["image_path"] == f"item_{item['id']}.jpg"
    assert (eng.c.images_dir / item["image_path"]).exists()
    assert "Our Legacy Box Shirt" in result["message"]


# ---------------------------------------------------------------- marketplace parsing
def test_ebay_parsing():
    body = {"itemSummaries": [{
        "itemId": "v1|123|0", "title": "Nike Air Max 1", "price": {"value": "120.00", "currency": "USD"},
        "itemWebUrl": "https://www.ebay.com/itm/123", "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
        "condition": "Pre-owned", "itemCreationDate": "2026-11-01T10:00:00.000Z"}]}
    [l] = parse_results(body)
    assert (l.listing_id, l.price, l.currency, l.condition) == ("v1|123|0", 120.0, "USD", "Pre-owned")
    assert l.listed_at and l.image_url.endswith("x.jpg")
    assert parse_results({}) == []


def test_grailed_parsing_and_key_discovery():
    body = {"hits": [{"id": 555, "title": "Box Shirt", "price": 140, "size": "m",
                      "designers": [{"name": "Our Legacy"}], "cover_photo": {"url": "https://p/x.jpg"},
                      "condition": "is_gently_used", "created_at": "2026-11-01T10:00:00Z"}]}
    [l] = parse_hits(body)
    assert l.title == "Our Legacy · Box Shirt" and l.url.endswith("/listings/555")
    assert (l.price, l.size_text, l.condition) == (140.0, "m", "gently used")

    html = '<script>window.CONFIG={"algolia":{"appId":"MNRWEFSS2Q","apiKey":"a3a4de2e05d9e9b463911705fb6323ad"}}</script>'
    assert find_algolia_credentials(html) == ("MNRWEFSS2Q", "a3a4de2e05d9e9b463911705fb6323ad")
    html2 = 'searchApiKey: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", applicationId: "ABCDEFGH12"'
    assert find_algolia_credentials(html2) == ("ABCDEFGH12", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    assert find_algolia_credentials("<html>nothing</html>") is None


# ---------------------------------------------------------------- web
@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER", "off")
    from wishlist_radar import web

    web.get_engine.cache_clear()
    with TestClient(web.app) as c:
        yield c, web.get_engine()
    web.get_engine.cache_clear()


def test_login_required_and_pages_render(client):
    c, eng = client
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    c.post("/login", data={"password": "pw"})
    item_id = eng.db.create_item(status="active", name="Box Shirt", search_query="q", sources="ebay")
    eng.db.create_item(status="draft", name="Jacket", search_query="j", category="outerwear")
    for path in ("/", f"/items/{item_id}", "/activity", "/settings", "/add", "/archived",
                 f"/items/{item_id}/rejected"):
        r = c.get(path)
        assert r.status_code == 200, path
    assert "Inbox" in c.get("/").text


def test_activate_draft_and_edit_item(client):
    c, eng = client
    c.post("/login", data={"password": "pw"})
    item_id = eng.db.create_item(status="draft", name="Jacket", search_query="j", sources="ebay")
    c.post(f"/items/{item_id}", data={"status": "active", "max_price": "$250", "size": "L",
                                      "interval_minutes": "180", "source": ["ebay", "grailed"],
                                      "sources_present": "1", "next": "/"})
    item = eng.db.item(item_id)
    assert (item["status"], item["max_price"], item["size"], item["interval_minutes"]) == \
        ("active", 250.0, "L", 180)
    assert item["sources"] == "ebay,grailed" and item["next_check_at"] <= time.time()

    c.post("/sizes", data={"size_tops": "M", "size_shoes": "US 10", "size_brands": "Nike: US 10.5"})
    c.post("/settings", data={"ebay_marketplace": "EBAY_GB", "default_interval_minutes": "30",
                              "default_source": "ebay", "min_match_score": "0.8"})
    s = eng.db.settings()
    # Saving Settings must not wipe the sizes
    assert (s["size_tops"], s["size_shoes"], s["size_brands"]) == ("M", "US 10", "Nike: US 10.5")
    assert (s["ebay_marketplace"], s["min_match_score"]) == ("EBAY_GB", 0.8)
    assert eng.currency() == "GBP" and eng.has_sizes()


def test_shops_pages_and_use_everywhere(client, monkeypatch):
    c, eng = client
    c.post("/login", data={"password": "pw"})
    monkeypatch.setattr(eng, "detect_stores_async", lambda only_pending=False: None)
    c.post("/shops/add", data={"text": "Haven | havenshop.com\nheddels.com"})
    assert len(eng.db.stores()) == 2
    item_id = eng.db.create_item(status="active", name="Shoe", search_query="shoe", sources="ebay")
    r = c.get("/shops")
    assert r.status_code == 200 and "Search these shops for every item" in r.text
    c.post("/shops/use-everywhere")
    assert eng.db.item(item_id)["sources"] == "ebay,stores"
    assert "stores" in eng.db.settings()["default_sources"]
    sid = eng.db.stores()[0]["id"]
    c.post(f"/shops/{sid}/toggle")
    assert eng.db.one("SELECT enabled FROM stores WHERE id = ?", sid)["enabled"] == 0
    assert c.get("/sizes").status_code == 200


def test_api_ingest_requires_token(client):
    c, _ = client
    assert c.post("/api/ingest", json={"url": "https://www.instagram.com/p/x/"}).status_code == 401
    r = c.post("/api/ingest", json={"url": "no link"}, headers={"Authorization": "Bearer tok"})
    assert r.status_code == 400


# ---------------------------------------------------------------- sizes & shops
def test_wanted_size_uses_brand_then_category(tmp_path):
    eng = make_engine(tmp_path, {})
    eng.db.save_settings({"size_tops": "M", "size_outerwear": "", "size_bottoms": "32",
                          "size_inseam": "30", "size_shoes": "US 10",
                          "size_brands": "Our Legacy: 48\nNike: US 10.5\nbad line"})
    assert eng.wanted_size({"brand": "Our Legacy", "category": "tops"}) == "48"
    assert eng.wanted_size({"brand": "Nike SB", "category": "shoes"}) == "US 10.5"
    assert eng.wanted_size({"brand": "Asics", "category": "shoes"}) == "US 10"
    assert eng.wanted_size({"brand": "", "category": "outerwear"}) == "M"   # falls back to tops
    assert eng.wanted_size({"brand": "", "category": "bottoms"}) == "W32 L30"
    assert eng.wanted_size({"brand": "", "category": "bags"}) == ""
    assert eng.wanted_size({"size": "XL", "brand": "Our Legacy", "category": "tops"}) == "XL"


def test_shopify_parsing_and_sizes_in_stock():
    from wishlist_radar.sources.shops import apply_product_js, parse_suggest

    body = {"resources": {"results": {"products": [{
        "handle": "box-shirt-black", "title": "Box Shirt Black Silk", "vendor": "Our Legacy",
        "price": "180.00", "compare_at_price_max": "260.00", "available": True,
        "image": "//cdn.shopify.com/x.jpg"}]}}}
    [l] = parse_suggest(body, "https://www.nomanwalksalone.com", {"currency": "USD"})
    assert l.listing_id == "www.nomanwalksalone.com:box-shirt-black"
    assert l.url == "https://www.nomanwalksalone.com/products/box-shirt-black"
    assert l.title == "Our Legacy · Box Shirt Black Silk" and l.image_url.startswith("https://")
    assert l.price == 180.0 and l.condition.startswith("On sale (was 260)")

    product = {"price": 17500, "compare_at_price": 26000, "variants": [
        {"title": "46", "available": False}, {"title": "48", "available": True},
        {"title": "50", "available": True}]}
    l = apply_product_js(l, product)
    assert l.size_text == "In stock: 48, 50" and l.price == 175.0


def test_feed_parsing_and_mention_filter():
    from wishlist_radar.sources.shops import find_feed_url, mentions, parse_feed

    page = '<head><link rel="alternate" type="application/rss+xml" href="/feed/"></head>'
    assert find_feed_url(page, "https://www.heddels.com/") == "https://www.heddels.com/feed/"
    now = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())
    rss = f"""<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
      <channel><item><title>Deal: Paraboot Michael derbies are 30% off</title>
        <link>https://www.heddels.com/2026/11/paraboot-deal/</link><pubDate>{now}</pubDate>
        <description>&lt;p&gt;The classic Michael in marron.&lt;/p&gt;</description>
        <media:content url="https://img/x.jpg"/></item>
      <item><title>Old post</title><link>https://www.heddels.com/old/</link>
        <pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item></channel></rss>"""
    posts = parse_feed(rss, {})
    assert len(posts) == 1 and posts[0].image_url == "https://img/x.jpg"
    assert posts[0].condition == "Article" and "marron" in posts[0].title
    assert mentions(posts[0], "Paraboot Michael marron", "Paraboot")
    assert not mentions(posts[0], "Nike Air Max 1 big bubble", "Nike")


def test_shops_source_search_and_partial_failure(tmp_path, monkeypatch):
    from wishlist_radar.sources import shops

    eng = make_engine(tmp_path, None)
    for name, url, kind in [("NMWA", "https://nmwa.example", "shopify"),
                            ("Broken", "https://broken.example", "shopify"),
                            ("SSENSE", "https://ssense.example", "unsupported")]:
        eng.db.run("INSERT INTO stores (name, url, kind, currency, created_at) VALUES (?,?,?,?,?)",
                   name, url, kind, "USD", time.time())

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, params=None):
            import httpx
            if "broken" in url:
                return httpx.Response(503)
            return httpx.Response(200, json={"resources": {"results": {"products": [
                {"handle": "michael", "title": "Michael Marron", "vendor": "Paraboot", "price": "420.00"}]}}})

    monkeypatch.setattr(shops, "_client", lambda: FakeClient())
    src = eng.sources()["stores"]
    found = src.search("paraboot michael", max_price=None)
    assert [l.merchant for l in found] == ["NMWA"]
    assert src.partial_errors == ["Broken: search returned 503"]
    assert "Last search failed" in eng.db.one("SELECT status FROM stores WHERE name='Broken'")["status"]


def test_add_stores_parses_lines(tmp_path, monkeypatch):
    eng = make_engine(tmp_path, {})
    monkeypatch.setattr(eng, "detect_stores_async", lambda only_pending=False: None)
    n = eng.add_stores("No Man Walks Alone | nomanwalksalone.com\nhttps://www.havenshop.com/collections/new\n"
                       "\nnot a url\nnomanwalksalone.com\nhttps://www.nomanwalksalone.com")
    assert n == 2
    stores = {s["name"]: s["url"] for s in eng.db.stores()}
    assert stores == {"No Man Walks Alone": "https://nomanwalksalone.com",
                      "Havenshop": "https://www.havenshop.com"}


def test_foreign_currency_listing_counts_against_budget(tmp_path, monkeypatch):
    from wishlist_radar import fx

    fx.set_rates("USD", {"USD": 1.0, "GBP": 0.8, "JPY": 150.0})
    shop = FakeSource("stores", [
        Listing(listing_id="a", title="Box Shirt", price=150, currency="GBP", url="u1"),   # $187.50
        Listing(listing_id="b", title="Box Shirt", price=180, currency="GBP", url="u2"),   # $225
        Listing(listing_id="c", title="Box Shirt", price=99, currency="XYZ", url="u3"),    # unknown
    ])
    eng = make_engine(tmp_path, {"stores": shop})
    monkeypatch.setattr(ai, "judge", lambda c, m, item, size, ls, **kw: [
        ai.Verdict(index=i, verdict="match", score=0.9, size_ok="yes", reason="") for i in range(len(ls))])
    item_id = eng.db.create_item(status="active", name="Box Shirt", search_query="box shirt",
                                 max_price=200, sources="stores")
    eng.check_item(item_id)
    deals = {l["listing_id"]: l["is_deal"] for l in eng.db.all("SELECT * FROM listings")}
    assert deals == {"a": 1, "b": 0, "c": 0}
    assert round(fx.convert(150, "GBP", "USD")) == 188


def test_add_suggested_shops(client, monkeypatch):
    c, eng = client
    c.post("/login", data={"password": "pw"})
    monkeypatch.setattr(eng, "detect_stores_async", lambda only_pending=False: None)
    page = c.get("/shops").text
    assert "Suggested shops" in page and "shopcanoeclub.com" in page
    c.post("/shops/add-suggested", data={"pick": ["shopcanoeclub.com", "store.unionlosangeles.com"]})
    urls = {s["url"] for s in eng.db.stores()}
    assert urls == {"https://shopcanoeclub.com", "https://store.unionlosangeles.com"}
    assert "shopcanoeclub.com" not in c.get("/shops").text.split("Suggested shops")[-1].split("Your shops")[0]


def test_rescan_reuses_frames_and_skips_already_found(tmp_path, monkeypatch):
    eng = make_engine(tmp_path, {})
    frame = tmp_path / "f0.jpg"
    frame.write_bytes(b"jpg")
    calls = []

    def identify(client, model, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return ai.Identification(summary="fit pic", items=[ai.FoundItem(
                name="Box Shirt", brand="Our Legacy", category="tops", color="black", description="",
                search_query="our legacy box shirt", frame_index=0, confidence=0.9, reason="")])
        return ai.Identification(summary="", items=[ai.FoundItem(
            name="Michael", brand="Paraboot", category="shoes", color="brown", description="",
            search_query="paraboot michael", frame_index=0, confidence=0.8, reason="frame 0")])

    monkeypatch.setattr(ai, "identify", identify)
    first = eng.import_images([frame], note="")
    post_id = first["post_id"]
    assert [p.name for p in eng.import_frames(post_id)] == ["upload_00.jpg"]

    second = eng.rescan(post_id, "the shoes")
    assert calls[1]["already_found"] == ["Our Legacy Box Shirt"]
    assert calls[1]["note"] == "the shoes" and len(calls[1]["images"]) == 1
    assert second["post_id"] == post_id
    post = eng.db.one("SELECT * FROM posts WHERE id = ?", post_id)
    assert post["item_count"] == 2 and post["summary"] == "fit pic"
    assert {i["post_id"] for i in eng.db.all("SELECT post_id FROM items")} == {post_id}


def test_import_page_renders(client, monkeypatch):
    c, eng = client
    c.post("/login", data={"password": "pw"})
    monkeypatch.setattr(ai, "identify", lambda *a, **k: ai.Identification(summary="A fit", items=[]))
    r = c.post("/add", data={"text": "Paraboot Michael"}, follow_redirects=False)
    assert r.headers["location"].startswith("/imports/")
    page = c.get(r.headers["location"])
    assert page.status_code == 200 and "Paraboot Michael" in page.text


# ---------------------------------------------------------------- sample sales
def test_sales_helpers():
    from datetime import date
    from wishlist_radar import sales

    text = sales.html_to_text('<html><script>x()</script><h2>Sales</h2><p><a href="/e/ol">Our Legacy '
                              'Sample Sale</a> Nov 12&ndash;15</p></html>', "https://site.com/sales/")
    assert "Our Legacy Sample Sale (https://site.com/e/ol)" in text and "x()" not in text
    assert sales.brand_matches("260 Sample Sale: Our Legacy, A.P.C.", ["Our Legacy", "Nike"]) == ["Our Legacy"]
    assert sales.brand_matches("Legacy Home Goods", ["Our Legacy"]) == []
    today = date(2026, 11, 5)
    assert sales.status_of({"start_date": "2026-11-04", "end_date": "2026-11-08"}, today) == "now"
    assert sales.status_of({"start_date": "2026-11-12", "end_date": ""}, today) == "upcoming"
    assert sales.status_of({"start_date": "2026-10-01", "end_date": "2026-10-03"}, today) == "past"
    assert sales.status_of({"start_date": "", "end_date": ""}, today) == "tba"


def test_refresh_sales_dedupes_matches_and_notifies(tmp_path, monkeypatch):
    from wishlist_radar import notify, sales

    eng = make_engine(tmp_path, {})
    eng.db.create_item(status="active", name="Box Shirt", brand="Our Legacy", search_query="q")
    eng.db.save_settings({"sale_brands": "Kapital", "ntfy_topic": "t"})
    monkeypatch.setattr(sales, "fetch_page_text", lambda url: "page text " * 50)
    sent = []
    monkeypatch.setattr(notify, "send", lambda topic, **kw: sent.append(kw["title"]))

    def extract(client, model, text, source_name, today):
        return [ai.FoundSale(brand="Our Legacy", title="Our Legacy Sample Sale", start_date="2099-11-12",
                             end_date="2099-11-15", location="261 W 36th St", online=False,
                             menswear="yes", discount="up to 70% off", details="", url="https://x/ol"),
                ai.FoundSale(brand="Sandro", title="Sandro", start_date="2099-11-20", end_date="",
                             location="", online=True, menswear="no", discount="", details="", url="")]

    monkeypatch.setattr(ai, "extract_sales", extract)
    eng.refresh_sales(force=True)
    rows = eng.db.all("SELECT * FROM sales ORDER BY brand")
    assert [r["brand"] for r in rows] == ["Our Legacy", "Sandro"]          # 5 sources, deduped
    assert len(rows[0]["sources"].split(", ")) == len(sales.DEFAULT_SOURCES)
    assert rows[0]["matched_brands"] == "Our Legacy" and rows[1]["matched_brands"] == ""
    assert sent == ["Sample sale: Our Legacy"]                             # notified once
    eng.refresh_sales(force=True)
    assert sent == ["Sample sale: Our Legacy"]
    assert "Kapital" in eng.followed_brands() and "Our Legacy" in eng.followed_brands()


def test_sales_page_renders_and_filters(client):
    c, eng = client
    c.post("/login", data={"password": "pw"})
    now = time.time()
    for brand, online, menswear in [("Our Legacy", 0, "yes"), ("Sandro", 1, "no"), ("Kapital", 1, "yes")]:
        eng.db.run("INSERT INTO sales (key, brand, title, start_date, online, menswear, url, sources, "
                   "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)", brand, brand, brand,
                   "2099-01-01", online, menswear, "https://x", "Chicmi", now, now)
    page = c.get("/sales").text
    assert "Our Legacy" in page and "Kapital" in page and "Sandro" not in page   # menswear-only default
    online = c.get("/sales?show=online").text
    assert "Kapital" in online and ">Our Legacy<" not in online


# ---------------------------------------------------------------- tracking, feedback, prices, fit
from wishlist_radar.sources import ListingUpdate  # noqa: E402


class RefreshableSource(FakeSource):
    def __init__(self, key, listings, updates=None, details_text=""):
        super().__init__(key, listings)
        self.updates, self.details_text = updates or {}, details_text

    def refresh(self, listing):
        return self.updates.get(listing["listing_id"])

    def details(self, listing):
        return self.details_text


def _match_all(monkeypatch, size_ok="yes"):
    monkeypatch.setattr(ai, "judge", lambda c, m, item, size, ls, **kw: [
        ai.Verdict(index=i, verdict="match", score=0.95, size_ok=size_ok, reason="") for i in range(len(ls))])


def test_size_in_stock():
    from wishlist_radar.prices import size_in_stock
    assert size_in_stock("M / 48", "In stock: 46, 48, 50") is True
    assert size_in_stock("US 10 / UK 9", "In stock: US 9, US 10, US 11") is True
    assert size_in_stock("US 10", "In stock: US 10.5, US 11") is False
    assert size_in_stock("M", "Sold out") is False
    assert size_in_stock("M", "Size M") is None and size_in_stock("", "In stock: M") is None


def test_recheck_price_drop_restock_and_sold(tmp_path, monkeypatch):
    sent = []
    shop = RefreshableSource("stores", [
        Listing(listing_id="s1", title="Box Shirt", price=260, currency="USD",
                url="https://shop/products/box-shirt", size_text="In stock: 46, 50"),
        Listing(listing_id="s2", title="Box Shirt", price=150, currency="USD", url="https://shop/products/b2")])
    eng = make_engine(tmp_path, {"stores": shop})
    eng.db.save_settings({"size_tops": "48", "ntfy_topic": "t"})
    monkeypatch.setattr(eng, "_push", lambda title, message, url="", image_url="": sent.append(title) or True)
    _match_all(monkeypatch, size_ok="no")
    item_id = eng.db.create_item(status="active", name="Box Shirt", brand="Our Legacy", category="tops",
                                 search_query="q", sources="stores", max_price=200)
    eng.check_item(item_id)
    assert sent == []                                     # 48 not in stock at first

    shop.updates = {"s1": ListingUpdate(price=180, size_text="In stock: 46, 48, 50"),
                    "s2": ListingUpdate(available=False)}
    eng.recheck_listings(eng.db.item(item_id), force=True)
    s1 = eng.db.one("SELECT * FROM listings WHERE listing_id = 's1'")
    s2 = eng.db.one("SELECT * FROM listings WHERE listing_id = 's2'")
    assert (s1["price"], s1["prev_price"], s1["size_ok"], s1["is_deal"]) == (180, 260, "yes", 1)
    assert s2["availability"] == "sold" and s2["is_deal"] == 0
    kinds = sorted(e["kind"] for e in eng.db.all("SELECT kind FROM events"))
    assert kinds == ["price_drop", "restock", "sold"]
    assert len(sent) == 1 and sent[0].startswith("Price drop (260 → 180 USD)")


def test_feedback_teaches_judge_and_hides(client, monkeypatch):
    c, eng = client
    c.post("/login", data={"password": "pw"})
    item_id = eng.db.create_item(status="active", name="Box Shirt", search_query="q", sources="ebay")
    a = eng.db.add_listing(item_id, "ebay", listing_id="a", title="Box Shirt faded", price=100, currency="USD")
    b = eng.db.add_listing(item_id, "ebay", listing_id="b", title="Box Shirt mint", price=100, currency="USD")
    eng.db.run("UPDATE listings SET verdict='maybe', score=0.5")
    c.post(f"/listings/{a}/feedback", data={"value": "down"})
    c.post(f"/listings/{b}/feedback", data={"value": "up"})
    la = eng.db.one("SELECT * FROM listings WHERE id = ?", a)
    lb = eng.db.one("SELECT * FROM listings WHERE id = ?", b)
    assert la["dismissed"] == 1 and la["feedback"] == "down"
    assert lb["verdict"] == "match" and lb["is_deal"] == 1
    text = eng.feedback_text(item_id)
    assert "Confirmed as the item:\n- Box Shirt mint" in text and "Rejected" in text and "faded" in text


def test_price_stats_and_budget_suggestion():
    from wishlist_radar import fx, prices
    fx.set_rates("USD", {"USD": 1.0, "GBP": 0.8})
    ls = [{"price": p, "currency": "USD"} for p in (200, 220, 240)] + [{"price": 160, "currency": "GBP"}]
    st = prices.stats(ls, "USD")
    assert st["n"] == 4 and st["median"] == 210 and st["low"] == 200
    assert prices.below_typical(150, 210, 4) == 29 and prices.below_typical(200, 210, 4) is None
    assert prices.below_typical(100, 210, 2) is None
    assert prices.suggested_budget(210, 4) == 180


def test_fit_check_blocks_deal_when_it_wont_fit(tmp_path, monkeypatch):
    ebay = RefreshableSource("ebay", [Listing(listing_id="e1", title="Box Shirt 48", price=100, currency="USD",
                                              url="u")], details_text="p2p 20in, length 27in")
    eng = make_engine(tmp_path, {"ebay": ebay})
    eng.db.save_settings({"meas_chest": "22.5", "meas_unit": "in"})
    _match_all(monkeypatch, size_ok="yes")
    seen = {}

    def fit(client, model, measurements, item, listing, details):
        seen.update(measurements=measurements, details=details)
        return ai.FitCheck(fit="bad", note="p2p 20in vs your 22.5in")

    monkeypatch.setattr(ai, "fit_check", fit)
    item_id = eng.db.create_item(status="active", name="Box Shirt", category="tops", search_query="q", sources="ebay")
    eng.check_item(item_id)
    l = eng.db.one("SELECT * FROM listings")
    assert (l["verdict"], l["fit"], l["is_deal"]) == ("match", "bad", 0)
    assert seen["measurements"] == "pit to pit: 22.5 in" and "p2p 20in" in seen["details"]


def test_product_page_structured_data():
    from wishlist_radar.product_page import structured_summary
    page = '''<html><head><meta property="og:image" content="/img/shirt.jpg">
      <script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Product",
        "name":"Box Shirt Black Silk","brand":{"@type":"Brand","name":"Our Legacy"},"color":"Black",
        "offers":{"@type":"Offer","price":"260.00","priceCurrency":"USD"}}]}</script></head></html>'''
    summary, image = structured_summary(page, "https://shop.com/p/box-shirt")
    assert "Name: Box Shirt Black Silk" in summary and "Brand: Our Legacy" in summary
    assert "Price: 260.00 USD" in summary and image == "https://shop.com/img/shirt.jpg"


def test_import_link_uses_product_page(tmp_path, monkeypatch):
    from wishlist_radar import product_page
    eng = make_engine(tmp_path, {})
    monkeypatch.setattr(product_page, "fetch", lambda url, folder: product_page.Page(
        url=url, site="shop.com", structured="Name: Box Shirt\nBrand: Our Legacy", text="page text", image=None))
    got = {}

    def identify(client, model, **kw):
        got.update(kw)
        return ai.Identification(summary="Product page", items=[])

    monkeypatch.setattr(ai, "identify", identify)
    eng.import_link("https://shop.com/products/box-shirt")
    assert "Brand: Our Legacy" in got["caption"] and "product page for one item" in got["note"]
    assert eng.db.one("SELECT kind FROM posts")["kind"] == "link"


def test_daily_digest_once_a_day_after_the_hour(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    eng = make_engine(tmp_path, {})
    eng.db.save_settings({"ntfy_topic": "t", "digest_hour": 9, "timezone": "America/New_York"})
    sent = []
    monkeypatch.setattr(eng, "_push", lambda title, message, url="": sent.append(message) or True)
    item_id = eng.db.create_item(status="active", name="x", search_query="x")
    eng.db.add_listing(item_id, "ebay", listing_id="m", title="maybe", price=1, currency="USD")
    eng.db.run("UPDATE listings SET verdict = 'maybe'")
    ny = ZoneInfo("America/New_York")
    early = datetime(2026, 11, 6, 8, 0, tzinfo=ny).timestamp()
    nine = datetime(2026, 11, 6, 9, 5, tzinfo=ny).timestamp()
    assert eng.maybe_send_digest(early) is False
    assert eng.maybe_send_digest(nine) is True and "1 possible match to look at" in sent[0]
    assert eng.maybe_send_digest(nine + 3600) is False           # once a day


def test_health_alerts_after_three_failures(tmp_path):
    from wishlist_radar.health import Health
    eng = make_engine(tmp_path, {})
    alerts = []
    h = Health(eng.db, alert=lambda t, m: alerts.append(t))
    for _ in range(4):
        h.fail("eBay", "403 forbidden")
    assert alerts == ["⚠️ eBay keeps failing"]
    h.ok("eBay")
    assert h.problems() == []
    for _ in range(3):
        h.fail("eBay", "again")
    assert len(alerts) == 2


def test_backup_and_pruning(tmp_path):
    eng = make_engine(tmp_path, {})
    folder = tmp_path / "backups"
    folder.mkdir()
    for d in range(1, 10):
        (folder / f"wishlist-2026-01-{d:02d}.db").write_bytes(b"x")
    path = eng.backup()
    import sqlite3
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM items").fetchone() == (0,)
    assert len(eng.backups()) == 7 and eng.backups()[0] == path


def test_yahoo_japan_parsing():
    from wishlist_radar.sources.yahoojp import parse_results
    page = ('<a class="Product__imageLink" href="https://auctions.yahoo.co.jp/jp/auction/x1234" '
            'data-auction-id="x1234" data-auction-title="キャピタル デニムジャケット" '
            'data-auction-price="12,800" data-auction-img="https://img/x.jpg">'
            '<a href="#" data-auction-id="x1234" data-auction-title="dup">')
    [l] = parse_results(page)
    assert (l.listing_id, l.price, l.currency) == ("x1234", 12800.0, "JPY")
    assert l.url == "https://buyee.jp/item/yahoo/auction/x1234" and "デニム" in l.title


def test_new_pages_render(client):
    c, eng = client
    c.post("/login", data={"password": "pw"})
    item_id = eng.db.create_item(status="active", name="Box Shirt", search_query="q", sources="stores,yahoojp")
    lid = eng.db.add_listing(item_id, "stores", listing_id="a", title="Box Shirt", price=180, currency="USD",
                             url="https://shop/products/a")
    eng.db.run("UPDATE listings SET verdict='match', prev_price=260, is_deal=1")
    eng.db.run("INSERT INTO events (item_id, listing_id, kind, old_value, new_value, created_at) "
               "VALUES (?,?,?,?,?,?)", item_id, lid, "price_drop", "260", "180", time.time())
    eng.health.fail("eBay", "No eBay keys yet")
    page = c.get(f"/items/{item_id}").text
    assert "Price drop" in page and "Price &amp; stock changes" in page and "👍" in page
    assert "need" in c.get("/").text and "attention" in c.get("/").text
    st = c.get("/status")
    assert st.status_code == 200 and "No eBay keys yet" in st.text
    assert c.get("/settings").status_code == 200 and c.get("/sizes").status_code == 200
    r = c.post("/settings/backup")
    assert r.status_code == 200 and len(r.content) > 0


# ---------------------------------------------------------------- browser shops & alert emails
def test_real_browser_renders_javascript_page(tmp_path):
    """Starts a local page whose products only appear via JavaScript, and renders it."""
    import http.server
    import threading
    from wishlist_radar import browser

    if not browser.available():
        pytest.skip("playwright not installed")
    (tmp_path / "index.html").write_text(
        "<html><body><div id=r></div><script>document.getElementById('r').innerHTML="
        "'<a href=\"/p/box-shirt\"><img src=\"/i.jpg\">Our Legacy Box Shirt</a> $180';</script></body></html>")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(tmp_path), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = browser.render(f"http://127.0.0.1:{server.server_port}/index.html", timeout_s=20)
    except Exception as e:  # no usable Chromium in this environment
        pytest.skip(f"browser unavailable: {e}")
    finally:
        server.shutdown()
    from wishlist_radar.sales import html_to_text
    text = html_to_text(page.html, page.url, images=True)
    assert "Our Legacy Box Shirt" in text and "/p/box-shirt" in text and "[image:" in text


def test_browser_shop_search(tmp_path):
    from types import SimpleNamespace
    from wishlist_radar.sources.shops import ShopsSource

    stores = [{"id": 1, "name": "SSENSE", "url": "https://www.ssense.com", "kind": "browser", "enabled": 1,
               "search_url": "https://www.ssense.com/en-us/men?q={q}", "currency": "USD"}]
    rendered, reports = [], []
    src = ShopsSource(lambda: stores, report=lambda sid, f: reports.append(f),
                      extract=lambda text, site, ctx: [ai.PageProduct(
                          title="Our Legacy Box Shirt", price=180, currency="USD", was_price=260,
                          url="https://www.ssense.com/en-us/men/product/our-legacy/box-shirt/123",
                          image_url="https://img.ssense.com/x.jpg", sizes="48, 50", sold_out=False)],
                      render=lambda url: rendered.append(url) or SimpleNamespace(html="<p>x</p>", url=url))
    [l] = src.search("our legacy box shirt", max_price=None)
    assert rendered == ["https://www.ssense.com/en-us/men?q=our+legacy+box+shirt"]
    assert l.listing_id == "ssense.com:/en-us/men/product/our-legacy/box-shirt/123"
    assert (l.price, l.size_text, l.condition, l.merchant) == (180, "In stock: 48, 50", "On sale (was 260)", "SSENSE")
    assert any("1 products" in r.get("status", "") for r in reports)
    assert src.search("our legacy box shirt", max_price=None) == []   # throttled: not re-rendered
    assert len(rendered) == 1


def test_use_browser_for_validates_template(tmp_path):
    eng = make_engine(tmp_path, {})
    sid = eng.db.run("INSERT INTO stores (name, url, kind, created_at) VALUES ('SSENSE', 'https://www.ssense.com', "
                     "'unsupported', ?)", time.time())
    with pytest.raises(ValueError):
        eng.use_browser_for(sid, "https://www.ssense.com/search")
    eng.use_browser_for(sid)
    s = eng.db.one("SELECT * FROM stores WHERE id = ?", sid)
    assert s["kind"] == "browser" and s["search_url"] == "https://www.ssense.com/en-us/men?q={q}"


def test_alert_email_parsed_and_matched(tmp_path, monkeypatch):
    from email.message import EmailMessage
    from wishlist_radar import emailbox

    msg = EmailMessage()
    msg["From"] = "Depop <noreply@depop.com>"
    msg["Subject"] = "New items for your saved search: our legacy"
    msg.set_content("plain version")
    msg.add_alternative('<a href="https://www.depop.com/products/x-ol-box/">Our Legacy Box Shirt 48</a> $120',
                        subtype="html")
    parsed = emailbox.parse("7", msg.as_bytes())
    assert parsed.sender_name == "Depop" and "saved search" in parsed.subject and "depop.com/products" in parsed.html

    eng = make_engine(tmp_path, {})
    item_id = eng.db.create_item(status="active", name="Box Shirt", brand="Our Legacy", search_query="our legacy box shirt")
    other = eng.db.create_item(status="active", name="Air Max 1", brand="Nike", search_query="nike air max 1")
    monkeypatch.setattr(ai, "extract_products", lambda client, model, text, site, ctx: [ai.PageProduct(
        title="Our Legacy Box Shirt 48", price=120, currency="USD", was_price=0,
        url="https://www.depop.com/products/x-ol-box/", image_url="", sizes="", sold_out=False)])
    _match_all(monkeypatch)
    assert eng.process_alert_email(parsed) == 1
    l = eng.db.one("SELECT * FROM listings")
    assert (l["item_id"], l["source"], l["merchant"], l["is_deal"]) == (item_id, "email", "Depop alert", 1)
    assert eng.db.one("SELECT COUNT(*) n FROM listings WHERE item_id = ?", other)["n"] == 0
    log = eng.db.one("SELECT * FROM alert_emails")
    assert (log["products"], log["matched"]) == (1, 1)
    assert eng.process_alert_email(parsed) == 0                 # same listing isn't added twice


def test_shops_page_shows_browser_and_email_setup(client):
    c, eng = client
    c.post("/login", data={"password": "pw"})
    eng.db.run("INSERT INTO stores (name, url, kind, status, created_at) VALUES ('SSENSE', 'https://www.ssense.com', "
               "'unsupported', 'blocked', ?)", time.time())
    page = c.get("/shops").text
    assert "Saved-search alerts by email" in page and "ALERTS_IMAP_USER" in page
    if "Search it with a real browser instead" in page:
        assert "ssense.com/en-us/men?q={q}" in page
