"""scrapers/kayak.py — Kayak flights scraper with multi-city support.

URL patterns:
  one-way:     https://www.kayak.com/flights/{O}-{D}/{DATE}
  round-trip:  https://www.kayak.com/flights/{O}-{D}/{DATE_OUT}/{DATE_BACK}
  multi-city:  https://www.kayak.com/flights/{O1}-{D1}/{DATE1}/{O2}-{D2}/{DATE2}/...

All append `?sort=price_a` for cheapest-first.

Returns the *combo* price Kayak displays for the whole itinerary (Kayak IS an
aggregator with combined pricing for multi-city when carriers offer it).
Deep-links to OTAs/airline pages are in each result card.

Anti-bot:
  Kayak is aggressive. We use UA + locale + viewport tuning and **retry with
  headful** if headless yields zero cards (Elias spec: try aggressively).
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Sequence

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class KayakBlocked(RuntimeError):
    pass


@dataclass
class KYLeg:
    origin: str
    dest: str
    depart_dt: str
    arrive_dt: str
    airline: str
    flight_no: str
    duration_min: int


@dataclass
class KYItinerary:
    legs: list[KYLeg]
    price_value: float
    price_display: str
    price_currency: str
    total_duration_min: int
    n_stops: int
    booking_url: str
    source: str = "kayak"
    raw_text: str = ""


# ──────────────────────────────────────────────────────────────────────────


def build_url(legs: Sequence[tuple[str, str, str]]) -> str:
    if not legs:
        raise ValueError("at least one leg required")
    segs = [f"{o}-{d}/{date}" for o, d, date in legs]
    return f"https://www.kayak.com/flights/{'/'.join(segs)}?sort=price_a"


# ──────────────────────────────────────────────────────────────────────────


_PRICE_RE = re.compile(
    r"(?P<sym>US\$|R\$|CLP|BRL|USD|\$|€|£)\s*(?P<num>[\d.,]+)"
)


def _parse_price(text: str) -> tuple[float | None, str, str]:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None, "", "USD"
    sym, num = m.group("sym"), m.group("num")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        num = num.replace(",", "") if len(num.split(",")[-1]) == 3 else num.replace(",", ".")
    try:
        val = float(num)
    except Exception:
        return None, m.group(0), "USD"
    cur_map = {"$": "USD", "US$": "USD", "USD": "USD", "R$": "BRL",
               "BRL": "BRL", "CLP": "CLP", "€": "EUR", "£": "GBP"}
    return val, m.group(0), cur_map.get(sym, sym)


_DUR_RE = re.compile(r"(?:(\d+)\s*h)\s*(?:(\d+)\s*m)?", re.I)


def _parse_duration(text: str) -> int:
    m = _DUR_RE.search(text or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


# ──────────────────────────────────────────────────────────────────────────


def _detect_block(page: Page) -> bool:
    try:
        body = page.locator("body").inner_text(timeout=2_500).lower()
    except Exception:
        return False
    if "are you a robot" in body or "are you human" in body:
        return True
    if "before you continue" in body and len(body) < 500:
        return True
    if "access denied" in body and "kayak" in body:
        return True
    return False


def _wait_for_results(page: Page, timeout_ms: int = 35_000) -> None:
    selectors = [
        'div[class*="resultWrapper"]',
        'div[class*="result-card"]',
        'div[id*="flight-result"]',
        '[data-resultid]',
        'div.nrc6',  # current main result class
        'div.Fxw9-result-item-container',
    ]
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                if page.locator(sel).first.is_visible(timeout=500):
                    return
            except Exception:
                pass
        # also accept presence of any "$ NNNN" inline
        try:
            body = page.locator("body").inner_text(timeout=1_500)
            if _PRICE_RE.search(body):
                return
        except Exception:
            pass
        page.wait_for_timeout(750)
    raise PWTimeout("kayak: results never appeared")


def _find_cards(page: Page):
    for sel in (
        '[data-resultid]',
        'div.nrc6',
        'div.Fxw9-result-item-container',
        'div[class*="resultWrapper"]',
    ):
        cards = page.locator(sel).all()
        if cards:
            return cards
    return []


def _extract_card(card, default_legs: Sequence[tuple[str, str, str]]) -> KYItinerary | None:
    try:
        txt = card.inner_text(timeout=2500)
    except Exception:
        return None
    val, raw, cur = _parse_price(txt)
    if val is None:
        return None

    # stops — count "nonstop" occurrences in raw text to detect round-trip with
    # 2 separate legs (each "nonstop"). For round-trip+2-leg-nonstop, this is fine;
    # for *combos* (e.g. "Sky SCL→GRU + LATAM GRU→FLN" stored as one quote),
    # Kayak sometimes still labels the whole combo "nonstop" if both legs are.
    # That's correct per-leg; the dashboard's sanity layer catches impossible
    # advertised "directo" routes (e.g. SCL→FLN nonstop). Keep parser simple here.
    if re.search(r"\bnonstop\b|\bdirect\b|\bdirecto\b", txt, re.I):
        n_stops = 0
    else:
        m = re.search(r"(\d+)\s+stops?", txt, re.I)
        n_stops = int(m.group(1)) if m else 0

    duration = _parse_duration(txt)

    # Parse airline(s) from the card text. Kayak shows them as lines like
    # "Turkish Airlines" or "LATAM, Gol" between the time and stops markers.
    airlines_seen: list[str] = []
    AIRLINE_HINTS = re.compile(
        r"\b(Turkish Airlines|LATAM|Latam|Gol|Azul|Avianca|JetSMART|JetSmart|"
        r"Copa|Sky|Aerom[ée]xico|United|American|Delta|Iberia|Air Europa|"
        r"KLM|Air France|Lufthansa|Aerol[íi]neas Argentinas|Plus Ultra|"
        r"Tap Air Portugal|Boliviana de Aviaci[óo]n|Paranair|Amaszonas)\b",
        re.I,
    )
    for m in AIRLINE_HINTS.finditer(txt):
        name = m.group(1).strip()
        if name not in airlines_seen:
            airlines_seen.append(name)
    airline_str = " / ".join(airlines_seen[:3])

    # try to find a click-through link
    booking_url = ""
    try:
        a = card.locator("a[href]").first
        if a.count():
            href = a.get_attribute("href")
            if href:
                booking_url = href if href.startswith("http") else f"https://www.kayak.com{href}"
    except Exception:
        pass

    # build leg shells from the planned legs (Kayak text parsing is brittle)
    legs = [KYLeg(
        origin=o, dest=d, depart_dt=date, arrive_dt="",
        airline=airline_str, flight_no="", duration_min=0,
    ) for o, d, date in default_legs]

    return KYItinerary(
        legs=legs,
        price_value=val,
        price_display=raw,
        price_currency=cur,
        total_duration_min=duration,
        n_stops=n_stops,
        booking_url=booking_url,
        raw_text=txt[:500],
    )


# ──────────────────────────────────────────────────────────────────────────


def _run(legs: Sequence[tuple[str, str, str]], headless: bool, debug_dir: Path | None,
         timeout_ms: int) -> list[KYItinerary]:
    url = build_url(legs)
    if debug_dir:
        print(f"[kayak] URL: {url}", file=sys.stderr)
    results: list[KYItinerary] = []
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
            page.wait_for_timeout(2_000)
            if _detect_block(page):
                if debug_dir:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(debug_dir / f"blocked-{int(time.time())}.png"))
                raise KayakBlocked("captcha/anti-bot wall")
            _wait_for_results(page, timeout_ms=timeout_ms)
            page.wait_for_timeout(2_500)
            cards = _find_cards(page)
            if debug_dir:
                print(f"[kayak] {len(cards)} card elements", file=sys.stderr)
            for c in cards[:30]:
                it = _extract_card(c, legs)
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


def search(
    legs: Sequence[tuple[str, str, str]],
    headless: bool = True,
    debug_dir: Path | None = None,
    timeout_ms: int = 45_000,
    retries: int = 2,
) -> list[KYItinerary]:
    """Multi-city Kayak search with retry. If headless yields nothing or blocks,
    retry headful once."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            res = _run(legs, headless=headless and attempt == 0, debug_dir=debug_dir,
                       timeout_ms=timeout_ms)
            if res:
                return res
            if debug_dir:
                print(f"[kayak] attempt {attempt + 1}: empty, retrying", file=sys.stderr)
        except KayakBlocked as e:
            last_err = e
            if debug_dir:
                print(f"[kayak] attempt {attempt + 1}: blocked → {e}", file=sys.stderr)
        except Exception as e:
            last_err = e
            if debug_dir:
                print(f"[kayak] attempt {attempt + 1}: error → {e}", file=sys.stderr)
        time.sleep(2 + attempt * 3)
    if last_err and isinstance(last_err, KayakBlocked):
        raise last_err
    return []


# ──────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--debug-dir", default="/tmp/flight-intel-debug-kayak")
    args = ap.parse_args()
    smoke = [
        ("SCL", "GRU", "2026-06-18"),
        ("GRU", "SCL", "2026-06-29"),
    ]
    print(f"Kayak smoke (multi-city / round-trip): {smoke}")
    print(f"URL: {build_url(smoke)}")
    t0 = time.time()
    res = search(smoke, headless=not args.show, debug_dir=Path(args.debug_dir))
    print(f"\nFinished {time.time()-t0:.1f}s — {len(res)} itineraries")
    for i, it in enumerate(res[:8], 1):
        print(f"  [{i}] {it.price_display}  stops={it.n_stops} dur={it.total_duration_min}min")
    if res:
        print(json.dumps(asdict(res[0]), indent=2, default=str))
