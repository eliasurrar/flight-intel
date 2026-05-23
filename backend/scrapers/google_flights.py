"""scrapers/google_flights.py — Scrape Google Flights for flight options.

URL pattern (modern Google Flights, 2024+):
  https://www.google.com/travel/flights/search?tfs=<base64_payload>&hl=en&curr=USD

We construct simple search-query URLs instead (more robust):
  https://www.google.com/travel/flights?q=Flights%20from%20{ORIG}%20to%20{DEST}%20on%20{DATE_OUT}%20returning%20{DATE_BACK}

Strategy:
  1. Build URL for a one-way OR round-trip search.
  2. Playwright opens the page, waits for results to render.
  3. Click "More options" / expand to get all itineraries (not just top 4).
  4. Extract each itinerary: legs (with airports + times + airline + flight #),
     total price (USD), total duration, n_stops.
  5. Return normalized list of dicts.

Anti-bot:
  - User-agent rotation
  - Wait for network idle + DOM signal "Best flights"
  - If CAPTCHA / consent → screenshot to /tmp + raise GoogleBlocked
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


class GoogleBlocked(RuntimeError):
    """Google served a consent / CAPTCHA page instead of results."""


@dataclass
class Leg:
    origin: str          # IATA
    dest: str            # IATA
    depart_dt: str       # ISO local datetime
    arrive_dt: str
    airline: str         # 2-letter IATA or full name
    flight_no: str       # e.g. "AV245"
    duration_min: int


@dataclass
class Itinerary:
    legs: list[Leg]
    price_usd: float
    total_duration_min: int
    n_stops: int          # legs-1 per direction; we use total stops outbound only here
    booking_url: str      # google.com deep link or carrier link
    source: str = "google_flights"


def _build_url(origin: str, dest: str, date_out: str, date_back: str | None = None) -> str:
    """Build a Google Flights search URL via the `?q=` natural language path."""
    if date_back:
        q = f"Flights from {origin} to {dest} on {date_out} returning {date_back}"
    else:
        q = f"Flights from {origin} to {dest} on {date_out}"
    return f"https://www.google.com/travel/flights?q={quote_plus(q)}&hl=en&curr=USD"


def _detect_block(page: Page) -> bool:
    """Detect consent walls / CAPTCHAs / unusual-traffic pages."""
    url = page.url.lower()
    if "consent.google" in url or "/sorry/" in url:
        return True
    try:
        body_txt = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        return False
    if "unusual traffic" in body_txt:
        return True
    if "before you continue" in body_txt and "cookies" in body_txt:
        # consent page — try to dismiss
        return True
    return False


def _dismiss_consent(page: Page) -> bool:
    """Try to click 'Reject all' or 'I agree' on the consent page."""
    for label in ["Reject all", "I agree", "Accept all", "Aceptar todo", "Rechazar todo"]:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if btn.is_visible(timeout=1500):
                btn.click()
                page.wait_for_load_state("networkidle", timeout=10_000)
                return True
        except Exception:
            continue
    return False


def _parse_duration(text: str) -> int:
    """'7 hr 30 min' or '2h 15m' → minutes."""
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*m", text)
    return int(h.group(1) if h else 0) * 60 + int(m.group(1) if m else 0)


def _parse_price(text: str) -> float | None:
    """'$849' or 'US$1,234' or 'From 849 US dollars' → 849.0."""
    # Try "From N US dollars" first (Google aria-label format)
    m = re.search(r"From\s+([\d,]+)\s+US\s+dollars", text, re.I)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


# Map airport-name fragments to IATA codes for layovers (extended on demand
# from the registry built by backend/airport_registry.py).
_AIRPORT_NAME_TO_IATA_CACHE: dict[str, str] = {}


def _layover_iata(airport_phrase: str) -> str | None:
    """Best-effort: map 'El Dorado International Airport in Bogotá' → 'BOG'.

    Strategy (priority order):
      1. Exact city match: 'in Bogotá' → city = 'Bogotá' → lookup
      2. Distinctive name fragment match (≥10 chars overlap, large_airport preferred)
      3. Generic substring fallback (least reliable)
    """
    global _AIRPORT_NAME_TO_IATA_CACHE, _AIRPORT_CITY_INDEX, _AIRPORT_TYPE_INDEX
    if not _AIRPORT_NAME_TO_IATA_CACHE:
        try:
            registry = Path(__file__).resolve().parents[2] / "data" / "airports.json"
            if registry.exists():
                airports = json.loads(registry.read_text())
                for iata, info in airports.items():
                    name = (info.get("name") or "").lower()
                    city = (info.get("city") or "").lower()
                    atype = info.get("type", "")
                    _AIRPORT_TYPE_INDEX[iata] = atype
                    if name:
                        _AIRPORT_NAME_TO_IATA_CACHE[name] = iata
                    if city:
                        _AIRPORT_CITY_INDEX.setdefault(city, []).append(iata)
        except Exception:
            return None

    phrase = airport_phrase.lower()

    # 1. City extraction: "... in CITY" pattern
    city_match = re.search(r"\s+in\s+([\w\s'\-áéíóúñ]+?)(?:\.|$)", airport_phrase, re.I)
    if city_match:
        city = city_match.group(1).strip().lower()
        if city in _AIRPORT_CITY_INDEX:
            candidates = _AIRPORT_CITY_INDEX[city]
            # Prefer large_airport
            large = [c for c in candidates if _AIRPORT_TYPE_INDEX.get(c) == "large_airport"]
            if large:
                return large[0]
            return candidates[0]

    # 2. Distinctive name fragment (look for airport names that appear *in* the phrase
    # AND have a length ≥ 10 to avoid spurious "Arturo" matches)
    best_iata = None
    best_overlap = 0
    for name, iata in _AIRPORT_NAME_TO_IATA_CACHE.items():
        if len(name) < 10:
            continue
        if name in phrase and len(name) > best_overlap:
            best_iata = iata
            best_overlap = len(name)
    return best_iata


# State for the cache
_AIRPORT_CITY_INDEX: dict[str, list[str]] = {}
_AIRPORT_TYPE_INDEX: dict[str, str] = {}


def _parse_aria_label(aria: str, origin: str, dest: str, page_url: str) -> Itinerary | None:
    """Parse a Google Flights itinerary aria-label into an Itinerary.

    Example aria:
      "From 1168 US dollars round trip total. 1 stop flight with Avianca. Leaves
       RIOgaleão International Airport at 7:20 AM on Thursday, October 8 and
       arrives at Gustavo Rojas Pinilla - San Andrés International Airport at
       4:35 PM on Thursday, October 8. Total duration 11 hr 15 min.  Layover
       (1 of 1) is a 2 hr 40 min layover at El Dorado International Airport in Bogotá.
       Select flight"
    """
    price = _parse_price(aria)
    if price is None:
        return None

    # Stops
    n_stops = 0
    if re.search(r"\bnonstop\b|\bdirect\b", aria, re.I):
        n_stops = 0
    else:
        sm = re.search(r"(\d+)\s+stops?\s+flight", aria, re.I)
        if sm:
            n_stops = int(sm.group(1))

    # Airline (primary, before "Operated by")
    airline = ""
    m = re.search(r"flight\s+with\s+([A-Z][\w&\-\'\s,]+?)(?:\.\s+Operated by|\.\s+Leaves|\.\s+Layover)", aria)
    if m:
        airline = re.split(r"\.|Operated by", m.group(1))[0].strip(" ,")

    # Duration
    dur_min = 0
    dm = re.search(r"Total\s+duration\s+(\d+\s*hr(?:\s+\d+\s*min)?|\d+\s*min)", aria, re.I)
    if dm:
        dur_min = _parse_duration(dm.group(1))

    # Depart / arrive times (we keep raw text; ISO conversion needs the year context)
    depart_m = re.search(r"Leaves\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s+on\s+(\w+,\s+\w+\s+\d+)", aria)
    arrive_m = re.search(r"arrives at\s+(.+?)\s+at\s+(\d{1,2}:\d{2}\s*(?:AM|PM)?)\s+on\s+(\w+,\s+\w+\s+\d+)", aria)
    depart_dt = f"{depart_m.group(3)} {depart_m.group(2)}" if depart_m else ""
    arrive_dt = f"{arrive_m.group(3)} {arrive_m.group(2)}" if arrive_m else ""

    # Layovers → build legs sequence (origin → layover1 → layover2 → ... → dest)
    layover_airports: list[str] = []
    for lm in re.finditer(r"Layover\s*\(\d+\s+of\s+\d+\)\s+is\s+a\s+[\d\s\w]+\s+layover\s+at\s+(.+?)(?:\.\s+Layover|\.\s+Select|\.\s+Operated|\.\s*$)", aria):
        phrase = lm.group(1)
        iata = _layover_iata(phrase) or phrase[:30]
        layover_airports.append(iata)

    # Build leg chain origin → lay1 → lay2 → ... → dest (we don't have per-leg times,
    # so set the same depart/arrive on the first/last leg as a placeholder).
    chain = [origin] + layover_airports + [dest]
    legs: list[Leg] = []
    for i in range(len(chain) - 1):
        legs.append(Leg(
            origin=chain[i], dest=chain[i + 1],
            depart_dt=depart_dt if i == 0 else "",
            arrive_dt=arrive_dt if i == len(chain) - 2 else "",
            airline=airline, flight_no="", duration_min=0,
        ))

    return Itinerary(
        legs=legs, price_usd=price,
        total_duration_min=dur_min, n_stops=n_stops,
        booking_url=page_url,
    )


def search(
    origin: str, dest: str, date_out: str, date_back: str | None = None,
    *, headless: bool = True, timeout_ms: int = 30_000, debug_dir: Path | None = None,
) -> list[Itinerary]:
    """Search Google Flights and return list of itineraries.

    Args:
      origin, dest: IATA 3-letter codes (e.g. "GIG", "ADZ")
      date_out: ISO date "2026-10-08"
      date_back: optional ISO date for round-trip
      headless: True for background, False to watch the browser
      debug_dir: if provided, dumps screenshots + HTML on parse failures

    Returns:
      List of Itinerary dataclasses (possibly empty if no flights or all parsed failed).

    Raises:
      GoogleBlocked if consent/CAPTCHA can't be dismissed.
    """
    url = _build_url(origin, dest, date_out, date_back)
    itineraries: list[Itinerary] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
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

            # Handle consent walls
            if _detect_block(page):
                if not _dismiss_consent(page):
                    if debug_dir:
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(debug_dir / f"blocked_{int(time.time())}.png"))
                    raise GoogleBlocked(f"Consent/CAPTCHA on {page.url}")
                page.wait_for_timeout(2000)

            # Wait for the results list to render. Google Flights uses aria-labels
            # like "From X US dollars round trip total..." on each result button.
            try:
                page.wait_for_selector("[aria-label*='US dollars'], [aria-label*='dólares']", timeout=20_000)
            except PWTimeout:
                if debug_dir:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(debug_dir / f"no_results_{int(time.time())}.png"))
                    (debug_dir / f"no_results_{int(time.time())}.html").write_text(page.content())
                return []

            # Click "View more flights" / "Other departing flights" to expand
            for label_pat in ["View more flights", "Show more", "more flights",
                              "Other departing flights", "Ver más vuelos"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(label_pat, re.I)).first
                    if btn.is_visible(timeout=1200):
                        btn.click()
                        page.wait_for_timeout(1200)
                except Exception:
                    continue

            # Extract itineraries by aria-label
            elements = page.locator("[aria-label*='US dollars'], [aria-label*='round trip total']").all()
            seen_arias: set[str] = set()
            for el in elements:
                try:
                    aria = el.get_attribute("aria-label") or ""
                    if not aria or aria in seen_arias:
                        continue
                    if "US dollars" not in aria and "round trip total" not in aria:
                        continue
                    seen_arias.add(aria)

                    itin = _parse_aria_label(aria, origin, dest, page.url)
                    if itin is not None:
                        itineraries.append(itin)
                except Exception as e:
                    if debug_dir:
                        print(f"parse error: {e}", file=sys.stderr)
                    continue

        finally:
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(debug_dir / f"final_{int(time.time())}.png"))
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
    ap.add_argument("--show", action="store_true", help="non-headless (watch)")
    ap.add_argument("--debug-dir", default="/tmp/flight-intel-debug")
    args = ap.parse_args()

    print(f"Searching {args.origin}→{args.dest} {args.date_out}" + (f" return {args.date_back}" if args.date_back else " (one-way)"))
    res = search(
        args.origin, args.dest, args.date_out, args.date_back,
        headless=not args.show,
        debug_dir=Path(args.debug_dir),
    )
    print(f"\n{len(res)} itineraries:")
    for i, it in enumerate(res, 1):
        print(f"  [{i}] USD {it.price_usd:.0f}  duration={it.total_duration_min}min  stops={it.n_stops}  airline={it.legs[0].airline}")
    print(json.dumps([asdict(it) for it in res[:3]], indent=2, default=str))
