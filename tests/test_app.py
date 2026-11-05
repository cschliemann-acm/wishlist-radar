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
