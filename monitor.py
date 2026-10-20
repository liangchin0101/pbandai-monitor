#!/usr/bin/env python3
"""
Premium Bandai Card Monitor (GitHub Actions edition)

Checks the Premium Bandai "BANDAI CARD SHOP" in every overseas region for
One Piece, Digimon and Union Arena products (see KEYWORDS) once per run, then sends Telegram alerts:
  - NEW product   -> alert once, the first time it is seen
  - RESTOCK       -> alert every time a product goes from unavailable to available
  - SOLD OUT      -> no alert

State is kept in state.json (committed back to the repo by the workflow).
On the very first run it only records what exists and sends one "started" message.

Secrets (GitHub -> Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------- settings
REGIONS = {  # area code -> label
    "SG": "Singapore",
    "HK": "Hong Kong",
    "TW": "Taiwan",
    "US": "USA",
    "AU": "Australia",
    "NZ": "New Zealand",
    "FR": "France",
}
CARD_SHOP = "05-0004"          # Premium Bandai "BANDAI CARD SHOP" = all card games
# Only products whose name contains one of these (any language, case-insensitive).
# Edit this list to add/remove games. Empty list = every card game.
KEYWORDS = [
    "ONE PIECE", "ワンピース", "航海王", "海賊王",      # One Piece Card Game
    "DIGIMON", "デジモン", "數碼寶貝", "数码宝贝",      # Digimon Card Game
    "UNION ARENA", "ユニオンアリーナ",                 # Union Arena
]
PAGE_SIZE = 100
MAX_PAGES = 20                 # safety cap per region
MAX_ALERTS_PER_RUN = 25        # avoid flooding Telegram; extras are summarised
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "90"))    # seconds between checks
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "270"))   # how long one GitHub run keeps checking

API_URL = "https://p-bandai.com/api/search"
WIDGET_URL = "https://p-bandai.com/api/widget/product/NewArrivalItem"  # fallback (old bot used this)
ITEM_URL = "https://p-bandai.com/{area}/item/{code}"
IMAGE_BASE = "https://p-bandai.com/"
UNAVAILABLE_FLAGS = {"OUT_OF_STOCK", "PRE_ORDER_CLOSED"}

STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------- fetching
def is_available(product: dict) -> bool:
    if product.get("saleStatus") != "On":
        return False
    return not (set(product.get("flags") or []) & UNAVAILABLE_FLAGS)


def status_text(product: dict) -> str:
    flags = set(product.get("flags") or [])
    if is_available(product):
        return "Pre-order open" if "PRE_ORDER" in flags else "In stock"
    if "CHANCE_TO_BUY_DRAWING" in flags:
        return "Lottery / drawing"
    if product.get("saleStatus") == "Waiting":
        return "Coming soon"
    return "Not available"


def price_text(product: dict) -> str:
    price = product.get("fixedListPrice") or product.get("baseListPrice") or {}
    amount, currency = price.get("amount"), price.get("currency") or ""
    if amount is None:
        return "-"
    try:
        return f"{currency} {float(amount):,.2f}".strip()
    except (TypeError, ValueError):
        return f"{currency} {amount}".strip()


def image_url(product: dict) -> str | None:
    images = product.get("productImages") or []
    url = images[0].get("fileUrl") if images else None
    if not url:
        return None
    return url if url.startswith("http") else IMAGE_BASE + url.lstrip("/")


def product_name(product: dict) -> str:
    names = product.get("productName") or {}
    return names.get("en") or next((v for v in names.values() if v), product["productCode"])


def get_json(session: requests.Session, url: str, params: dict, headers: dict, area: str):
    for attempt in range(3):
        try:
            r = session.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  [{area}] attempt {attempt + 1} failed: {exc}")
            time.sleep(2 ** attempt)
    return None


def wanted(product: dict) -> bool:
    if not KEYWORDS:
        return True
    names = " ".join(str(v) for v in (product.get("productName") or {}).values()).upper()
    return any(k.upper() in names for k in KEYWORDS)


def fetch_region(session: requests.Session, area: str) -> list[dict]:
    """Whole card-shop catalogue for one region (incl. sold-out items).
    Falls back to the 'new arrivals' widget if the catalogue search fails."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "X-G1-Area-Code": area,
        "Accept-Language": "en",
        "Referer": f"https://p-bandai.com/{area.lower()}/shop/bandaicardshop",
    }
    products: list[dict] = []
    offset, total = 0, None
    for _ in range(MAX_PAGES):
        params = {"_f_shops": CARD_SHOP, "offset": offset,
                  "limit": PAGE_SIZE, "sortType": "NewArrival"}
        data = get_json(session, API_URL, params, headers, area)
        if not isinstance(data, dict):
            products = []
            break
        result = data.get("productResults") or {}
        total = int(result.get("totalCount") or 0) if total is None else total
        page = [p for p in (result.get("products") or []) if p.get("productCode")]
        products.extend(page)
        offset += len(page)
        if not page or len(page) < PAGE_SIZE or offset >= total:
            break
    if products:
        return products

    print(f"  [{area}] catalogue search gave nothing - trying new-arrivals widget")
    data = get_json(session, WIDGET_URL,
                    {"shopCode": CARD_SHOP, "excludeOutOfStock": "false", "limit": 100},
                    headers, area)
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict) and p.get("productCode")]
    raise RuntimeError(f"{area}: API not reachable")


# ---------------------------------------------------------------- telegram
def tg_call(method: str, payload: dict) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for _ in range(3):
        r = requests.post(url, data=payload, timeout=30)
        if r.status_code == 429:  # rate limited
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(int(wait) + 1)
            continue
        if not r.ok:
            print(f"  Telegram {method} failed: {r.status_code} {r.text[:200]}")
        return r.ok
    return False


def send_text(text: str) -> None:
    tg_call("sendMessage", {"chat_id": CHAT_ID, "text": text,
                            "parse_mode": "HTML", "disable_web_page_preview": "true"})


def send_alert(kind: str, area: str, product: dict) -> None:
    code = product["productCode"]
    link = ITEM_URL.format(area=area.lower(), code=code)
    title = "🆕 NEW" if kind == "new" else "🔄 RESTOCK / ON SALE"
    caption = (
        f"<b>{title}</b> · {REGIONS[area]}\n"
        f"<b>{html.escape(product_name(product))}</b>\n"
        f"💰 {html.escape(price_text(product))}\n"
        f"📦 {status_text(product)}\n"
        f"🔗 <a href=\"{link}\">Open on Premium Bandai</a>"
    )
    img = image_url(product)
    sent = img and tg_call("sendPhoto", {"chat_id": CHAT_ID, "photo": img,
                                         "caption": caption, "parse_mode": "HTML"})
    if not sent:  # no image, or Telegram couldn't load it
        send_text(caption)
    time.sleep(1.2)


# ---------------------------------------------------------------- state
def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- main
def check_once(session: requests.Session) -> bool:
    """One full check of all regions. Returns False if every region failed."""
    previous = load_state()
    first_run = previous is None
    state = dict(previous or {})          # keep entries for regions that fail
    seeded = set(state.pop("_seeded_regions", []))   # regions with a baseline
    alerts: list[tuple[str, str, dict]] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ok_regions, failed = [], []

    for area in REGIONS:
        try:
            products = fetch_region(session, area)
        except RuntimeError as exc:
            print(f"! {exc}")
            failed.append(area)
            continue
        ok_regions.append(area)
        quiet = area not in seeded        # first successful check: record only
        products = [p for p in products if wanted(p)]
        print(f"{area}: {len(products)} matching products" + (" (baseline)" if quiet else ""))

        for p in products:
            key = f"{area}:{p['productCode']}"
            avail = is_available(p)
            prior = state.get(key)
            if not quiet:
                if prior is None:
                    alerts.append(("new", area, p))
                elif avail and not prior.get("available"):
                    alerts.append(("restock", area, p))
            state[key] = {"name": product_name(p), "available": avail,
                          "first_seen": (prior or {}).get("first_seen", today)}

    if not ok_regions:
        print("All regions failed - nothing saved.")
        return False

    state["_seeded_regions"] = sorted(seeded | set(ok_regions))
    save_state(state)

    if first_run:
        send_text(
            "✅ <b>Premium Bandai monitor started</b>\n"
            f"Tracking {len(state) - 1} One Piece / Digimon / Union Arena products in {', '.join(ok_regions)}.\n"
            "You'll get alerts for new products and restocks."
            + (f"\n⚠️ Not reachable: {', '.join(failed)}" if failed else "")
        )
        return True

    for kind, area, p in alerts[:MAX_ALERTS_PER_RUN]:
        send_alert(kind, area, p)
    if len(alerts) > MAX_ALERTS_PER_RUN:
        send_text(f"…and {len(alerts) - MAX_ALERTS_PER_RUN} more updates this round. "
                  "Check Premium Bandai.")

    print(f"Sent {len(alerts)} alert(s). Failed regions: {failed or 'none'}")
    return True


def main() -> int:
    """Repeat the check every CHECK_EVERY seconds until RUN_SECONDS is used up.
    GitHub starts a new run every 5 minutes, so this gives ~90-second checks."""
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret.")
        return 1

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    deadline = time.monotonic() + RUN_SECONDS
    any_ok = False
    n = 0
    while True:
        n += 1
        started = time.monotonic()
        print(f"--- check #{n} at {datetime.now(timezone.utc):%H:%M:%S} UTC ---")
        any_ok = check_once(session) or any_ok
        next_start = started + CHECK_EVERY
        if next_start + 30 > deadline:      # not enough time left for another check
            break
        time.sleep(max(0, next_start - time.monotonic()))
    return 0 if any_ok else 1               # red X in GitHub if nothing worked


if __name__ == "__main__":
    sys.exit(main())
