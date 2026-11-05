"""Claude calls: identify products in a post, and judge marketplace listings against an item."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

CATEGORIES = ["tops", "bottoms", "outerwear", "shoes", "bags", "accessories", "other"]


# --------------------------------------------------------------------------- identify
class FoundItem(BaseModel):
    name: str
    brand: str
    category: Literal["tops", "bottoms", "outerwear", "shoes", "bags", "accessories", "other"]
    color: str
    description: str
    search_query: str
    frame_index: int
    confidence: float
    reason: str


class Identification(BaseModel):
    items: list[FoundItem]
    summary: str


IDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "brand": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "color": {"type": "string"},
                    "description": {"type": "string"},
                    "search_query": {"type": "string"},
                    "frame_index": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "brand", "category", "color", "description", "search_query",
                             "frame_index", "confidence", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["items", "summary"],
    "additionalProperties": False,
}

IDENTIFY_SYSTEM = """You help someone build a wishlist from social media posts. Identify the \
products in the post they might want to buy (clothing, shoes, bags, accessories, other objects), \
so they can be hunted down in shops and on resale marketplaces like eBay and Grailed.

You get some of: the caption, a transcript of what's said, still frames (numbered from 0), and \
a note from the user.

Be thorough. Outfit and "fit check" posts usually show several pieces: list EVERY distinct piece \
worn or shown: outerwear, shirts, knitwear, t-shirts, trousers, shoes, socks, bags, hats, \
eyewear, belts, watches, jewellery. Look across all frames: pieces are often only visible in a \
few, and close-ups, labels and on-screen text overlays ("jacket: Kapital") give them away. The \
transcript often names brands and pieces; trust it for names, but match each to what you see. \
Include pieces whose brand you can't identify, described precisely, with low confidence.

For each distinct product:
- name: the specific product name/model if identifiable (e.g. "Box Shirt", "Air Max 1 '86 OG \
Big Bubble"), otherwise a short precise description ("Boxy suede work jacket").
- brand: as it's usually written on resale sites; "" if unknown. Use tags, captions and visible \
logos; don't guess a brand from style alone.
- category: one of the allowed values.
- color, description: what distinguishes it (material, fit, details, era/season if known).
- search_query: the query a savvy reseller-shopper would type on eBay/Grailed: brand + model + \
1-3 key descriptors, no size, no filler words. 3-8 words.
- frame_index: the frame that shows it best, or -1 if none.
- confidence (0-1): how sure you are about brand+model. Unbranded/guessed items under 0.5.
- reason: where you saw it.

If the user's note names specific items, focus on those (and return only those if the note says \
"only"). If there's no post and the note is a list of products (one per line, or \
comma-separated), return every one of them as its own item, in the same order, keeping any \
colourway, year or version they specified. Skip things that aren't part of the outfit or product \
focus, like furniture or other people in the background, unless the note asks for them. If a \
list of already-found items is given, don't return those again. Return an empty list if \
nothing (new) is identifiable.
summary: one short sentence about the post."""


def _image_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    mt = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}


def _first_text(response) -> str:
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process this")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude's response was cut off")
    return next(b.text for b in response.content if b.type == "text")


def identify(client: anthropic.Anthropic, model: str, *, caption: str = "", note: str = "",
             uploader: str = "", images: list[Path] | None = None, transcript: str = "",
             already_found: list[str] | None = None) -> Identification:
    content: list[dict] = []
    for i, p in enumerate(images or []):
        content.append({"type": "text", "text": f"Frame {i}:"})
        content.append(_image_block(p))
    parts = []
    if uploader:
        parts.append(f"<uploader>{uploader}</uploader>")
    parts.append(f"<caption>\n{caption or '(none)'}\n</caption>")
    if transcript:
        parts.append(f"<transcript>\n{transcript}\n</transcript>")
    if already_found:
        parts.append("<already_found>\n" + "\n".join(already_found) + "\n</already_found>")
    if note:
        parts.append(f"<user_note>\n{note}\n</user_note>")
    content.append({"type": "text", "text": "\n\n".join(parts)})

    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=IDENTIFY_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium",
                       "format": {"type": "json_schema", "schema": IDENTIFY_SCHEMA}},
        # Retry on Anthropic's recommended model if a safety classifier declines the request
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    return Identification.model_validate(json.loads(_first_text(response)))


# --------------------------------------------------------------------------- judge listings
class Verdict(BaseModel):
    index: int
    verdict: Literal["match", "maybe", "no"]
    score: float
    size_ok: Literal["yes", "no", "unknown"]
    reason: str


class Verdicts(BaseModel):
    results: list[Verdict]


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["match", "maybe", "no"]},
                    "score": {"type": "number"},
                    "size_ok": {"type": "string", "enum": ["yes", "no", "unknown"]},
                    "reason": {"type": "string"},
                },
                "required": ["index", "verdict", "score", "size_ok", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = """You screen resale marketplace listings for someone hunting a specific item. \
Listing titles are messy (abbreviations, typos, keyword stuffing); read them like an experienced \
reseller.

For every listing return:
- verdict: "match" = the same product (same brand and same model/style; colour must match if \
the wanted item specifies one). "maybe" = plausibly the same but the title is too vague to be \
sure, or a very close variant. "no" = a different product, a different brand, a replica/"inspired \
by" item, an accessory or part for it, a lot/bundle, or a "wanted"/"ISO" post.
- score (0-1): confidence that it's the wanted product.
- size_ok: "yes" if the listing's size matches the wanted size (allow equivalent notations, e.g. \
"M"/"48"/"2" when the brand uses that scale; shoe sizes: compare in the same system), "no" if it \
clearly doesn't, "unknown" if the listing doesn't say or no size is wanted.
- reason: a few words.

Some entries come from shops rather than resale sites: their size field lists the sizes in \
stock ("In stock: S, M, L"), so size_ok is "yes" only if the wanted size is in that list, "no" if \
it isn't or it's sold out. Entries with condition "Article" are posts from news/deals sites: \
"match" only if the post is specifically about the wanted item (e.g. a deal on it or its \
release), not a passing mention; size_ok "unknown".

Return one result per listing, using its index."""


def judge(client: anthropic.Anthropic, model: str, item: dict, wanted_size: str,
          listings: list[dict], fit_notes: str = "") -> list[Verdict]:
    lines = []
    for i, l in enumerate(listings):
        price = f"{l['price']:.2f} {l['currency']}" if l.get("price") is not None else "?"
        extra = " | ".join(x for x in (f"shop: {l['merchant']}" if l.get("merchant") else "",
                                        f"size: {l['size_text']}" if l.get("size_text") else "",
                                        f"condition: {l['condition']}" if l.get("condition") else "")
                           if x)
        lines.append(f"[{i}] {l['title']} | {price}{' | ' + extra if extra else ''}")

    wanted = "\n".join(f"{k}: {v}" for k, v in (
        ("name", item["name"]), ("brand", item.get("brand") or "(any)"),
        ("category", item.get("category")), ("colour", item.get("color") or "(any)"),
        ("details", item.get("description") or ""), ("user notes", item.get("notes") or ""),
        ("wanted size", wanted_size or "(any size)"), ("fit notes", fit_notes),
    ) if v)
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content":
                   f"<wanted_item>\n{wanted}\n</wanted_item>\n\n<listings>\n"
                   + "\n".join(lines) + "\n</listings>"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
    )
    results = Verdicts.model_validate(json.loads(_first_text(response))).results
    return [r for r in results if 0 <= r.index < len(listings)]


# --------------------------------------------------------------------------- sample sales
class FoundSale(BaseModel):
    brand: str
    title: str
    start_date: str
    end_date: str
    location: str
    online: bool
    menswear: Literal["yes", "no", "unknown"]
    discount: str
    details: str
    url: str


class FoundSales(BaseModel):
    sales: list[FoundSale]


SALES_SCHEMA = {
    "type": "object",
    "properties": {
        "sales": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "brand": {"type": "string"},
                    "title": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "location": {"type": "string"},
                    "online": {"type": "boolean"},
                    "menswear": {"type": "string", "enum": ["yes", "no", "unknown"]},
                    "discount": {"type": "string"},
                    "details": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["brand", "title", "start_date", "end_date", "location", "online",
                             "menswear", "discount", "details", "url"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sales"],
    "additionalProperties": False,
}

SALES_SYSTEM = """You read pages that list sample sales and pull out each individual sale.

For every distinct sale on the page that is happening now or in the future (or whose dates \
aren't announced yet), return:
- brand: the brand or designer (for multi-brand sales, the organiser plus the main brands, \
e.g. "260 Sample Sale: Our Legacy, A.P.C.").
- title: short name of the sale as listed.
- start_date / end_date: YYYY-MM-DD. Infer the year from today's date (a sale "Nov 12-15" listed \
today is this year unless that's clearly past). "" if not stated.
- location: street address or neighbourhood for in-person sales; "" for online.
- online: true for online sales.
- menswear: "yes" if it includes men's clothing/shoes/accessories, "no" if clearly women's- or \
kids-only or homeware, "unknown" otherwise.
- discount: e.g. "up to 70% off", "" if not stated.
- details: one short line of anything useful (RSVP needed, cash only, members' early access).
- url: the sale's own link from the page if there is one (they appear in brackets after the \
link text), otherwise "".

Skip sales that have clearly ended, adverts, and navigation. Don't invent sales or dates."""


def extract_sales(client: anthropic.Anthropic, model: str, page_text: str, source_name: str,
                  today: str) -> list[FoundSale]:
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=SALES_SYSTEM,
        messages=[{"role": "user", "content":
                   f"Today is {today}. Page: {source_name}\n\n<page>\n{page_text}\n</page>"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SALES_SCHEMA}},
    )
    return FoundSales.model_validate(json.loads(_first_text(response))).sales
