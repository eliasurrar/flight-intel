"""scrapers/google_flights_multicity.py — Multi-city scraper for Google Flights.

Accepts a list of legs `[(origin, dest, date_iso), ...]` and returns Itinerary
objects (per-leg airline/flight/times, total price USD, total duration, n_stops,
and the best available `booking_url`).

Approach:
  1. Open https://www.google.com/travel/flights?hl=en&curr=USD
  2. Switch ticket type to "Multi-city".
  3. For each leg: fill origin combobox (IATA → first option), dest combobox,
     date textbox (typed as e.g. "Jun 18, 2026") + Enter.
  4. Click "Add flight" if more than 2 legs.
  5. Click "Search" button.
  6. Wait for result list and parse each result via its aria-label
     ("From N US dollars total. …").
  7. Click each result to surface the booking deep-link in the right panel /
     bottom-sheet — extract `https://www.google.com/travel/clk?…` or carrier
     URL. If the panel shows "Book elsewhere unavailable" / "Not available",
     leave booking_url empty.

Anti-bot:
  - UA + viewport + locale tuned to look like a regular Chrome on macOS.
  - Detect consent / sorry / unusual traffic walls → screenshot to /tmp +
    raise GoogleBlocked.
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PWTimeout,
    Page,
    Locator,
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class GoogleBlocked(RuntimeError):
    """Google served a consent / CAPTCHA page instead of results."""


# ──────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class MCLeg:
    origin: str          # IATA
    dest: str            # IATA
    depart_dt: str       # ISO local datetime ("2026-06-18T11:50:00") best-effort
    arrive_dt: str
    airline: str         # carrier name as Google shows
    flight_no: str       # e.g. "LA8084" — may be empty if not surfaced
    duration_min: int


@dataclass
class MCItinerary:
    legs: list[MCLeg]
    price_usd: float
    total_duration_min: int
    n_stops: int                  # sum of stops across all legs
    booking_url: str              # deep link, "" if not bookable online
    source: str = "google_flights_multicity"
    raw_aria: str = ""            # debug — original aria-label


# ──────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ──────────────────────────────────────────────────────────────────────────


def _parse_duration(text: str) -> int:
    """'7 hr 30 min' / '2h 15m' / '90 min' → minutes."""
    h = re.search(r"(\d+)\s*(?:hr|h\b)", text)
    m = re.search(r"(\d+)\s*(?:min|m\b)", text)
    return int(h.group(1) if h else 0) * 60 + int(m.group(1) if m else 0)


def _parse_price(text: str) -> float | None:
    m = re.search(r"From\s+([\d,]+)\s+US\s+dollars", text, re.I)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _to_iso(time_str: str, date_str: str, fallback_year: int) -> str:
    """Combine '11:50 AM' + 'Thursday, June 18' (+ year fallback) → ISO local."""
    if not time_str:
        return ""
    t = time_str.strip().upper().replace(".", "")
    tm = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", t)
    if not tm:
        return ""
    hour = int(tm.group(1))
    minute = int(tm.group(2))
    ampm = tm.group(3)
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    dm = re.search(r"(\w+)\s+(\d{1,2})", date_str)
    if not dm:
        return ""
    mon = _MONTHS.get(dm.group(1).lower())
    if not mon:
        return ""
    day = int(dm.group(2))
    return f"{fallback_year:04d}-{mon:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"


# ──────────────────────────────────────────────────────────────────────────
# Page interaction
# ──────────────────────────────────────────────────────────────────────────


def _detect_block(page: Page) -> bool:
    url = page.url.lower()
    if "consent.google" in url or "/sorry/" in url:
        return True
    try:
        body_txt = page.locator("body").inner_text(timeout=2500).lower()
    except Exception:
        return False
    if "unusual traffic" in body_txt:
        return True
    if "before you continue" in body_txt and "cookies" in body_txt:
        return True
    return False


def _dismiss_consent(page: Page) -> bool:
    for label in ["Reject all", "I agree", "Accept all", "Aceptar todo", "Rechazar todo"]:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if btn.is_visible(timeout=1200):
                btn.click()
                page.wait_for_load_state("networkidle", timeout=8_000)
                return True
        except Exception:
            continue
    return False


def _fmt_date_for_input(iso_date: str) -> str:
    """ISO 'YYYY-MM-DD' → 'Jun 18, 2026' (the format the textbox accepts)."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return dt.strftime("%b %-d, %Y") if sys.platform != "win32" else dt.strftime("%b %#d, %Y")


def _fill_combobox(page: Page, combobox: Locator, code: str, *, slow: int = 80) -> None:
    """Click an airport combobox, type IATA, press Enter to accept the top option."""
    combobox.click()
    page.wait_for_timeout(250)
    # type into the focused inner search input
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(120)
    page.keyboard.type(code, delay=slow)
    # wait for the suggestions listbox
    try:
        page.wait_for_selector("ul[role='listbox'] li[role='option']", timeout=5_000)
    except PWTimeout:
        pass
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")
    page.wait_for_timeout(350)


def _fill_date(page: Page, date_textbox: Locator, iso_date: str) -> None:
    pretty = _fmt_date_for_input(iso_date)
    date_textbox.click()
    page.wait_for_timeout(220)
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(120)
    page.keyboard.type(pretty, delay=40)
    page.wait_for_timeout(250)
    page.keyboard.press("Enter")
    # sometimes opening the calendar requires a second Enter / Done click
    try:
        done = page.get_by_role("button", name=re.compile(r"^(Done|Listo)$", re.I)).first
        if done.is_visible(timeout=900):
            done.click()
    except Exception:
        pass
    page.wait_for_timeout(250)


def _switch_to_multicity(page: Page) -> None:
    box = page.get_by_role("combobox", name=re.compile("Change ticket type", re.I)).first
    box.click()
    page.wait_for_timeout(300)
    page.get_by_role("option", name=re.compile(r"^Multi-?city$", re.I)).first.click()
    page.wait_for_timeout(500)


def _leg_form_rows(page: Page) -> list[dict[str, Locator]]:
    """Return list of {origin, dest, date} locators for each leg row currently rendered."""
    # Each row contains two airport comboboxes + one date textbox.
    rows: list[dict[str, Locator]] = []
    origins = page.get_by_role("combobox", name=re.compile(r"Where from\??", re.I)).all()
    dests = page.get_by_role("combobox", name=re.compile(r"Where to\??", re.I)).all()
    dates = page.get_by_role("textbox", name=re.compile(r"^Departure$", re.I)).all()
    for i in range(min(len(origins), len(dests), len(dates))):
        rows.append({"origin": origins[i], "dest": dests[i], "date": dates[i]})
    return rows


def _ensure_n_legs(page: Page, n: int) -> None:
    """Make sure there are at least n leg-rows visible. Default is 2."""
    while True:
        rows = _leg_form_rows(page)
        if len(rows) >= n:
            return
        add = page.get_by_role("button", name=re.compile(r"^Add flight$", re.I)).first
        add.click()
        page.wait_for_timeout(450)


def _click_search(page: Page) -> None:
    for label in ["Search", "Done"]:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{label}$", re.I)).first
            if btn.is_visible(timeout=1000):
                btn.click()
                return
        except Exception:
            continue
    # fallback: press Enter on the last focused element
    page.keyboard.press("Enter")


# ──────────────────────────────────────────────────────────────────────────
# Direct TFS URL encoder (multi-city = trip type 3)
# ──────────────────────────────────────────────────────────────────────────


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_no: int, wire: int) -> bytes:
    return _varint((field_no << 3) | wire)


def _len_delim(field_no: int, payload: bytes) -> bytes:
    return _tag(field_no, 2) + _varint(len(payload)) + payload


def _str_field(field_no: int, s: str) -> bytes:
    b = s.encode("utf-8")
    return _len_delim(field_no, b)


def _varint_field(field_no: int, n: int) -> bytes:
    return _tag(field_no, 0) + _varint(n)


def _encode_leg(origin: str, dest: str, date_iso: str) -> bytes:
    """Encode a single leg as the inner protobuf TFS uses.

    Empirically observed shape (from a live SCL→GRU 2026-06-18 URL):
        2: date string ("2026-06-18")
       13: { 1: 1 (airport-type), 2: IATA }       # origin
       14: { 1: 1 (airport-type), 2: IATA }       # destination
    """
    inner_origin = _varint_field(1, 1) + _str_field(2, origin)
    inner_dest = _varint_field(1, 1) + _str_field(2, dest)
    return (
        _str_field(2, date_iso)
        + _len_delim(13, inner_origin)
        + _len_delim(14, inner_dest)
    )


def build_tfs_url(legs: Sequence[tuple[str, str, str]], *, adults: int = 1, cabin: int = 1) -> str:
    """Build https://www.google.com/travel/flights?tfs=<base64> URL for multi-city.

    cabin: 1=economy, 2=premium economy, 3=business, 4=first.
    """
    payload = b""
    payload += _varint_field(1, 28)   # ?? observed constant
    payload += _varint_field(2, 1)    # version?
    for o, d, dt in legs:
        payload += _len_delim(3, _encode_leg(o, d, dt))
    payload += _varint_field(8, cabin)
    payload += _varint_field(9, adults)
    payload += _varint_field(14, 1)
    # Field 16: max-stops blob — "any number of stops" = field 1 varint = max int (all-1s)
    stops_blob = bytes([0x08]) + bytes([0xFF] * 9) + bytes([0x01])
    payload += _len_delim(16, stops_blob)
    payload += _varint_field(19, 3)   # trip type = multi-city
    tfs = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"https://www.google.com/travel/flights?tfs={tfs}&hl=en&curr=USD"


# ──────────────────────────────────────────────────────────────────────────
# Result parsing
# ──────────────────────────────────────────────────────────────────────────


def _parse_aria(
    aria: str,
    requested_legs: Sequence[tuple[str, str, str]],
) -> MCItinerary | None:
    """Parse a Google Flights *multi-city* result aria-label.

    The aria is structured as:
      "From 1124 US dollars total. 1 stop flight with LATAM. Leaves Arturo
       Merino Benítez International Airport at 11:50 AM on Thursday, June 18
       and arrives at São Paulo–Guarulhos International Airport at 7:20 PM on
       Thursday, June 18. Total duration 5 hr 30 min. … 1 stop flight with
       LATAM. Leaves São Paulo–Guarulhos International Airport at 9:40 AM on
       Monday, June 29 and arrives … Select flight"
    """
    price = _parse_price(aria)
    if price is None:
        return None

    # Total stops across legs: sum of "N stop(s) flight" / "Nonstop flight"
    stop_pieces = re.findall(r"(\d+)\s+stops?\s+flight|\b(nonstop|direct)\s+flight\b", aria, re.I)
    n_stops_total = 0
    for s_num, s_word in stop_pieces:
        if s_num:
            n_stops_total += int(s_num)

    # Match each "Leaves … at TIME on DATE and arrives at … at TIME on DATE"
    leg_matches = list(re.finditer(
        r"(?:(\d+)\s+stops?\s+flight|\b(nonstop|direct)\s+flight\b)?"
        r"(?:\s+with\s+(?P<airline>[A-Z][\w&\-'’\s,]+?))?"
        r"\.\s*Leaves\s+(?P<o_air>.+?)\s+at\s+(?P<o_time>\d{1,2}:\d{2}\s*(?:AM|PM)?)\s+on\s+(?P<o_date>\w+,\s+\w+\s+\d{1,2})"
        r"\s+and\s+arrives\s+at\s+(?P<d_air>.+?)\s+at\s+(?P<d_time>\d{1,2}:\d{2}\s*(?:AM|PM)?)\s+on\s+(?P<d_date>\w+,\s+\w+\s+\d{1,2})",
        aria,
    ))
    if not leg_matches:
        return None

    # Per-leg durations may follow each segment as "Total duration X hr Y min"
    dur_matches = re.findall(r"Total\s+duration\s+(\d+\s*hr(?:\s+\d+\s*min)?|\d+\s*min)", aria, re.I)

    legs: list[MCLeg] = []
    for i, lm in enumerate(leg_matches):
        airline = (lm.group("airline") or "").strip(" ,.")
        airline = re.split(r"\s+Operated by\b", airline)[0].strip(" ,.")
        req = requested_legs[i] if i < len(requested_legs) else (None, None, None)
        year = int(req[2][:4]) if req and req[2] else datetime.utcnow().year
        depart_iso = _to_iso(lm.group("o_time"), lm.group("o_date"), year)
        arrive_iso = _to_iso(lm.group("d_time"), lm.group("d_date"), year)
        leg_dur = _parse_duration(dur_matches[i]) if i < len(dur_matches) else 0
        legs.append(MCLeg(
            origin=req[0] or "",
            dest=req[1] or "",
            depart_dt=depart_iso,
            arrive_dt=arrive_iso,
            airline=airline,
            flight_no="",
            duration_min=leg_dur,
        ))

    total_dur = sum(l.duration_min for l in legs)
    return MCItinerary(
        legs=legs,
        price_usd=price,
        total_duration_min=total_dur,
        n_stops=n_stops_total,
        booking_url="",
        raw_aria=aria,
    )


# ──────────────────────────────────────────────────────────────────────────
# Booking URL extraction
# ──────────────────────────────────────────────────────────────────────────


_NOT_AVAILABLE_PATTERNS = re.compile(
    r"not\s+available\s+to\s+book|unable\s+to\s+book|no\s+booking\s+options|"
    r"book\s+with\s+the\s+airline\s+directly\s+only|"
    r"this\s+itinerary\s+can.?t\s+be\s+booked",
    re.I,
)


def _capture_booking_url(page: Page, card: Locator, timeout_ms: int = 6_000) -> str:
    """Click a result card and read the first 'Book on …' / 'Book with …' link."""
    try:
        card.click(timeout=3_000)
    except Exception:
        return ""
    page.wait_for_timeout(1_300)

    # If a sub-list of legs is shown (one card per leg in multi-city), Google
    # asks the user to click "Select flight" once per leg. To grab the booking
    # link in a single shot, look for the "Book with"/"Book on" buttons on the
    # current detail view.
    end_ts = time.time() + timeout_ms / 1000.0
    while time.time() < end_ts:
        # 1. If we hit a "no booking options" message → empty
        try:
            body_txt = page.locator("body").inner_text(timeout=900)
            if _NOT_AVAILABLE_PATTERNS.search(body_txt):
                return ""
        except Exception:
            pass

        # 2. Look for "Book on <agency>" / "Book with <carrier>" buttons & their hrefs
        for sel in [
            "a[aria-label*='Book on']",
            "a[aria-label*='Book with']",
            "a[data-test-id*='book']",
            "a[href*='/travel/clk']",
        ]:
            try:
                a = page.locator(sel).first
                if a.is_visible(timeout=400):
                    href = a.get_attribute("href") or ""
                    if href:
                        if href.startswith("/"):
                            href = "https://www.google.com" + href
                        return href
            except Exception:
                continue

        # 3. "Select flight" button for the next leg of multi-city
        try:
            nxt = page.get_by_role("button", name=re.compile(r"^Select flight$", re.I)).first
            if nxt.is_visible(timeout=400):
                nxt.click()
                page.wait_for_timeout(900)
                continue
        except Exception:
            pass

        page.wait_for_timeout(400)

    return ""


# ──────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────


def search_multicity(
    legs: Sequence[tuple[str, str, str]],
    *,
    headless: bool = True,
    timeout_ms: int = 45_000,
    max_results: int = 8,
    fetch_booking_urls: bool = True,
    debug_dir: Path | None = None,
) -> list[MCItinerary]:
    """Run a Google Flights multi-city search.

    Args:
      legs: list of (origin_iata, dest_iata, "YYYY-MM-DD") — 2–6 legs supported
      headless: True for background
      timeout_ms: overall page navigation timeout
      max_results: cap on how many cards to parse (defaults to 8)
      fetch_booking_urls: if True, click each card to pull the booking deep-link.
                          Disable to make the scrape ~10× faster.
      debug_dir: if provided, screenshots are dumped on failure paths.

    Returns:
      List of MCItinerary (possibly empty).

    Raises:
      GoogleBlocked if consent/CAPTCHA cannot be dismissed.
      ValueError if legs is empty or shape is wrong.
    """
    if not legs or len(legs) < 2:
        raise ValueError("Multi-city search requires ≥2 legs")
    for o, d, dt in legs:
        if not (isinstance(o, str) and isinstance(d, str) and isinstance(dt, str)):
            raise ValueError(f"Bad leg shape: {(o, d, dt)!r}")
        datetime.strptime(dt, "%Y-%m-%d")  # validates

    url = "https://www.google.com/travel/flights?hl=en&curr=USD"
    results: list[MCItinerary] = []

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
            page.wait_for_timeout(1500)

            if _detect_block(page):
                if not _dismiss_consent(page):
                    if debug_dir:
                        debug_dir.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(debug_dir / f"gf-multicity-blocked-{int(time.time())}.png"))
                    else:
                        page.screenshot(path=f"/tmp/gf-multicity-{int(time.time())}.png")
                    raise GoogleBlocked(f"Consent/CAPTCHA on {page.url}")

            # Switch ticket type → Multi-city
            _switch_to_multicity(page)

            # Ensure we have enough leg rows
            _ensure_n_legs(page, len(legs))

            # Fill each leg
            rows = _leg_form_rows(page)
            for i, ((origin, dest, date_iso), row) in enumerate(zip(legs, rows)):
                _fill_combobox(page, row["origin"], origin)
                _fill_combobox(page, row["dest"], dest)
                _fill_date(page, row["date"], date_iso)

            # Submit
            _click_search(page)

            # Wait for results to render
            try:
                page.wait_for_selector(
                    "[aria-label*='US dollars']",
                    timeout=25_000,
                )
            except PWTimeout:
                if _detect_block(page):
                    ts = int(time.time())
                    out = (debug_dir / f"gf-multicity-blocked-{ts}.png") if debug_dir else Path(f"/tmp/gf-multicity-{ts}.png")
                    page.screenshot(path=str(out))
                    raise GoogleBlocked(f"Blocked after submit at {page.url} (screenshot: {out})")
                if debug_dir:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    ts = int(time.time())
                    page.screenshot(path=str(debug_dir / f"no-results-{ts}.png"))
                    (debug_dir / f"no-results-{ts}.html").write_text(page.content())
                    (debug_dir / f"no-results-{ts}.url").write_text(page.url)
                return []

            # Expand "View more flights" if present
            for label_pat in ["View more flights", "Show more", "more flights"]:
                try:
                    btn = page.get_by_role("button", name=re.compile(label_pat, re.I)).first
                    if btn.is_visible(timeout=900):
                        btn.click()
                        page.wait_for_timeout(900)
                except Exception:
                    continue

            # Collect cards by aria-label (multi-city says "total." not "round trip total")
            cards = page.locator("[aria-label*='US dollars']").all()
            seen: set[str] = set()
            parsed: list[tuple[Locator, MCItinerary]] = []
            for card in cards:
                try:
                    aria = card.get_attribute("aria-label") or ""
                    if not aria or "US dollars" not in aria or aria in seen:
                        continue
                    seen.add(aria)
                    itin = _parse_aria(aria, legs)
                    if itin is None:
                        continue
                    parsed.append((card, itin))
                    if len(parsed) >= max_results:
                        break
                except Exception as e:
                    if debug_dir:
                        print(f"parse error: {e}", file=sys.stderr)
                    continue

            # Fetch booking URLs (slow — one click per result)
            if fetch_booking_urls:
                for card, itin in parsed:
                    try:
                        itin.booking_url = _capture_booking_url(page, card)
                    except Exception as e:
                        if debug_dir:
                            print(f"booking_url error: {e}", file=sys.stderr)
                        itin.booking_url = ""
                    # navigate back to the result list
                    try:
                        page.go_back(wait_until="domcontentloaded", timeout=8_000)
                        page.wait_for_selector("[aria-label*='US dollars']", timeout=8_000)
                        page.wait_for_timeout(500)
                    except Exception:
                        # if back nav fails, abort URL extraction for the rest
                        break

            results = [it for _, it in parsed]
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
# Smoke test
# ──────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="non-headless")
    ap.add_argument("--no-urls", action="store_true", help="skip booking-url click loop (fast)")
    ap.add_argument("--debug-dir", default="/tmp/flight-intel-debug-mc")
    args = ap.parse_args()

    smoke_legs = [
        ("SCL", "GRU", "2026-06-18"),
        ("GRU", "SCL", "2026-06-29"),
    ]
    print(f"Multi-city smoke: {smoke_legs}")
    t0 = time.time()
    try:
        res = search_multicity(
            smoke_legs,
            headless=not args.show,
            fetch_booking_urls=not args.no_urls,
            debug_dir=Path(args.debug_dir),
        )
    except GoogleBlocked as e:
        print(f"BLOCKED in headless: {e}", file=sys.stderr)
        print("Retrying with headless=False ...", file=sys.stderr)
        res = search_multicity(
            smoke_legs,
            headless=False,
            fetch_booking_urls=not args.no_urls,
            debug_dir=Path(args.debug_dir),
        )
    dt = time.time() - t0
    print(f"\nFinished in {dt:.1f}s — {len(res)} itineraries")
    for i, it in enumerate(res, 1):
        airlines = "/".join(l.airline or "?" for l in it.legs)
        print(f"  [{i}] USD {it.price_usd:.0f}  {airlines}  "
              f"stops={it.n_stops}  dur={it.total_duration_min}min  "
              f"book={'Y' if it.booking_url else '-'}")
    print(json.dumps([asdict(it) for it in res[:2]], indent=2, default=str))
