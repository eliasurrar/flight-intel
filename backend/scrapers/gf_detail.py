"""scrapers/gf_detail.py — Google Flights detail scraper that extracts
physical segments (including intermediate stop airports) from a multi-city
itinerary card.

`fast_flights` only exposes headline (price, total duration, n_stops, carrier
name). It does NOT expose intermediate hubs. For Elias's use case — building
per-segment booking links and totalizing scraped per-airline prices — we need
the actual stop airports.

The page's aria-labels carry this information in a stable format:
  "A partir de 573 dólares estadounidenses (precio total de ida y vuelta).
   Vuelo con 1 escala de LATAM. Operado por Latam Airlines Brasil.
   Sale de Aeropuerto Internacional de Aracaju el viernes, octubre 9 a las 10:35.
   Llega a Aeropuerto Internacional Arturo Merino Benítez el viernes, octubre 9 a las 18:15.
   Duración total: 7 h 40 min.
   Esta escala (1 de 1) es una escala de 1 h 30 min en Aeropuerto Internacional
   de São Paulo-Guarulhos de São Paulo."

We parse these labels and synthesize physical-segment lists. Stop airport names
are mapped to IATA via a curated dict (extend as new airports appear).
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Sequence

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# ──────────────────────────────────────────────────────────────────────────
# Airport name → IATA (Google Flights uses long Spanish/Portuguese names)
# ──────────────────────────────────────────────────────────────────────────

# Substring matches in lowercase. Order matters: more specific first.
AIRPORT_NAME_TO_IATA: list[tuple[str, str]] = [
    ("são paulo-guarulhos", "GRU"),
    ("guarulhos", "GRU"),
    ("congonhas", "CGH"),
    ("viracopos", "VCP"),
    ("galeão", "GIG"),
    ("santos dumont", "SDU"),
    ("brasília", "BSB"),
    ("brasilia", "BSB"),
    ("confins", "CNF"),
    ("salgado filho", "POA"),
    ("recife", "REC"),
    ("guararapes", "REC"),
    ("salvador", "SSA"),
    ("luis eduardo", "SSA"),
    ("fortaleza", "FOR"),
    ("pinto martins", "FOR"),
    ("eduardo gomes", "MAO"),
    ("hercílio luz", "FLN"),
    ("florianópolis", "FLN"),
    ("florianopolis", "FLN"),
    ("afonso pena", "CWB"),
    ("curitiba", "CWB"),
    ("vitória", "VIX"),
    ("vitoria", "VIX"),
    ("aracaju", "AJU"),
    ("santa maria", "AJU"),
    ("natal", "NAT"),
    ("aluízio alves", "NAT"),
    ("manaus", "MAO"),
    ("belém", "BEL"),
    ("belem", "BEL"),
    # Chile
    ("arturo merino benítez", "SCL"),
    ("santiago", "SCL"),
    # Colombia
    ("el dorado", "BOG"),
    ("bogotá", "BOG"),
    ("bogota", "BOG"),
    ("josé maría córdova", "MDE"),
    ("rionegro", "MDE"),
    ("medellín", "MDE"),
    ("rafael núñez", "CTG"),
    ("cartagena", "CTG"),
    ("gustavo rojas pinilla", "ADZ"),
    ("san andrés", "ADZ"),
    ("san andres", "ADZ"),
    # Peru
    ("jorge chávez", "LIM"),
    ("lima", "LIM"),
    # Panama
    ("tocumen", "PTY"),
    ("panamá", "PTY"),
    ("panama", "PTY"),
    # Argentina
    ("ministro pistarini", "EZE"),
    ("ezeiza", "EZE"),
    ("aeroparque", "AEP"),
    ("jorge newbery", "AEP"),
    # US
    ("miami", "MIA"),
    ("john f. kennedy", "JFK"),
    ("kennedy", "JFK"),
    ("los angeles", "LAX"),
    ("o'hare", "ORD"),
    ("dallas", "DFW"),
    # Europe
    ("madrid-barajas", "MAD"),
    ("madrid", "MAD"),
    ("barcelona-el prat", "BCN"),
    ("el prat", "BCN"),
    ("charles de gaulle", "CDG"),
    ("schiphol", "AMS"),
    ("lisboa", "LIS"),
    ("lisbon", "LIS"),
    ("portela", "LIS"),
    ("fiumicino", "FCO"),
    ("istanbul", "IST"),
    ("estambul", "IST"),
    # Mexico
    ("benito juárez", "MEX"),
    ("ciudad de méxico", "MEX"),
]


def name_to_iata(name: str) -> str:
    n = (name or "").lower()
    for needle, iata in AIRPORT_NAME_TO_IATA:
        if needle in n:
            return iata
    return ""


# ──────────────────────────────────────────────────────────────────────────
# Data shapes
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class PhysicalSegment:
    origin: str
    dest: str
    airline: str
    operated_by: str = ""
    depart_time: str = ""  # e.g. "10:35"
    arrive_time: str = ""  # e.g. "18:15"
    layover_min: int = 0  # layover AFTER this segment (until next one)


@dataclass
class DetailItinerary:
    leg_origin: str
    leg_dest: str
    leg_date: str
    price_value: float | None
    price_currency: str
    price_display: str
    total_duration_min: int
    n_stops: int
    headline_airline: str
    operated_by: str
    segments: list[PhysicalSegment]
    aria_label: str = ""


# ──────────────────────────────────────────────────────────────────────────
# Parsing
# ──────────────────────────────────────────────────────────────────────────

_PRICE_RE = re.compile(r"A partir de\s+(?P<num>[\d.,]+)\s+(?P<cur>dólares|euros|libras|reales|pesos)")
_STOPS_RE = re.compile(r"Vuelo\s+con\s+(\d+)\s+escalas?|Vuelo\s+directo|Vuelo\s+sin\s+escalas")
# Captures both "1 escala de LATAM" and "2 escalas de LATAM y Aerolíneas Argentinas"
_AIRLINE_RE = re.compile(r"\d+\s+escalas?\s+de\s+([^.]+?)\.\s*Operado", re.IGNORECASE)
_AIRLINE_NOSTOPS_RE = re.compile(r"(?:directo|sin\s+escalas)\s+de\s+([^.]+?)\.\s*Operado", re.IGNORECASE)
_OPERATED_RE = re.compile(r"Operado por\s+([^.]+?)\.\s*Sale", re.IGNORECASE)
_DEPART_RE = re.compile(r"Sale de\s+(.+?)\s+el\s+\S+,\s+\S+\s+\d+\s+a las\s+(\d{1,2}:\d{2})", re.IGNORECASE)
_ARRIVE_RE = re.compile(r"Llega a\s+(.+?)\s+el\s+\S+,\s+\S+\s+\d+\s+a las\s+(\d{1,2}:\d{2})", re.IGNORECASE)
_DURATION_RE = re.compile(r"Duración\s+total:\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?", re.IGNORECASE)
_STOP_RE = re.compile(
    r"Esta\s+escala\s+\((\d+)\s+de\s+\d+\)\s+es\s+una\s+escala\s+de\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?\s+en\s+(.+?)(?=\.\s*(?:Esta\s+escala|Seleccionar|$))",
    re.IGNORECASE,
)

_CUR_MAP = {
    "dólares": "USD",
    "euros": "EUR",
    "libras": "GBP",
    "reales": "BRL",
    "pesos": "CLP",  # ambiguous — defaults to CLP for our use case
}


def _parse_duration_to_min(s: str) -> int:
    m = _DURATION_RE.search(s or "")
    if not m:
        return 0
    h = int(m.group(1) or 0)
    mi = int(m.group(2) or 0)
    return h * 60 + mi


def _parse_layover_to_min(h_str: str | None, m_str: str | None) -> int:
    h = int(h_str) if h_str else 0
    m = int(m_str) if m_str else 0
    return h * 60 + m


def parse_card(aria_label: str, leg_origin: str, leg_dest: str, leg_date: str) -> DetailItinerary | None:
    """Parse a single result card's aria-label into a DetailItinerary."""
    if not aria_label or "A partir de" not in aria_label:
        return None
    label = aria_label

    # Price
    m = _PRICE_RE.search(label)
    if not m:
        return None
    try:
        num = m.group("num").replace(".", "").replace(",", ".")
        price_value = float(num)
    except Exception:
        price_value = None
    price_currency = _CUR_MAP.get(m.group("cur").lower(), "USD")
    price_display = f"{m.group('num')} {m.group('cur')}"

    # Stops
    stops_m = _STOPS_RE.search(label)
    if stops_m and stops_m.group(1):
        n_stops = int(stops_m.group(1))
    else:
        n_stops = 0

    # Headline airline (e.g. "LATAM", "Avianca / Copa") + Operated by
    air_m = _AIRLINE_RE.search(label) or _AIRLINE_NOSTOPS_RE.search(label)
    headline_airline = (air_m.group(1).strip() if air_m else "").strip(".")
    op_m = _OPERATED_RE.search(label)
    operated_by = op_m.group(1).strip() if op_m else ""

    # Origin/dest from Sale/Llega
    dep_m = _DEPART_RE.search(label)
    arr_m = _ARRIVE_RE.search(label)
    if not dep_m or not arr_m:
        return None
    origin_name, depart_time = dep_m.group(1).strip(), dep_m.group(2)
    dest_name, arrive_time = arr_m.group(1).strip(), arr_m.group(2)
    origin_iata = name_to_iata(origin_name) or leg_origin
    dest_iata = name_to_iata(dest_name) or leg_dest

    # Duration
    total_duration_min = _parse_duration_to_min(label)

    # Stops: find all "Esta escala (k de N) es una escala de Xh Ym en NAME"
    stops_found = []
    for sm in _STOP_RE.finditer(label):
        idx, h, mi, ap_name = sm.group(1), sm.group(2), sm.group(3), sm.group(4)
        stops_found.append({
            "index": int(idx),
            "layover_min": _parse_layover_to_min(h, mi),
            "airport_name": ap_name.strip(),
            "airport_iata": name_to_iata(ap_name),
        })
    stops_found.sort(key=lambda x: x["index"])

    # Reconstruct physical segments. With N stops, there are N+1 segments.
    # We know: depart airport (origin_iata), final arrival (dest_iata), and
    # intermediate stops. We DO know depart_time (first segment) and
    # arrive_time (last segment), but not intermediate timings — which is fine
    # for our use case (linking out to airline pages).
    physical_seg_airports = [origin_iata] + [s["airport_iata"] for s in stops_found] + [dest_iata]
    segments = []
    # Operated_by may be a list of carriers for multi-airline itineraries, e.g.
    # "Latam Airlines Brasil, Latam Airlines Brasil" or "Avianca, Avianca, Gol".
    op_list = [s.strip() for s in re.split(r"[,/]", operated_by) if s.strip()]
    for i in range(len(physical_seg_airports) - 1):
        o = physical_seg_airports[i]
        d = physical_seg_airports[i + 1]
        carrier = op_list[i] if i < len(op_list) else (op_list[0] if op_list else headline_airline)
        layover = stops_found[i]["layover_min"] if i < len(stops_found) else 0
        segments.append(PhysicalSegment(
            origin=o, dest=d,
            airline=carrier,
            operated_by=carrier,
            depart_time=depart_time if i == 0 else "",
            arrive_time=arrive_time if i == len(physical_seg_airports) - 2 else "",
            layover_min=layover,
        ))

    return DetailItinerary(
        leg_origin=leg_origin,
        leg_dest=leg_dest,
        leg_date=leg_date,
        price_value=price_value,
        price_currency=price_currency,
        price_display=price_display,
        total_duration_min=total_duration_min,
        n_stops=n_stops,
        headline_airline=headline_airline,
        operated_by=operated_by,
        segments=segments,
        aria_label=label[:600],  # truncate for storage
    )


# ──────────────────────────────────────────────────────────────────────────
# Scraper
# ──────────────────────────────────────────────────────────────────────────


def fetch_itineraries(
    legs_tuples: Sequence[tuple[str, str, str]],
    *,
    headless: bool = True,
    timeout_ms: int = 25_000,
    top_n: int = 12,
) -> list[DetailItinerary]:
    """Open GF for `legs_tuples` and extract itineraries with physical segments.

    `legs_tuples`: list of (origin, dest, date_YYYY-MM-DD).
    Returns the top_n itineraries.
    """
    # Build URL — we can use fast_flights' TFS builder
    from fast_flights import FlightData, create_filter, Passengers
    flight_data = [FlightData(date=d, from_airport=o, to_airport=dd) for o, dd, d in legs_tuples]
    f = create_filter(
        flight_data=flight_data,
        trip="multi-city" if len(legs_tuples) > 1 else "one-way",
        passengers=Passengers(adults=1),
        seat="economy",
        max_stops=2,
    )
    url = f"https://www.google.com/travel/flights?tfs={f.as_b64().decode()}&hl=es&curr=USD"

    leg_origin, leg_dest, leg_date = legs_tuples[0][0], legs_tuples[-1][1], legs_tuples[0][2]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=UA, locale="es-CL", viewport={"width": 1400, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            # Wait for prices to render
            deadline = time.time() + 15
            while time.time() < deadline:
                txt = page.locator("body").inner_text(timeout=2000)
                if "A partir de" in txt or "dólares" in txt or "US$" in txt:
                    break
                page.wait_for_timeout(800)
            # Extract all aria-labels with "A partir de"
            labels = page.evaluate("""
                () => {
                    const out = [];
                    document.querySelectorAll('[aria-label]').forEach(el => {
                        const al = el.getAttribute('aria-label') || '';
                        if (al.length > 200 && al.includes('A partir de')) {
                            out.push(al);
                        }
                    });
                    return [...new Set(out)];
                }
            """)
        except PWTimeout:
            labels = []
        finally:
            browser.close()

    parsed: list[DetailItinerary] = []
    for lab in labels[:top_n]:
        it = parse_card(lab, leg_origin, leg_dest, leg_date)
        if it:
            parsed.append(it)
    return parsed


def to_dict(it: DetailItinerary) -> dict:
    return asdict(it)


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    its = fetch_itineraries([(args.origin, args.dest, args.date)], headless=args.headless)
    print(f"Found {len(its)} itineraries")
    for i, it in enumerate(its):
        print(f"\n[{i}] {it.price_display}  {it.headline_airline}  {it.n_stops}stop  {it.total_duration_min}min")
        for s in it.segments:
            print(f"     {s.airline:30s} {s.origin}→{s.dest}  dep={s.depart_time or '?'} arr={s.arrive_time or '?'} layover={s.layover_min}min")
