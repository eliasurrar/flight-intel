"""sanity.py — physical sanity checks for scraped flight quotes.

Scrapers (especially Kayak) sometimes mis-parse cards and emit "nonstop"
with a duration that's physically impossible for the route. Example seen
2026-05-25: Kayak claimed "Sky Airline SCL→FLN nonstop 3h15m" — Sky doesn't
fly to Brazil at all, and SCL→FLN great-circle is ~3300 km (min ~4h30 nonstop
at jet cruise).

We compute a great-circle distance from an embedded IATA→lat/lon table for
the airports we actually use. If a "nonstop" quote's duration / distance
implies > 950 km/h average ground speed (commercial jets cruise ~830-900 km/h
in still air, so anything > 950 incl. taxi/climb is suspect), flag it.

We do NOT drop the quote — we annotate it with `suspect_reasons` so the
dashboard can badge it.
"""
from __future__ import annotations

import math
from typing import Iterable

# Lat/lon for airports we actually use (IATA: (lat, lon))
# Expand as new trips come in.
AIRPORT_COORDS: dict[str, tuple[float, float]] = {
    # Chile
    "SCL": (-33.393, -70.786),
    # Brazil
    "GRU": (-23.435, -46.473),
    "GIG": (-22.810, -43.250),
    "FLN": (-27.670, -48.547),
    "BSB": (-15.870, -47.918),
    "CNF": (-19.624, -43.972),
    "REC": (-8.126, -34.923),
    "SSA": (-12.910, -38.331),
    "FOR": (-3.776, -38.532),
    "AJU": (-10.984, -37.070),
    "VIX": (-20.258, -40.286),
    "POA": (-29.994, -51.171),
    "CWB": (-25.529, -49.176),
    "MAO": (-3.039, -60.050),
    "NAT": (-5.768, -35.376),
    # Colombia
    "BOG": (4.701, -74.147),
    "MDE": (6.165, -75.423),
    "CTG": (10.443, -75.513),
    "ADZ": (12.583, -81.711),
    # Peru
    "LIM": (-12.022, -77.114),
    # Panama
    "PTY": (9.071, -79.383),
    # Argentina
    "EZE": (-34.822, -58.535),
    "AEP": (-34.559, -58.416),
    # Mexico
    "MEX": (19.436, -99.072),
    # USA
    "MIA": (25.793, -80.290),
    "JFK": (40.640, -73.779),
    "LAX": (33.943, -118.408),
    # Europe
    "MAD": (40.472, -3.561),
    "CDG": (49.010, 2.548),
    "AMS": (52.309, 4.764),
    "LIS": (38.781, -9.136),
    "FCO": (41.804, 12.251),
    # Turkey hub
    "IST": (41.275, 28.752),
    # Ecuador, Uruguay, Paraguay, Bolivia
    "UIO": (-0.129, -78.358),
    "MVD": (-34.838, -56.030),
    "ASU": (-25.240, -57.519),
    "VVI": (-17.645, -63.135),
}


def great_circle_km(o: str, d: str) -> float | None:
    """Great-circle distance in km. None if either airport unknown."""
    a = AIRPORT_COORDS.get(o.upper())
    b = AIRPORT_COORDS.get(d.upper())
    if not a or not b:
        return None
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def min_nonstop_minutes(km: float) -> int:
    """Minimum plausible nonstop duration for `km` (incl. taxi+climb+descent).

    Assumes 820 km/h block speed (conservative — typical narrow-body avg)
    plus 30 min for taxi/climb/descent overhead.
    """
    return int(round(km / 820.0 * 60.0)) + 30


# Routes we *know* are not served nonstop by any commercial carrier (operator gap).
# Lookups are bidirectional (we check both directions).
KNOWN_NO_DIRECT_PAIRS: set[frozenset[str]] = {
    frozenset({"SCL", "FLN"}),
    frozenset({"SCL", "AJU"}),
    frozenset({"SCL", "ADZ"}),
    frozenset({"SCL", "REC"}),
    frozenset({"SCL", "NAT"}),
    frozenset({"SCL", "SSA"}),
    frozenset({"SCL", "BSB"}),
    frozenset({"SCL", "CNF"}),
    frozenset({"SCL", "POA"}),
    frozenset({"SCL", "FOR"}),
    frozenset({"SCL", "VIX"}),
    frozenset({"GIG", "FLN"}),  # exists seasonally; mostly via GRU
    frozenset({"GRU", "ADZ"}),
}


# Carrier serviceability heuristics — if quote claims `airline` operates `O→D`
# nonstop but the carrier doesn't serve one of the endpoints, flag it.
# Compact: airline → set of countries it does NOT serve (anything else is plausible).
AIRLINE_NEVER_SERVES_COUNTRY: dict[str, set[str]] = {
    "sky airline": {"BR"},   # Sky Airline (Chile) does not fly to Brazil
    "sky": {"BR"},
    "jetsmart": {"PA", "CO_ADZ"},  # JetSMART doesn't fly to PTY or ADZ
}

AIRPORT_COUNTRY: dict[str, str] = {
    "SCL": "CL",
    "GRU": "BR", "GIG": "BR", "FLN": "BR", "BSB": "BR", "CNF": "BR",
    "REC": "BR", "SSA": "BR", "FOR": "BR", "AJU": "BR", "VIX": "BR",
    "POA": "BR", "CWB": "BR", "MAO": "BR", "NAT": "BR",
    "BOG": "CO", "MDE": "CO", "CTG": "CO", "ADZ": "CO",
    "LIM": "PE", "PTY": "PA",
    "EZE": "AR", "AEP": "AR",
    "MEX": "MX",
    "MIA": "US", "JFK": "US", "LAX": "US",
    "MAD": "ES", "CDG": "FR", "AMS": "NL", "LIS": "PT", "FCO": "IT",
    "IST": "TR",
    "UIO": "EC", "MVD": "UY", "ASU": "PY", "VVI": "BO",
}


def assess_quote(
    origin: str,
    dest: str,
    duration_min: int,
    n_stops: int,
    airline: str = "",
) -> dict:
    """Return dict with `suspect: bool`, `reasons: [str]`, `min_minutes`, `distance_km`."""
    reasons: list[str] = []
    km = great_circle_km(origin, dest)
    min_min = min_nonstop_minutes(km) if km else None

    if n_stops == 0:
        # speed check
        if km and duration_min:
            avg_kmh = km / (duration_min / 60.0)
            if avg_kmh > 950:
                reasons.append(
                    f"speed {avg_kmh:.0f}km/h exceeds jet cruise (route {km:.0f}km in {duration_min}min)"
                )
            if min_min and duration_min < min_min - 15:  # allow 15min slack
                reasons.append(f"duration {duration_min}min < physical min {min_min}min")
        # known-gap check
        if frozenset({origin.upper(), dest.upper()}) in KNOWN_NO_DIRECT_PAIRS:
            reasons.append("no carrier operates this route nonstop")
        # carrier serviceability check
        if airline:
            key = airline.lower().split("/")[0].strip()
            forbidden = AIRLINE_NEVER_SERVES_COUNTRY.get(key, set())
            for ap in (origin, dest):
                cty = AIRPORT_COUNTRY.get(ap.upper())
                if cty and cty in forbidden:
                    reasons.append(f"{airline} does not serve {cty} ({ap})")

    return {
        "suspect": bool(reasons),
        "reasons": reasons,
        "distance_km": int(km) if km else None,
        "min_nonstop_minutes": min_min,
    }


def annotate_quotes(quotes: Iterable[dict]) -> list[dict]:
    """In-place-ish: add `sanity` dict to each quote. Returns same list."""
    out = []
    for q in quotes:
        if not isinstance(q, dict):
            out.append(q)
            continue
        # Find the headline leg (first leg of the itinerary)
        legs = q.get("legs") or []
        if not legs:
            out.append(q)
            continue
        first = legs[0]
        airline = first.get("airline") or ""
        n_stops = first.get("stops")
        if n_stops is None:
            n_stops = q.get("n_stops", 0)
        duration = first.get("duration_min") or q.get("total_duration_min") or 0
        assessment = assess_quote(
            first.get("origin", ""),
            first.get("dest", ""),
            int(duration or 0),
            int(n_stops or 0),
            airline,
        )
        q["sanity"] = assessment
        out.append(q)
    return out


if __name__ == "__main__":
    # Quick CLI sanity test
    samples = [
        ("SCL", "FLN", 195, 0, "Sky Airline"),  # the bad one
        ("SCL", "GRU", 290, 0, "LATAM"),         # legit
        ("SCL", "ADZ", 360, 0, "JetSMART"),      # impossible (JetSMART doesn't go)
        ("GRU", "FLN", 75, 0, "GOL"),            # legit short hop
    ]
    for o, d, m, s, a in samples:
        r = assess_quote(o, d, m, s, a)
        print(f"{o}->{d} {m}min {s}stops {a:15s} → suspect={r['suspect']:1d}  {r['reasons']}")
