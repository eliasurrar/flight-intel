"""composer.py — Build multi-leg "independent tickets" itineraries from per-leg scrapes.

Strategy (Elias spec):
  Given (origin, dest, date_window):
    1. Pull all 1-stop & nonstop itineraries from Google Flights/Kayak (single ticket).
    2. ALSO: scrape each leg as independent search (origin → hub, hub → dest)
       across major regional hubs, and compose pairs respecting MIN_CONNECT_HR.
    3. Combine: sum prices, sum durations, count stops, attach booking links.
    4. Dedupe and rank.

This first cut supports:
  - Round-trip OR one-way
  - Hub list inferred from registry routes.json (top connecting airports)
  - MIN_CONNECT_HR default = 1.5 (configurable)

Phase 1 of F3 = just expose the composer skeleton with hub heuristic.
Phase 2 = wire to scrapers and dedupe.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

MIN_CONNECT_HR = 1.5
MAX_TOTAL_DURATION_HR = 36  # discard combinations > 36h end-to-end


# Top regional hub airports by region (manually curated, ranked by passenger volume).
# Used as candidate intermediate stops for independent-leg composition.
REGIONAL_HUBS = {
    "SA": ["GRU", "GIG", "EZE", "SCL", "BOG", "LIM", "MDE", "UIO", "PTY"],
    "CA": ["PTY", "SJO", "GUA", "SAL"],
    "CB": ["SDQ", "HAV", "MBJ", "NAS", "POS"],
    "NA": ["ATL", "ORD", "DFW", "LAX", "JFK", "MIA", "MEX", "YYZ", "IAH"],
    "EU": ["LHR", "CDG", "FRA", "MAD", "AMS", "FCO", "IST", "BCN", "MUC"],
    "AS": ["DXB", "DOH", "ICN", "NRT", "SIN", "HKG", "BKK", "KUL", "PEK", "PVG"],
    "OC": ["SYD", "AKL", "MEL", "BNE", "NAN"],
    "AF": ["JNB", "CAI", "ADD", "LOS", "NBO", "CMN"],
    "ME": ["DXB", "DOH", "AUH", "RUH", "JED"],
}


@dataclass
class CombinedItinerary:
    """Multi-leg trip composed from N independent tickets."""
    legs: list[dict]      # each: {origin, dest, depart_dt, arrive_dt, airline, ticket_n}
    total_price_usd: float
    total_duration_min: int
    n_stops: int
    n_tickets: int        # how many independent bookings
    booking_urls: list[str]
    sources: list[str]
    composition_type: str  # "single_ticket" | "split_2_legs" | "split_3_legs"


def _load_airports() -> dict:
    p = DATA / "airports.json"
    return json.loads(p.read_text()) if p.exists() else {}


def candidate_hubs(origin: str, dest: str, *, max_hubs: int = 8) -> list[str]:
    """Return likely connecting airports for origin → dest.

    Heuristic v1:
      - Origin region hubs (top 3)
      - Destination region hubs (top 3)
      - Continental hubs that span both (PTY, MIA, ATL, MAD for SA↔NA, etc.)
    Excludes origin/dest themselves.
    """
    airports = _load_airports()
    o_region = airports.get(origin, {}).get("region", "??")
    d_region = airports.get(dest, {}).get("region", "??")

    hubs: list[str] = []
    hubs.extend(REGIONAL_HUBS.get(o_region, [])[:4])
    hubs.extend(REGIONAL_HUBS.get(d_region, [])[:4])

    # Trans-regional hubs
    if {o_region, d_region} <= {"SA", "NA"}:
        hubs.extend(["PTY", "MIA", "BOG"])
    if {o_region, d_region} <= {"SA", "EU"}:
        hubs.extend(["MAD", "LIS"])
    if {o_region, d_region} <= {"NA", "EU"}:
        hubs.extend(["LHR", "CDG", "FRA"])

    # Dedup, drop origin/dest, cap
    seen = {origin, dest}
    out = []
    for h in hubs:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:max_hubs]


def compose_split_two(
    leg_a_options: list,
    leg_b_options: list,
    *,
    min_connect_hr: float = MIN_CONNECT_HR,
) -> list[CombinedItinerary]:
    """Given itineraries for legs A and B, return all valid combinations.

    leg_a/leg_b are lists of Itinerary objects from a scraper. We pair each
    A with each B if the arrival time of A + min_connect_hr ≤ departure of B.
    Returns combinations sorted by composite score (price + duration penalty).
    """
    combos: list[CombinedItinerary] = []
    for a in leg_a_options:
        for b in leg_b_options:
            # Time gap check (skip if depart_dt not parseable)
            arr_a = _parse_dt(getattr(a.legs[-1], "arrive_dt", ""))
            dep_b = _parse_dt(getattr(b.legs[0], "depart_dt", ""))
            if arr_a and dep_b:
                gap_hr = (dep_b - arr_a).total_seconds() / 3600
                if gap_hr < min_connect_hr:
                    continue
                # Discard if total > MAX_TOTAL_DURATION_HR
                dep_a = _parse_dt(a.legs[0].depart_dt) if a.legs[0].depart_dt else None
                total_hr = (arr_a - dep_a).total_seconds()/3600 if dep_a else 0
                if total_hr > MAX_TOTAL_DURATION_HR:
                    continue

            combo = CombinedItinerary(
                legs=[{**asdict(leg), "ticket_n": 1} for leg in a.legs]
                     + [{**asdict(leg), "ticket_n": 2} for leg in b.legs],
                total_price_usd=a.price_usd + b.price_usd,
                total_duration_min=a.total_duration_min + b.total_duration_min,
                n_stops=a.n_stops + b.n_stops + 1,  # +1 for the inter-ticket connection
                n_tickets=2,
                booking_urls=[a.booking_url, b.booking_url],
                sources=[a.source, b.source],
                composition_type="split_2_legs",
            )
            combos.append(combo)
    return combos


def _parse_dt(s: str) -> datetime | None:
    """Best-effort parse 'Thursday, October 8 7:20 AM' → datetime (assumed current/next year)."""
    if not s:
        return None
    try:
        # Add current year if missing
        today = datetime.now()
        # Try multiple formats
        for fmt in (
            "%A, %B %d %I:%M %p",
            "%A, %B %d %H:%M",
            "%a, %b %d %I:%M %p",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                dt = dt.replace(year=today.year)
                if dt < today:
                    dt = dt.replace(year=today.year + 1)
                return dt
            except ValueError:
                continue
    except Exception:
        return None
    return None


def rank(combinations: list[CombinedItinerary],
         *, price_weight: float = 1.0,
         duration_weight: float = 0.3,   # USD per hour of total trip
         stop_penalty: float = 50.0) -> list[CombinedItinerary]:
    """Rank by composite score: lower is better.

    score = price + duration_weight × (duration_hr) + stop_penalty × n_stops
    """
    def score(c: CombinedItinerary) -> float:
        return (c.total_price_usd
                + duration_weight * (c.total_duration_min / 60)
                + stop_penalty * c.n_stops)

    return sorted(combinations, key=score)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True)
    ap.add_argument("--dest", required=True)
    args = ap.parse_args()

    hubs = candidate_hubs(args.origin, args.dest)
    print(f"Candidate hubs for {args.origin} → {args.dest}: {hubs}")
