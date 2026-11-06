# wishlist-radar

Share a post (or any product page) with clothes or products in it, and wishlist-radar works out what the items are,
adds them to a wishlist, and keeps searching resale marketplaces for them. When a listing turns up
that's the right item, in your size and under your budget, you get a push notification.

```
Instagram post / screenshot / description
        │
        ▼  Claude Opus identifies the products  → Inbox (you confirm, set size + budget)
        │
Wishlist item ──every N minutes──▶ eBay (official API)  ┐
                                   Grailed (experimental) ┘→ new listings
                                                              │
                                    Claude Haiku: same item? right size? ◀┘
                                                              │
                                  in budget → Deal → push notification (ntfy)
```

The web interface shows every item's **search plan**: what's searched for, where, how often,
when it last ran and when it runs next. It also shows the **deals and matches** found and a
**history** of every search, with results, new listings and errors. The Activity page shows
the same across all items.

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # also needs ffmpeg + ffprobe on your PATH
cp .env.example .env                      # then fill it in (see Keys)
uvicorn wishlist_radar.web:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 and sign in with `APP_PASSWORD`.

**Just want to look around?** Fake data, no keys needed:

```bash
DATA_DIR=./demo-data python scripts/demo_data.py
APP_PASSWORD=demo INGEST_TOKEN=demo DATA_DIR=./demo-data SCHEDULER=off uvicorn wishlist_radar.web:app --port 8000
```

## Keys

| Setting | Where |
|---|---|
| `APP_PASSWORD` | Choose one: it's the web interface's password |
| `INGEST_TOKEN` | `openssl rand -hex 24`. The iPhone Shortcut sends it |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | developer.ebay.com: sign up, then **Application Keys → Production → Create a keyset**. Use the **App ID** and **Cert ID**. You may first need to complete the "marketplace account deletion" notification form, or opt out of it as you don't store eBay user data. |

Then in the app's **Settings** page: your sizes, eBay country, and an ntfy topic for notifications
(install the free **ntfy** app on your phone and subscribe to the same topic name).

## Tracking, learning and prices

- **Price drops, restocks, sold:** matched listings are re-checked every 6 hours (eBay, Grailed
  and Shopify shops). A price drop, or your size coming back in stock, sends a notification and
  can turn a match into a deal. Sold listings move to a "Sold" section, which is useful for
  seeing what things actually go for. Every change is listed on the item page.
- **👍 / 👎:** tell it when a listing is or isn't the item. 👎 hides the listing, and both are
  shown to Claude when it judges new listings for that item, so results improve as you use it.
- **Typical price:** each item shows the typical price, range and number of matches priced
  (converted to your currency). Listings 10%+ under typical are flagged, and items without a
  budget get a suggested one.
- **Measurements:** add yours on the Sizes page (pit to pit, length, waist, inseam…). Matches on
  resale sites are checked against the seller's listed measurements, and ones that won't fit
  don't count as deals.

## Notifications

Deals, price drops, restocks and sample sales for your brands are pushed straight away. Everything
else (possible matches, other changes, new sales, failing sources) comes in one **daily
summary** at a time you choose (Settings).

## Status & backups

The **Status** page (gear icon) shows whether each part works: Claude, each marketplace, each
sale calendar, notifications, exchange rates, speech-to-text. "Test everything" tries them all.
Anything failing 3 times in a row sends a notification, and a red dot shows on the gear icon.
The database is backed up daily on the server (last 7 kept), and Settings has a download button.

## Sample sales

The **Sales** page lists current and upcoming sample sales in NYC and online, gathered every
6 hours from Chicmi (NYC), NYC Insider Guide, Time Out New York, 260 Sample Sale and Arlettie.
Each page is read by Claude Haiku, which pulls out brand, dates, location, discount and whether
it includes menswear. Add any other calendar or brand sales page at the bottom of the page.

Sales for **brands you follow** go to the top and send a push notification. You follow brands on
your wishlist and on your Sizes page automatically, plus any you list on the Sales page. For
item-level sample-sale searches, add "260 Sample Sale (online)" and "Unionmade (archive sale)"
from the Shops page suggestions.

## Imports

Every import gets a review page (linked from the Inbox and Activity) showing what was found,
**the frames Claude looked at**, and the transcript and caption. If it missed something, type what
(e.g. "the shoes and the cap") into **Find what it missed** and it looks again, without
repeating items it already found. Reels are sampled at every scene change plus every 2 seconds
(up to 24 frames). With `TRANSCRIBE=groq` (free key from console.groq.com) pieces named in the
voiceover are caught too.

## Your sizes

The **Sizes** page holds your sizes for tops, outerwear, trousers (waist + inseam) and shoes,
plus **brand-specific sizes** (`Our Legacy: 48`) and free-text fit notes. Every search uses
them automatically, in this order: the item's own size, then your size for that brand, then
your size for the category.

## Shops

The **Shops** page holds your favourite stores and sites (one address per line, or
`Name | address`). Each is checked when added:

- **Shopify stores** (most independent menswear shops) are searched with the store's own
  product search. New products are opened once to see which sizes are **in stock** and whether
  they're **on sale**.
- **News and deal sites** (e.g. Heddels) have their RSS feed read. Posts
  mentioning a wishlist item are passed to the matcher, and a post specifically about it counts
  as a deal.
- **Everything else** (big retailers with custom sites that block automated access, such as
  SSENSE, Huckberry or BSTN) is marked *not supported*.

The page also offers a ready-made list of shops (yours plus similar ones) to add in one click.
Prices in other currencies are converted with daily exchange rates when comparing with your
budget, so a £150 listing counts against a $200 budget.

Tick **Shops** on an item to search them all, or use "Yes, use them everywhere" on the Shops page.

## Marketplaces

- **eBay:** official Browse API. Reliable.
- **Grailed (experimental, off by default):** Grailed has no public API. The connector reads
  the search key Grailed's own website uses and runs the same searches. It can break when Grailed
  changes their site, and automated access may be against their terms, so use it for personal use
  with infrequent checks. If key discovery stops working, find the `x-algolia-application-id` and
  `x-algolia-api-key` headers in your browser's network tab on grailed.com/shop, and set
  `GRAILED_ALGOLIA_APP_ID` / `GRAILED_ALGOLIA_API_KEY`.
- **Yahoo! Japan Auctions (experimental, off by default):** the main source for Japanese brands.
  Reads the public search page with a Japanese query Claude writes for each item, and links open
  on **Buyee**, a proxy service that buys and ships for you. Unofficial, so it may break or be
  against Yahoo's terms. Keep checks infrequent.
- Depop, Vinted and Mercari aren't supported. They actively block automated access.

Each marketplace is a small module in `wishlist_radar/sources/` with a single `search()` method,
so adding one is self-contained.

## iPhone Shortcut

Same as reel-tunes: a share-sheet Shortcut with **Get Contents of URL**:
- URL `https://<your-server>/api/ingest`, method POST
- Header `Authorization: Bearer <INGEST_TOKEN>`
- JSON body: `url` = Shortcut Input (optionally `note`, e.g. "the jacket")
- then **Get Dictionary Value** `message`, then **Show Notification**

Shared items land in the **Inbox** on the wishlist page. Set a size and budget, then tap **Start tracking**.

## Deploy to Railway

1. railway.com → **New Project → Deploy from GitHub repo → wishlist-radar**
2. In the service: **Settings → Volumes → Add Volume**, mount path `/data`. Without it
   your wishlist is wiped on every deploy.
3. **Variables → Raw Editor**: paste your `.env` (without `DATA_DIR`)
4. **Settings → Networking → Generate Domain**
5. Open the domain, sign in, fill in Settings, and point the Shortcut at `https://<domain>/api/ingest`

Keep it at **one replica**: the database and background checker assume a single server.

## Costs

- Identifying items in a post: one Claude Opus call with up to 20 video frames, roughly 5–15¢.
- Checking listings: Claude Haiku, only for listings it hasn't seen before. That's usually a
  fraction of a cent per check.
- eBay API: free (5,000 calls/day by default). An item checked hourly on eBay and Grailed uses
  48 calls a day.

## Tests

```bash
pip install -r requirements-dev.txt && pytest
```
