"""scrapers/airlines.py — Per-airline deep-link builders + minimal price scrape.

Used when an aggregator (GF/Booking/Kayak) returns a multi-leg itinerary but
NO single booking link (because no OTA sells the pack). We then surface
per-leg, per-airline direct purchase links so Elias can build the trip
manually from the cheapest carrier per leg.

URL templates are reused from flight-monitor/adz_browser_base.py (proven to
load actual results pages). Where the per-airline site exposes a price quickly,
we fetch + parse via Playwright; else we just emit the deep link.
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Sequence

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


# ──────────────────────────────────────────────────────────────────────────
# URL templates per airline (one-way & round-trip when supported)
# ──────────────────────────────────────────────────────────────────────────


AIRLINE_URLS: dict[str, str] = {
    "latam": (
        "https://www.latamairlines.com/cl/es/ofertas-vuelos"
        "?origin={origin}&destination={destination}&outbound={dep}"
        "&adt=1&trip=OW"
    ),
    "latam_rt": (
        "https://www.latamairlines.com/cl/es/ofertas-vuelos"
        "?origin={origin}&destination={destination}&outbound={dep}"
        "&inbound={ret}&adt=1&trip=RT"
    ),
    "gol": (
        "https://b2c.voegol.com.br/compra/busca-parceiros"
        "?pv=br&tipo=OW&de={origin}&para={destination}&ida={dep}&adultos=1"
    ),
    "gol_rt": (
        "https://b2c.voegol.com.br/compra/busca-parceiros"
        "?pv=br&tipo=RT&de={origin}&para={destination}&ida={dep}&volta={ret}&adultos=1"
    ),
    "azul": (
        "https://www.voeazul.com.br/br/pt/home/compra/busca"
        "?TripType=1&Origin={origin}&Destination={destination}&DepartureDate={dep}"
        "&Cabin=Y&Adults=1"
    ),
    "azul_rt": (
        "https://www.voeazul.com.br/br/pt/home/compra/busca"
        "?TripType=2&Origin={origin}&Destination={destination}&DepartureDate={dep}"
        "&ReturnDate={ret}&Cabin=Y&Adults=1"
    ),
    "avianca": (
        "https://www.avianca.com/cl/es/search"
        "?origin={origin}&destination={destination}&departure={dep}&adults=1"
    ),
    "avianca_rt": (
        "https://www.avianca.com/cl/es/search"
        "?origin={origin}&destination={destination}&departure={dep}&return={ret}&adults=1"
    ),
    "jetsmart": (
        "https://jetsmart.com/cl/es/?from={origin}&to={destination}&departure={dep}&adults=1"
    ),
    "jetsmart_rt": (
        "https://jetsmart.com/cl/es/?from={origin}&to={destination}"
        "&departure={dep}&return={ret}&adults=1"
    ),
    "copa": (
        "https://www.copaair.com/es-us/booking"
        "?from={origin}&to={destination}&departureDate={dep}&adults=1"
    ),
    "copa_rt": (
        "https://www.copaair.com/es-us/booking"
        "?from={origin}&to={destination}&departureDate={dep}&returnDate={ret}&adults=1"
    ),
    "skyairline": (
        "https://www.skyairline.com/booking/select"
        "?o={origin}&d={destination}&dep={dep}&adults=1"
    ),
    "skyairline_rt": (
        "https://www.skyairline.com/booking/select"
        "?o={origin}&d={destination}&dep={dep}&ret={ret}&adults=1"
    ),
}


def url_for(airline: str, origin: str, dest: str, dep: str, ret: str | None = None) -> str:
    key = f"{airline.lower()}_rt" if ret else airline.lower()
    tmpl = AIRLINE_URLS.get(key) or AIRLINE_URLS.get(airline.lower())
    if not tmpl:
        return ""
    return tmpl.format(origin=origin, destination=dest, dep=dep, ret=ret or "")


# Heuristic mapping carrier name (as GF/Kayak shows it) → airline key
NAME_TO_KEY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\blatam\b", re.I), "latam"),
    (re.compile(r"\bgol\b", re.I), "gol"),
    (re.compile(r"\bazul\b", re.I), "azul"),
    (re.compile(r"\bavianca\b", re.I), "avianca"),
    (re.compile(r"\bjet\s*smart\b", re.I), "jetsmart"),
    (re.compile(r"\bcopa\b", re.I), "copa"),
    (re.compile(r"\bsky\b", re.I), "skyairline"),
]


def name_to_key(name: str) -> str | None:
    for pat, key in NAME_TO_KEY:
        if pat.search(name or ""):
            return key
    return None


# ──────────────────────────────────────────────────────────────────────────
# Quote
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class AirlineLegQuote:
    airline: str
    origin: str
    dest: str
    dep: str
    ret: str | None
    url: str
    price_value: float | None
    price_display: str
    price_currency: str
    ok: bool
    error: str = ""


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


def fetch_price(
    airline: str, origin: str, dest: str, dep: str, ret: str | None = None,
    headless: bool = True, timeout_ms: int = 25_000,
) -> AirlineLegQuote:
    url = url_for(airline, origin, dest, dep, ret)
    if not url:
        return AirlineLegQuote(airline, origin, dest, dep, ret, "",
                               None, "", "USD", ok=False, error="no_url_template")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 12_000))
                except Exception:
                    pass
                body = page.locator("body").inner_text(timeout=5_000)
                val, raw, cur = _parse_price(body)
                return AirlineLegQuote(
                    airline=airline, origin=origin, dest=dest, dep=dep, ret=ret,
                    url=url, price_value=val, price_display=raw, price_currency=cur,
                    ok=val is not None,
                    error="" if val is not None else "price_not_visible",
                )
            finally:
                browser.close()
    except Exception as e:
        return AirlineLegQuote(airline, origin, dest, dep, ret, url, None, "", "USD",
                               ok=False, error=f"{type(e).__name__}:{e}")


# ──────────────────────────────────────────────────────────────────────────
# Smoke
# ──────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--airline", default="latam")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--from", dest="orig", default="SCL")
    ap.add_argument("--to", dest="dest", default="GRU")
    ap.add_argument("--dep", default="2026-06-18")
    ap.add_argument("--ret", default="2026-06-29")
    args = ap.parse_args()
    print(f"URL: {url_for(args.airline, args.orig, args.dest, args.dep, args.ret)}")
    q = fetch_price(args.airline, args.orig, args.dest, args.dep, args.ret, headless=not args.show)
    print(asdict(q))
