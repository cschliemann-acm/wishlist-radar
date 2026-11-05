"""Web interface + the API the iPhone Shortcut calls.

Run:  uvicorn wishlist_radar.web:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import re
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import fx
from .config import load_config
from .db import DB, ITEM_FIELDS
from .engine import INTERVAL_CHOICES, Engine
from .instagram import DownloadError
from .sources.ebay import MARKETPLACES
from .sources.shops import SUGGESTED_SHOPS, host_key

logging.basicConfig(level=logging.INFO)
HERE = Path(__file__).parent
INSTAGRAM_URL = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s]+")
PUBLIC_PATHS = ("/login", "/health", "/static/", "/api/")


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    config = load_config()
    return Engine(config, DB(config.db_path))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    engine = get_engine()
    if engine.c.scheduler_enabled:
        engine.start_scheduler()
    yield
    engine.stop()


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(PUBLIC_PATHS) and not request.session.get("auth"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)


app = FastAPI(title="wishlist-radar", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=load_config().secret_key,
                   max_age=60 * 60 * 24 * 60, same_site="lax", https_only=False)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ---------------------------------------------------------------------- template helpers
def money(value, currency: str = "") -> str:
    if value is None or value == "":
        return "—"
    symbol = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$"}.get(currency or "", "")
    amount = f"{value:,.0f}" if float(value).is_integer() or value >= 100 else f"{value:,.2f}"
    return f"{symbol}{amount}" if symbol else f"{amount} {currency}".strip()


def interval_label(minutes: int) -> str:
    if minutes < 60:
        return f"every {minutes} min"
    if minutes < 1440:
        h = minutes // 60
        return "every hour" if h == 1 else f"every {h} hours"
    return "once a day"


def ago(ts: float | None) -> str:
    """Server-side fallback; the page's JS turns <time> tags into live relative times."""
    if not ts:
        return "never"
    d = time.time() - ts
    future = d < 0
    d = abs(d)
    for unit, sec in (("day", 86400), ("hour", 3600), ("min", 60)):
        if d >= sec:
            n = int(d // sec)
            s = f"{n} {unit}{'s' if n != 1 and unit != 'min' else ''}"
            return f"in {s}" if future else f"{s} ago"
    return "in under a min" if future else "just now"


def approx(value, frm: str, to: str) -> str:
    """'≈ $123' for prices in another currency (cached rates only, never blocks a page)."""
    if value is None or not frm or not to or frm == to:
        return ""
    converted = fx.convert(value, frm, to, fetch=False)
    return f"≈ {money(round(converted), to)}" if converted is not None else ""


templates.env.filters.update(money=money, interval=interval_label, ago=ago, approx=approx)
templates.env.globals.update(INTERVAL_CHOICES=INTERVAL_CHOICES, MARKETPLACES=MARKETPLACES)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    engine = get_engine()
    ctx.setdefault("flash", request.session.pop("flash", None))
    ctx.setdefault("currency", engine.currency())
    ctx.setdefault("source_labels", {k: s.label for k, s in engine.sources().items()})
    ctx.setdefault("inbox_count", len(engine.db.items(("draft",))))
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _float(v: str | None) -> float | None:
    try:
        return float(str(v).replace(",", "").strip().lstrip("$£€")) if v not in (None, "") else None
    except ValueError:
        return None


# ---------------------------------------------------------------------- auth
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password.encode(), get_engine().c.app_password.encode()):
        request.session["auth"] = True
        return back("/")
    time.sleep(1)
    return templates.TemplateResponse(request, "login.html", {"error": "Wrong password"},
                                      status_code=401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


# ---------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def wishlist(request: Request):
    engine = get_engine()
    db = engine.db
    settings = db.settings()
    items = db.items(("active", "paused"))
    drafts = db.items(("draft",))
    for d in drafts:
        d["default_size"] = engine.wanted_size({**d, "size": ""}, settings)
    upcoming = [i["next_check_at"] for i in items if i["status"] == "active" and i["next_check_at"]]
    return render(request, "wishlist.html", items=items, drafts=drafts, has_sizes=engine.has_sizes(),
                  settings=settings, next_check=min(upcoming) if upcoming else None,
                  deal_total=sum(i["deal_count"] for i in items),
                  archived_count=len(db.items(("archived",))))


@app.get("/archived", response_class=HTMLResponse)
def archived(request: Request):
    db = get_engine().db
    return render(request, "archived.html", items=db.items(("archived",)))


@app.get("/items/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: int):
    engine = get_engine()
    item = engine.db.item(item_id)
    if not item:
        raise HTTPException(404)
    settings = engine.db.settings()
    deals = [l for l in engine.db.item_listings(item_id, ("match",)) if l["is_deal"]]
    matches = [l for l in engine.db.item_listings(item_id, ("match",)) if not l["is_deal"]]
    stats = engine.db.one(
        "SELECT COUNT(*) AS seen, SUM(verdict='match') AS matched, "
        "MIN(CASE WHEN verdict='match' THEN price END) AS low, "
        "AVG(CASE WHEN verdict='match' THEN price END) AS avg FROM listings "
        "WHERE item_id = ? AND dismissed = 0", item_id)
    return render(
        request, "item.html", item=item, settings=settings,
        wanted_size=engine.wanted_size(item, settings),
        deals=deals, matches=matches,
        maybes=engine.db.item_listings(item_id, ("maybe",), limit=50),
        rejected_count=engine.db.one("SELECT COUNT(*) n FROM listings WHERE item_id = ? AND "
                                     "verdict = 'no'", item_id)["n"],
        checks=engine.db.checks(item_id, limit=30),
        last_by_source=engine.db.last_check_by_source(item_id),
        statuses=engine.source_statuses(), stats=stats,
    )


@app.get("/items/{item_id}/rejected", response_class=HTMLResponse)
def item_rejected(request: Request, item_id: int):
    db = get_engine().db
    item = db.item(item_id)
    if not item:
        raise HTTPException(404)
    return render(request, "rejected.html", item=item,
                  listings=db.item_listings(item_id, ("no",), limit=300))


@app.post("/items/{item_id}")
async def update_item(request: Request, item_id: int):
    engine = get_engine()
    item = engine.db.item(item_id)
    if not item:
        raise HTTPException(404)
    form = await request.form()
    fields = {k: (form.get(k) or "").strip() for k in ITEM_FIELDS if k in form}
    if "max_price" in fields:
        fields["max_price"] = _float(fields["max_price"])
    if "interval_minutes" in fields:
        fields["interval_minutes"] = int(fields["interval_minutes"] or 60)
    if form.getlist("source"):
        fields["sources"] = ",".join(s for s in form.getlist("source") if s in engine.sources())
    elif "sources_present" in form:
        fields["sources"] = ""
    if not fields.get("search_query", item["search_query"]):
        fields["search_query"] = item["search_query"]
    query_changed = fields.get("search_query", item["search_query"]) != item["search_query"]
    activating = fields.get("status") == "active" and item["status"] != "active"
    if query_changed or activating or "interval_minutes" in fields:
        fields["next_check_at"] = time.time()  # check soon with the new settings
    engine.db.update_item(item_id, **fields)
    if activating and item["status"] == "draft":
        flash(request, f"Tracking {fields.get('name') or item['name']}. First search starts now.")
        return back(form.get("next") or "/")
    flash(request, "Saved")
    return back(form.get("next") or f"/items/{item_id}")


@app.post("/items/{item_id}/check")
def check_now(request: Request, item_id: int):
    engine = get_engine()
    if not engine.db.item(item_id):
        raise HTTPException(404)
    engine.check_now(item_id)
    flash(request, "Searching now. This page updates when it's done.")
    return back(f"/items/{item_id}")


@app.post("/items/{item_id}/status")
def set_status(request: Request, item_id: int, status: str = Form(...), next: str = Form("")):
    engine = get_engine()
    if status not in ("active", "paused", "archived"):
        raise HTTPException(400)
    extra = {"next_check_at": time.time()} if status == "active" else {}
    engine.db.update_item(item_id, status=status, **extra)
    flash(request, {"active": "Resumed", "paused": "Paused", "archived": "Archived"}[status])
    return back(next or f"/items/{item_id}")


@app.post("/items/{item_id}/delete")
def delete_item(request: Request, item_id: int, next: str = Form("/")):
    engine = get_engine()
    item = engine.db.item(item_id)
    if item:
        engine.db.run("DELETE FROM items WHERE id = ?", item_id)
        if item["image_path"]:
            (engine.c.images_dir / item["image_path"]).unlink(missing_ok=True)
        flash(request, f"Removed {item['name']}")
    return back(next)


@app.post("/listings/{listing_id}/dismiss")
def dismiss_listing(request: Request, listing_id: int):
    db = get_engine().db
    l = db.one("SELECT item_id FROM listings WHERE id = ?", listing_id)
    if not l:
        raise HTTPException(404)
    db.run("UPDATE listings SET dismissed = 1 WHERE id = ?", listing_id)
    return back(f"/items/{l['item_id']}")


@app.post("/listings/{listing_id}/promote")
def promote_listing(request: Request, listing_id: int):
    """User says a 'maybe' or 'no' listing is actually the item."""
    db = get_engine().db
    l = db.one("SELECT l.*, i.max_price FROM listings l JOIN items i ON i.id = l.item_id "
               "WHERE l.id = ?", listing_id)
    if not l:
        raise HTTPException(404)
    in_budget = l["max_price"] is None or (l["price"] is not None and l["price"] <= l["max_price"])
    db.run("UPDATE listings SET verdict='match', score=1, reason='Marked as a match by you', "
           "is_deal=? WHERE id = ?", int(in_budget), listing_id)
    return back(f"/items/{l['item_id']}")


@app.get("/api/items/{item_id}/status")
def item_status(item_id: int):
    item = get_engine().db.item(item_id)
    if not item:
        raise HTTPException(404)
    return {"checking": bool(item["checking"]), "last_checked_at": item["last_checked_at"]}


# ---------------------------------------------------------------------- adding
@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request):
    return render(request, "add.html")


@app.post("/add")
async def add(request: Request, url: str = Form(""), text: str = Form(""), note: str = Form(""),
              files: list[UploadFile] = File(default=[])):
    engine = get_engine()
    files = [f for f in files if f.filename]
    try:
        if url.strip():
            m = INSTAGRAM_URL.search(url)
            if not m:
                flash(request, "That doesn't look like an Instagram link", "error")
                return back("/add")
            result = engine.import_instagram(m.group(0).split("?")[0], note)
        elif files:
            result = await _import_uploads(engine, files, note)
        elif text.strip():
            result = engine.import_text(text.strip())
        else:
            flash(request, "Paste a link, add a screenshot or describe the item", "error")
            return back("/add")
    except DownloadError as e:
        flash(request, str(e), "error")
        return back("/add")
    except Exception as e:
        logging.exception("import failed")
        flash(request, f"Something went wrong: {e}", "error")
        return back("/add")
    flash(request, result["message"], "ok" if result["item_ids"] else "error")
    return back("/" if result["item_ids"] else "/add")


async def _import_uploads(engine: Engine, files: list[UploadFile], note: str) -> dict:
    with tempfile.TemporaryDirectory(dir=engine.c.data_dir) as tmp:
        paths = []
        for i, f in enumerate(files[:10]):
            suffix = ".png" if (f.content_type or "").endswith("png") else ".jpg"
            p = Path(tmp) / f"upload_{i}{suffix}"
            p.write_bytes(await f.read())
            paths.append(p)
        return engine.import_images(paths, note)


# ---------------------------------------------------------------------- activity & settings
@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request):
    db = get_engine().db
    deals = db.all("SELECT l.*, i.name AS item_name, i.brand AS item_brand FROM listings l "
                   "JOIN items i ON i.id = l.item_id WHERE l.is_deal = 1 AND l.dismissed = 0 "
                   "ORDER BY l.first_seen_at DESC LIMIT 30")
    totals = db.one("SELECT COUNT(*) AS checks, SUM(results) AS results, SUM(new_listings) AS new, "
                    "SUM(error != '') AS errors FROM checks WHERE started_at > ?",
                    time.time() - 86400)
    return render(request, "activity.html", deals=deals, checks=db.checks(limit=100),
                  posts=db.all("SELECT * FROM posts ORDER BY created_at DESC LIMIT 20"),
                  totals=totals)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    engine = get_engine()
    base = str(request.base_url).rstrip("/")
    return render(request, "settings.html", settings=engine.db.settings(),
                  statuses=engine.source_statuses(), sources=engine.sources(),
                  ingest_url=f"{base}/api/ingest")


@app.post("/settings")
async def save_settings(request: Request):
    engine = get_engine()
    form = await request.form()
    values = {k: (form.get(k) or "").strip() for k in ("ebay_marketplace", "ntfy_topic")}
    if values["ebay_marketplace"] not in MARKETPLACES:
        values.pop("ebay_marketplace")
    values["default_interval_minutes"] = int(form.get("default_interval_minutes") or 60)
    values["default_sources"] = ",".join(s for s in form.getlist("default_source")
                                         if s in engine.sources()) or "ebay"
    values["min_match_score"] = min(1.0, max(0.0, _float(form.get("min_match_score")) or 0.7))
    engine.db.save_settings(values)
    flash(request, "Settings saved")
    return back("/settings")


@app.get("/sizes", response_class=HTMLResponse)
def sizes_page(request: Request):
    return render(request, "sizes.html", settings=get_engine().db.settings())


SIZE_KEYS = ("size_tops", "size_outerwear", "size_bottoms", "size_inseam", "size_shoes",
             "size_brands", "size_notes")


@app.post("/sizes")
async def save_sizes(request: Request):
    form = await request.form()
    get_engine().db.save_settings({k: (form.get(k) or "").strip() for k in SIZE_KEYS})
    flash(request, "Sizes saved. Every search uses them from now on.")
    return back("/sizes")


@app.get("/shops", response_class=HTMLResponse)
def shops_page(request: Request):
    engine = get_engine()
    stores = engine.db.stores()
    settings = engine.db.settings()
    counts = {k: sum(1 for s in stores if s["kind"] == k) for k in ("shopify", "feed", "unsupported", "pending")}
    tracked = engine.db.items(("active", "paused", "draft"))
    have = {host_key(s["url"]) for s in stores}
    suggestions = [{"name": n, "url": u, "group": g} for n, u, g in SUGGESTED_SHOPS
                   if host_key(u) not in have]
    return render(request, "shops.html", stores=stores, counts=counts, suggestions=suggestions,
                  default_on="stores" in settings["default_sources"].split(","),
                  missing=sum(1 for i in tracked if "stores" not in i["sources"].split(",")))


@app.post("/shops/add")
def shops_add(request: Request, text: str = Form("")):
    n = get_engine().add_stores(text)
    flash(request, f"Added {n} shop{'s' if n != 1 else ''}. Checking what each one supports…" if n
          else "Nothing new to add (already added, or not a web address)", "ok" if n else "error")
    return back("/shops")


@app.post("/shops/add-suggested")
async def shops_add_suggested(request: Request):
    form = await request.form()
    picked = set(form.getlist("pick"))
    lines = [f"{n} | {u}" for n, u, _ in SUGGESTED_SHOPS if u in picked]
    n = get_engine().add_stores("\n".join(lines))
    flash(request, f"Added {n} shop{'s' if n != 1 else ''}. Checking what each one supports…")
    return back("/shops")


@app.post("/shops/redetect")
def shops_redetect(request: Request):
    get_engine().detect_stores_async()
    flash(request, "Re-checking every shop…")
    return back("/shops")


@app.post("/shops/use-everywhere")
def shops_use_everywhere(request: Request):
    engine = get_engine()
    db = engine.db
    for i in db.items(("active", "paused", "draft")):
        sources = [s for s in i["sources"].split(",") if s]
        if "stores" not in sources:
            db.update_item(i["id"], sources=",".join(sources + ["stores"]), next_check_at=time.time())
    defaults = [s for s in db.settings()["default_sources"].split(",") if s]
    if "stores" not in defaults:
        db.save_settings({"default_sources": ",".join(defaults + ["stores"])})
    flash(request, "Your shops are now searched for every item, including new ones")
    return back("/shops")


@app.post("/shops/{store_id}/toggle")
def shops_toggle(request: Request, store_id: int):
    db = get_engine().db
    s = db.one("SELECT enabled FROM stores WHERE id = ?", store_id)
    if s:
        db.update_store(store_id, enabled=0 if s["enabled"] else 1)
    return back("/shops")


@app.post("/shops/{store_id}/delete")
def shops_delete(request: Request, store_id: int):
    get_engine().db.run("DELETE FROM stores WHERE id = ?", store_id)
    return back("/shops")


@app.post("/settings/test-notification")
def test_notification(request: Request):
    from . import notify

    topic = get_engine().db.settings()["ntfy_topic"]
    if not topic:
        flash(request, "Set an ntfy topic first", "error")
        return back("/settings")
    try:
        notify.send(topic, title="wishlist-radar", message="Notifications are working 🎉")
        flash(request, "Test notification sent")
    except Exception as e:
        flash(request, f"Couldn't send: {e}", "error")
    return back("/settings")


@app.get("/images/{name}")
def image(name: str):
    path = get_engine().c.images_dir / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ---------------------------------------------------------------------- API (iPhone Shortcut)
def _check_token(authorization: str) -> None:
    expected = f"Bearer {get_engine().c.ingest_token}"
    if not secrets.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(401, "bad token")


class IngestRequest(BaseModel):
    url: str
    note: str = ""


@app.post("/api/ingest")
def api_ingest(req: IngestRequest, authorization: str = Header(default="")):
    _check_token(authorization)
    m = INSTAGRAM_URL.search(req.url)
    if not m:
        raise HTTPException(400, "No instagram.com link found")
    try:
        return get_engine().import_instagram(m.group(0).split("?")[0], req.note)
    except DownloadError as e:
        return JSONResponse({"message": str(e), "item_ids": []}, status_code=422)


@app.post("/api/ingest/screenshot")
async def api_ingest_screenshot(files: list[UploadFile] = File(...), note: str = Form(""),
                                authorization: str = Header(default="")):
    _check_token(authorization)
    return await _import_uploads(get_engine(), files, note)
