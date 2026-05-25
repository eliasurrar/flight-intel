"""scrapers/booking.py — Booking.com Flights multi-city scraper.

Booking Flights URL pattern (multi-stop / multi-city):

  https://flights.booking.com/flights/
    {ORIG1}.AIRPORT-{DEST1}.AIRPORT/
    {ORIG2}.AIRPORT-{DEST2}.AIRPORT/
    ?type=MULTISTOP&adults=1&cabinClass=ECONOMY
    &from=ORIG1.AIRPORT&to=DEST1.AIRPORT&depart=YYYY-MM-DD
    &from=ORIG2.AIRPORT&to=DEST2.AIRPORT&depart=YYYY-MM-DD

For one-way / round-trip:
  type=ONEWAY  → single from/to/depart
  type=ROUNDTRIP → from/to/depart + return=YYYY-MM-DD

Strategy:
  1. Build URL → Playwright open.
  2. Wait for result cards (`[data-testid="flight-card"]` or fallback to body-text heuristic).
  3. Scroll until results stabilize or N items loaded.
  4. Parse each card: total price (Booking shows in user currency; force en-US via
     URL param `cur_code=USD` if available, else read displayed currency).
  5. Click "Select" / "Continue" to surface the deep booking link (or read it
     from the card if present). Booking is itself the booking party, so the URL
     IS the booking link.
  6. Return MCItinerary list.

Anti-bot:
  - UA + viewport tuned. Booking is more lenient than Google but has bot detection on
    aggressive scraping. Random sleeps + headed fallback.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Sequence

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class BookingBlocked(RuntimeError):
    pass


@dataclass
class BKLeg:
    origin: str
    dest: str
    depart_dt: str
    arrive_dt: str
    airline: str
    flight_no: str
    duration_min: int


@dataclass
class BKItinerary:
    legs: list[BKLeg]
    price_usd: float
    price_display: str          # raw price string (might be USD, BRL, CLP, etc.)
    total_duration_min: int
    n_stops: int
    booking_url: str            # booking.com deep-link to checkout
    source: str = "booking_flights"
    raw_text: str = ""


# ──────────────────────────────────────────────────────────────────────────
# URL builder
# ──────────────────────────────────────────────────────────────────────────


def build_url(legs: Sequence[tuple[str, str, str]]) -> str:
    """Builds a Booking Flights search URL.

    Booking's multi-city URL is fragile — its form often doesn't accept the
    pre-filled params and lands on an empty results page. For round-trip we use
    the canonical 2-airport URL which loads results reliably. For multi-city
    we still emit the URL but expect manual interaction.
    """
    if not legs:
        raise ValueError("at least one leg required")

    n = len(legs)
    # Detect round-trip pattern (2 reverse legs) — Booking handles these best.
    if (
        n == 2
        and legs[0][0] == legs[1][1]
        and legs[0][1] == legs[1][0]
    ):
        o, d, dep = legs[0]
        ret = legs[1][2]
        params = urllib.parse.urlencode([
            ("type", "ROUNDTRIP"),
            ("adults", "1"),
            ("cabinClass", "ECONOMY"),
            ("sort", "BEST"),
            ("from", f"{o}.AIRPORT"),
            ("to", f"{d}.AIRPORT"),
            ("depart", dep),
            ("return", ret),
        ])
        return f"https://flights.booking.com/flights/{o}.AIRPORT-{d}.AIRPORT/?{params}"

    # One-way
    if n == 1:
        o, d, dep = legs[0]
        params = urllib.parse.urlencode([
            ("type", "ONEWAY"),
            ("adults", "1"),
            ("cabinClass", "ECONOMY"),
            ("sort", "BEST"),
            ("from", f"{o}.AIRPORT"),
            ("to", f"{d}.AIRPORT"),
            ("depart", dep),
        ])
        return f"https://flights.booking.com/flights/{o}.AIRPORT-{d}.AIRPORT/?{params}"

    # Multi-city fallback (URL exists but may land on empty form)
    path_segs = [f"{o}.AIRPORT-{d}.AIRPORT" for o, d, _ in legs]
    path = "/".join(path_segs)
    params: list[tuple[str, str]] = [
        ("type", "MULTISTOP"),
        ("adults", "1"),
        ("cabinClass", "ECONOMY"),
        ("sort", "BEST"),
    ]
    for o, d, dep_d in legs:
        params += [
            ("from", f"{o}.AIRPORT"),
            ("to", f"{d}.AIRPORT"),
            ("depart", dep_d),
        ]
    qs = urllib.parse.urlencode(params)
    return f"https://flights.booking.com/flights/{path}/?{qs}"


# ──────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────


_DURATION_RE = re.compile(r"(?:(\d+)\s*h)?\s*(\d+)?\s*m", re.I)


def _parse_duration(text: str) -> int:
    m = _DURATION_RE.search(text or "")
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    return h * 60 + mins


_PRICE_RE = re.compile(
    r"(?P<sym>[A-Z]{2,3}\$?|\$|R\$|€|£)\s*"
    r"(?P<num>[\d.,]+)"
)


def _parse_price(text: str) -> tuple[float | None, str]:
    """Best-effort. Returns (usd_approx, raw_match). Booking forces user-locale
    currency, so we try to detect and pass-through. USD conversion left to caller.
    """
    m = _PRICE_RE.search(text)
    if not m:
        return None, ""
    raw_num = m.group("num").replace(",", ".") if "." not in m.group("num") else m.group("num").replace(",", "")
    try:
        # heuristic: if there are two separators, the last one is decimal
        s = m.group("num")
        if s.count(",") and s.count("."):
            # 1,234.56 (US) or 1.234,56 (EU)
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif s.count(",") and not s.count("."):
            # 1,234 or 1,23 — ambiguous; assume thousands if 3 digits after
            if len(s.split(",")[-1]) == 3:
                s = s.replace(",", "")
            else:
                s = s.replace(",", ".")
        val = float(s)
        return val, m.group(0)
    except Exception:
        return None, m.group(0)


# ──────────────────────────────────────────────────────────────────────────
# Block detection
# ──────────────────────────────────────────────────────────────────────────


def _detect_block(page: Page) -> bool:
    try:
        url = page.url.lower()
        if "captcha" in url or "blocked" in url:
            return True
        body = page.locator("body").inner_text(timeout=2500).lower()
    except Exception:
        return False
    if "are you a robot" in body or "unusual activity" in body:
        return True
    if "access denied" in body and "booking" in body:
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────
# Card extraction
# ──────────────────────────────────────────────────────────────────────────


def _wait_for_results(page: Page, timeout_ms: int = 25_000) -> None:
    """Booking renders SPA cards under [data-testid=...] selectors that shift over
    time. We use multiple fallbacks."""
    selectors = [
        '[data-testid="searchresults-flight-card"]',
        '[data-testid="flight-card"]',
        'div[data-ui-name="flight_card_v2"]',
        'article[role="article"]',
    ]
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=timeout_ms // len(selectors))
            return
        except PWTimeout:
            continue
    # last resort — wait for any text matching a price
    page.wait_for_function(
        "() => /(?:US\\$|R\\$|\\$|€|£)\\s*[\\d.,]+/.test(document.body.innerText)",
        timeout=timeout_ms,
    )


def _find_cards(page: Page):
    for sel in (
        '[data-testid="searchresults-flight-card"]',
        '[data-testid="flight-card"]',
        'div[data-ui-name="flight_card_v2"]',
    ):
        cards = page.locator(sel).all()
        if cards:
            return cards
    return []


def _extract_card(card) -> BKItinerary | None:
    try:
        txt = card.inner_text(timeout=2_500)
    except Exception:
        return None
    price_val, price_raw = _parse_price(txt)
    if price_val is None:
        return None

    duration = _parse_duration(txt)
    # stops
    n_stops = 0
    if re.search(r"\bnonstop\b|\bdirect\b|\bdirecto\b", txt, re.I):
        n_stops = 0
    else:
        m = re.search(r"(\d+)\s+stop", txt, re.I)
        if m:
            n_stops = int(m.group(1))

    # legs: heuristic — pick all "HH:MM" times in order
    times = re.findall(r"\b(\d{1,2}:\d{2})\s*(AM|PM)?\b", txt)
    # airports — three-letter codes in caps surrounded by separators
    airports = re.findall(r"\b([A-Z]{3})\b", txt)
    legs: list[BKLeg] = []
    if airports and len(airports) >= 2:
        # naive: pair consecutive airport codes
        for i in range(0, len(airports) - 1, 2):
            legs.append(BKLeg(
                origin=airports[i],
                dest=airports[i + 1] if i + 1 < len(airports) else "",
                depart_dt="",
                arrive_dt="",
                airline="",
                flight_no="",
                duration_min=0,
            ))

    return BKItinerary(
        legs=legs,
        price_usd=price_val,  # Note: caller must convert if user locale != USD
        price_display=price_raw,
        total_duration_min=duration,
        n_stops=n_stops,
        booking_url=page_url_for_card(card),
        raw_text=txt[:500],
    )


def page_url_for_card(card) -> str:
    """Try to extract a select-flight URL from the card. Falls back to current page URL."""
    try:
        link = card.locator("a[href*='flights.booking.com']").first
        if link.count():
            href = link.get_attribute("href")
            if href:
                return href
    except Exception:
        pass
    return ""


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


def search_multicity(
    legs: Sequence[tuple[str, str, str]],
    headless: bool = True,
    debug_dir: Path | None = None,
    timeout_ms: int = 45_000,
) -> list[BKItinerary]:
    """Try to scrape Booking. Honest behaviour: Booking's SPA frequently hangs
    on `search_loading_overlay` for 30+ seconds and yields no cards. If we
    can't find prices in time, return [] — the dashboard still shows the
    URL so the user can click through manually.
    """
    url = build_url(legs)
    if debug_dir:
        print(f"[booking] URL: {url}", file=sys.stderr)

    results: list[BKItinerary] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Santiago",
        )
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2000)

            if _detect_block(page):
                if debug_dir:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(debug_dir / f"blocked-{int(time.time())}.png"))
                raise BookingBlocked("captcha or access denied")

            # Booking sometimes shows results, sometimes hangs on overlay.
            # Try to wait, but don't let it block us forever.
            try:
                _wait_for_results(page, timeout_ms=timeout_ms)
                page.wait_for_timeout(1_500)
            except (PWTimeout, Exception) as e:
                if debug_dir:
                    print(f"[booking] no results panel: {e}", file=sys.stderr)
                return results

            cards = _find_cards(page)
            if debug_dir:
                print(f"[booking] {len(cards)} cards", file=sys.stderr)
            for c in cards[:30]:
                it = _extract_card(c)
                if it:
                    if not it.booking_url:
                        it.booking_url = page.url
                    results.append(it)
        finally:
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(debug_dir / f"final-{int(time.time())}.png"))
                except Exception:
                    pass
            browser.close()

    return results


# ──────────────────────────────────────────────────────────────────────────
# Smoke
# ──────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--debug-dir", default="/tmp/flight-intel-debug-booking")
    args = ap.parse_args()

    smoke = [
        ("SCL", "GRU", "2026-06-18"),
        ("GRU", "SCL", "2026-06-29"),
    ]
    print(f"Booking smoke: {smoke}")
    t0 = time.time()
    try:
        res = search_multicity(smoke, headless=not args.show, debug_dir=Path(args.debug_dir))
    except BookingBlocked as e:
        print(f"BLOCKED headless: {e}; retrying headful…", file=sys.stderr)
        res = search_multicity(smoke, headless=False, debug_dir=Path(args.debug_dir))
    print(f"Finished {time.time()-t0:.1f}s — {len(res)} itineraries")
    for i, it in enumerate(res[:8], 1):
        print(f"  [{i}] {it.price_display}  stops={it.n_stops}  dur={it.total_duration_min}min")
    if res:
        print(json.dumps(asdict(res[0]), indent=2, default=str))
