"""scrapers/google_flights_v2.py — Google Flights via fast_flights + URL builder.

Rationale (descubierto 2026-05-25): GF no entrega un "pack price" para multi-city —
solo para round-trip. Multi-city devuelve flights por leg independientes. Para
nuestro caso de uso:

  - Round-trip trips → fast_flights round-trip → 1 fila con precio combo
  - Multi-leg trips (3+ legs distintos) → scrape cada leg one-way y sumar
  - One-way (1 leg) → fast_flights one-way

Para cada trip generamos también la URL de Google Flights humana (UI/booking
entry) que es la misma que `fast_flights` consulta. Booking URLs por carrier
quedan implícitos en la página de GF; las extraemos sólo cuando el dashboard
las pida (lazy, vía Playwright).
"""
from __future__ import annotations

import base64
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Sequence

from fast_flights import FlightData, Passengers, get_flights, create_filter

# ──────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class LegOffer:
    """A single one-way flight option for a specific leg."""
    origin: str
    dest: str
    leg_date: str
    airline: str
    departure: str          # raw, e.g. "10:15 AM on Thu, Jun 18"
    arrival: str
    duration_min: int
    stops: int
    price_value: float | None
    price_currency: str     # USD / CLP / BRL / ...
    price_display: str      # raw "CLP 289703"
    is_best: bool


@dataclass
class TripQuote:
    """Quote for a full multi-leg trip, composed by summing best per-leg offers
    OR by a single round-trip query when trip has 2 reverse legs.
    """
    legs: list[LegOffer]
    total_price_value: float | None
    total_price_currency: str
    composition: str  # "round-trip" | "multi-city-sum" | "one-way"
    booking_url: str  # GF URL (human-readable search) — single ticket only when round-trip
    source: str = "google_flights"


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


_DURATION_RE = re.compile(r"(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?", re.I)


def _parse_duration(s: str) -> int:
    if not s:
        return 0
    m = _DURATION_RE.search(s)
    if not m:
        return 0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


_PRICE_RE = re.compile(r"([A-Z]{3}|US\$|R\$|\$|€|£)\s*([\d.,]+)")


def _parse_price(s: str) -> tuple[float | None, str]:
    if not s:
        return None, "USD"
    m = _PRICE_RE.search(s.replace("\xa0", " "))
    if not m:
        return None, "USD"
    sym, num = m.group(1), m.group(2)
    # normalize
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
        return None, sym
    curr_map = {"$": "USD", "US$": "USD", "R$": "BRL", "€": "EUR", "£": "GBP"}
    cur = curr_map.get(sym, sym)
    return val, cur


def _to_leg_offer(f, origin: str, dest: str, date_iso: str) -> LegOffer:
    val, cur = _parse_price(f.price or "")
    try:
        stops_int = int(f.stops) if f.stops is not None else 0
    except (TypeError, ValueError):
        stops_int = 0
    return LegOffer(
        origin=origin,
        dest=dest,
        leg_date=date_iso,
        airline=f.name or "",
        departure=f.departure or "",
        arrival=f.arrival or "",
        duration_min=_parse_duration(f.duration or ""),
        stops=stops_int,
        price_value=val,
        price_currency=cur,
        price_display=(f.price or "").replace("\xa0", " "),
        is_best=bool(getattr(f, "is_best", False)),
    )


# ──────────────────────────────────────────────────────────────────────────
# URL builders
# ──────────────────────────────────────────────────────────────────────────


def build_search_url(legs: Sequence[tuple[str, str, str]], trip: str | None = None) -> str:
    """Builds a Google Flights search URL using TFS payload via fast_flights' filter.

    legs: [(orig, dest, date_iso), ...]
    trip: "one-way" | "round-trip" | "multi-city" | None (auto)
    """
    if trip is None:
        if len(legs) == 1:
            trip = "one-way"
        elif (
            len(legs) == 2
            and legs[0][0] == legs[1][1]
            and legs[0][1] == legs[1][0]
        ):
            trip = "round-trip"
        else:
            trip = "multi-city"
    flight_data = [FlightData(date=d, from_airport=o, to_airport=dst) for o, dst, d in legs]
    f = create_filter(
        flight_data=flight_data,
        trip=trip,
        passengers=Passengers(adults=1),
        seat="economy",
        max_stops=2,
    )
    tfs = f.as_b64().decode() if hasattr(f, "as_b64") else base64.urlsafe_b64encode(f.SerializeToString()).decode()
    return f"https://www.google.com/travel/flights?tfs={tfs}&hl=en&curr=USD"


# ──────────────────────────────────────────────────────────────────────────
# Search
# ──────────────────────────────────────────────────────────────────────────


def _search_one_leg(origin: str, dest: str, date_iso: str, max_stops: int = 2) -> list[LegOffer]:
    """One-way query for a single leg."""
    res = get_flights(
        flight_data=[FlightData(date=date_iso, from_airport=origin, to_airport=dest)],
        trip="one-way",
        passengers=Passengers(adults=1),
        seat="economy",
        max_stops=max_stops,
    )
    return [_to_leg_offer(f, origin, dest, date_iso) for f in (res.flights or [])]


def _search_roundtrip(legs: Sequence[tuple[str, str, str]], max_stops: int = 2) -> list[LegOffer]:
    """Round-trip query → returns the per-leg fragments (best combo first)."""
    o1, d1, dep1 = legs[0]
    o2, d2, dep2 = legs[1]
    res = get_flights(
        flight_data=[
            FlightData(date=dep1, from_airport=o1, to_airport=d1),
            FlightData(date=dep2, from_airport=o2, to_airport=d2),
        ],
        trip="round-trip",
        passengers=Passengers(adults=1),
        seat="economy",
        max_stops=max_stops,
    )
    # Round-trip mode: fast_flights returns flights for the first leg only,
    # with prices reflecting the combined RT cost. Second leg is implicit;
    # to get its details, the dashboard can re-query one-way.
    return [_to_leg_offer(f, o1, d1, dep1) for f in (res.flights or [])]


def quote_trip(
    legs: Sequence[tuple[str, str, str]],
    max_stops: int = 2,
    top_n: int = 5,
) -> list[TripQuote]:
    """Returns up to top_n TripQuote candidates for a trip.

    - 1 leg → one-way search.
    - 2 legs reverse → round-trip search (combo price).
    - N legs → multi-city: per-leg searches, return cartesian top-N by cheapest sum.
    """
    if not legs:
        return []

    booking_url = build_search_url(legs)

    if len(legs) == 1:
        o, d, dep = legs[0]
        offers = _search_one_leg(o, d, dep, max_stops=max_stops)[:top_n]
        return [
            TripQuote(
                legs=[off],
                total_price_value=off.price_value,
                total_price_currency=off.price_currency,
                composition="one-way",
                booking_url=booking_url,
            )
            for off in offers
        ]

    if (
        len(legs) == 2
        and legs[0][0] == legs[1][1]
        and legs[0][1] == legs[1][0]
    ):
        # round-trip combo price
        rt_offers = _search_roundtrip(legs, max_stops=max_stops)[:top_n]
        quotes: list[TripQuote] = []
        for rt in rt_offers:
            quotes.append(TripQuote(
                legs=[rt],  # only outbound has detail; price is combo
                total_price_value=rt.price_value,
                total_price_currency=rt.price_currency,
                composition="round-trip",
                booking_url=booking_url,
            ))
        return quotes

    # Multi-city: query each leg one-way and combine cheapest
    per_leg_offers: list[list[LegOffer]] = []
    for o, d, dep in legs:
        per_leg_offers.append(_search_one_leg(o, d, dep, max_stops=max_stops))

    # Cartesian product with limit (top 3 per leg)
    def combine(idx: int, current: list[LegOffer], price_sum: float | None) -> list[TripQuote]:
        if idx == len(per_leg_offers):
            return [TripQuote(
                legs=list(current),
                total_price_value=price_sum,
                total_price_currency=current[0].price_currency if current else "USD",
                composition="multi-city-sum",
                booking_url=booking_url,
            )]
        out = []
        for off in per_leg_offers[idx][:3]:
            new_sum = (price_sum or 0.0) + (off.price_value or 0.0) if off.price_value else None
            out += combine(idx + 1, current + [off], new_sum)
        return out

    quotes = combine(0, [], 0.0)
    quotes.sort(key=lambda q: (q.total_price_value or 1e12))
    return quotes[:top_n]


# ──────────────────────────────────────────────────────────────────────────
# Smoke
# ──────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import json
    smoke = [
        ("SCL", "GRU", "2026-06-18"),
        ("GRU", "SCL", "2026-06-29"),
    ]
    print(f"Smoke (round-trip): {smoke}")
    q = quote_trip(smoke, top_n=5)
    for i, t in enumerate(q, 1):
        leg = t.legs[0]
        print(f" [{i}] {t.total_price_currency} {t.total_price_value} "
              f"{leg.airline} stops={leg.stops} dur={leg.duration_min}min ({t.composition})")
    print(f"GF URL: {q[0].booking_url[:140] if q else '-'}…")

    smoke3 = [
        ("SCL", "GIG", "2026-06-18"),
        ("GIG", "FLN", "2026-06-20"),
        ("FLN", "SCL", "2026-06-25"),
    ]
    print(f"\nSmoke (multi-city 3 legs): {smoke3}")
    q3 = quote_trip(smoke3, top_n=3)
    for i, t in enumerate(q3, 1):
        legs = " · ".join(f"{l.airline} {l.origin}-{l.dest}" for l in t.legs)
        print(f" [{i}] sum~{t.total_price_currency} {t.total_price_value}  {legs}")
