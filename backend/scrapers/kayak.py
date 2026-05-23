"""scrapers/kayak.py — Scrape Kayak multi-city for flight options.

Kayak supports multi-leg search via direct URL:
  https://www.kayak.com/flights/{ORIG}-{DEST}/{DATE_OUT}/{DEST}-{ORIG}/{DATE_BACK}?sort=bestflight_a

For multi-city (independent legs):
  https://www.kayak.com/flights/{LEG1_ORIG}-{LEG1_DEST}/{LEG1_DATE},{LEG2_ORIG}-{LEG2_DEST}/{LEG2_DATE}?sort=bestflight_a

Strategy:
  1. Build URL based on round-trip or multi-city.
  2. Playwright opens, waits for results.
  3. Click "Show more results" loop.
  4. Parse each result card: price, airlines, duration, stops, deep link.
  5. Return normalized Itinerary list.

Anti-bot:
  - Kayak heavily rate-limits. We use stealth-ish user-agent and random delays.
  - If "Are you human?" page appears → raise KayakBlocked.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


class KayakBlocked(RuntimeError):
    pass


@dataclass
class Leg:
    origin: str
    dest: str
    depart_dt: str
    arrive_dt: str
    airline: str
    flight_no: str
    duration_min: int


@dataclass
class Itinerary:
    legs: list[Leg]
    price_usd: float
    total_duration_min: int
    n_stops: int
    booking_url: str
    source: str = "kayak"


def _build_url(origin: str, dest: str, date_out: str, date_back: str | None = None) -> str:
    if date_back:
        return f"https://www.kayak.com/flights/{origin}-{dest}/{date_out}/{dest}-{origin}/{date_back}?sort=bestflight_a"
    return f"https://www.kayak.com/flights/{origin}-{dest}/{date_out}?sort=bestflight_a"


def _build_multicity_url(legs: list[tuple[str, str, str]]) -> str:
    """legs = [(orig, dest, date), ...]"""
    parts = [f"{o}-{d}/{date}" for o, d, date in legs]
    return f"https://www.kayak.com/flights/{','.join(parts)}?sort=bestflight_a"


def _detect_block(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        return False
    return ("are you human" in body or "verify you are human" in body
            or "automated traffic" in body or "captcha" in body)


def _parse_duration(text: str) -> int:
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*m", text)
    return int(h.group(1) if h else 0) * 60 + int(m.group(1) if m else 0)


def _parse_price(text: str) -> float | None:
    m = re.search(r"\$?\s*([\d,]+)", text.replace(" ", ""))
    return float(m.group(1).replace(",", "")) if m else None


def search(
    origin: str, dest: str, date_out: str, date_back: str | None = None,
    *, headless: bool = True, timeout_ms: int = 40_000, debug_dir: Path | None = None,
) -> list[Itinerary]:
    url = _build_url(origin, dest, date_out, date_back)
    return _search_url(url, [(origin, dest)], headless=headless,
                       timeout_ms=timeout_ms, debug_dir=debug_dir)


def search_multicity(
    legs: list[tuple[str, str, str]],
    *, headless: bool = True, timeout_ms: int = 40_000, debug_dir: Path | None = None,
) -> list[Itinerary]:
    """legs = [(origin, dest, date_iso), ...]"""
    url = _build_multicity_url(legs)
    return _search_url(url, [(o, d) for o, d, _ in legs], headless=headless,
                       timeout_ms=timeout_ms, debug_dir=debug_dir)


def _search_url(
    url: str, od_pairs: list[tuple[str, str]],
    *, headless: bool, timeout_ms: int, debug_dir: Path | None,
) -> list[Itinerary]:
    itineraries: list[Itinerary] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 900},
            locale="en-US", timezone_id="America/Santiago",
        )
        page = ctx.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3000)

            if _detect_block(page):
                if debug_dir:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(debug_dir / f"kayak_blocked_{int(time.time())}.png"))
                raise KayakBlocked(f"Anti-bot detected on {page.url}")

            # Wait for results
            try:
                page.wait_for_selector("[class*='resultWrapper'], [class*='result-list']", timeout=20_000)
            except PWTimeout:
                if debug_dir:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(debug_dir / f"kayak_no_results_{int(time.time())}.png"))
                return []

            # Click "Show more results" up to 3x
            for _ in range(3):
                try:
                    btn = page.get_by_text(re.compile("Show more results|Show more|Ver más", re.I)).first
                    if btn.is_visible(timeout=1500):
                        btn.click()
                        page.wait_for_timeout(2500)
                    else:
                        break
                except Exception:
                    break

            cards = page.locator("[class*='resultWrapper']").all()
            for i, card in enumerate(cards):
                try:
                    txt = card.inner_text()
                    price = _parse_price(txt)
                    if price is None:
                        continue
                    dur_min = _parse_duration(txt)
                    stops_match = re.search(r"(\d+)\s+stop|nonstop|direct", txt, re.I)
                    if stops_match and stops_match.lastindex:
                        n_stops = int(stops_match.group(1))
                    else:
                        n_stops = 0

                    # Extract airline (heuristic: capitalized word(s) near top)
                    airline = ""
                    for line in txt.splitlines()[:10]:
                        line = line.strip()
                        if line and not any(ch.isdigit() for ch in line) and len(line) > 3:
                            airline = line
                            break

                    # Booking deep link: look for an anchor inside the card
                    try:
                        href = card.locator("a").first.get_attribute("href", timeout=1000)
                        if href and href.startswith("/"):
                            href = "https://www.kayak.com" + href
                    except Exception:
                        href = page.url

                    o, d = od_pairs[0]  # for now main leg
                    itin = Itinerary(
                        legs=[Leg(origin=o, dest=d, depart_dt="", arrive_dt="",
                                  airline=airline, flight_no="", duration_min=dur_min)],
                        price_usd=price,
                        total_duration_min=dur_min,
                        n_stops=n_stops,
                        booking_url=href or page.url,
                    )
                    itineraries.append(itin)
                except Exception as e:
                    if debug_dir:
                        print(f"kayak parse error {i}: {e}", file=sys.stderr)
                    continue
        finally:
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(debug_dir / f"kayak_final_{int(time.time())}.png"))
                except Exception:
                    pass
            browser.close()
    return itineraries


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--date-out", required=True)
    ap.add_argument("--date-back")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--debug-dir", default="/tmp/flight-intel-debug")
    args = ap.parse_args()

    res = search(args.origin, args.dest, args.date_out, args.date_back,
                 headless=not args.show, debug_dir=Path(args.debug_dir))
    print(f"{len(res)} itineraries:")
    for i, it in enumerate(res, 1):
        print(f"  [{i}] USD {it.price_usd:.0f}  {it.total_duration_min}min  stops={it.n_stops}  {it.legs[0].airline}")
