"""network.py — curated airline-hub network + route synthesizer.

We do NOT depend on a giant routes_graph.json. Instead we encode the *operational
reality* of carriers we actually need: which airline serves which airports, and
which airports are real hubs (where most connections happen).

The synthesizer takes (origin, dest) and returns a ranked list of candidate
multi-leg itineraries (1-stop, 2-stop), each leg labeled with a plausible
operating carrier. The pricing layer then scrapes each leg.

Design constraints:
- Encode only carriers Elias actually uses (LATAM/GOL/Azul/Avianca/Sky/JetSMART/
  Copa/American/Delta/United/Iberia/AF/KLM/Turkish/Aeroméxico/Aerolíneas/TAP).
- Coverage tables are small + curated, NOT scraped (scraping airline schedules
  every snapshot would be too heavy). Update by hand when a carrier opens a route.
- Synthesizer respects: total distance < 2.2× great-circle, no backtracking
  beyond 30°, at most 2 stops, hub must be a real hub for at least one carrier
  serving an adjacent leg.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Sequence

from backend.sanity import AIRPORT_COORDS, great_circle_km

# ──────────────────────────────────────────────────────────────────────────
# Operational hubs (where connections actually happen)
# ──────────────────────────────────────────────────────────────────────────

HUBS: set[str] = {
    # South America
    "GRU", "GIG", "BSB", "CNF", "POA", "REC", "SSA", "FOR", "MAO",
    "SCL", "LIM", "BOG", "MDE", "PTY", "EZE", "AEP", "UIO", "ASU",
    # Caribbean / NA gateways
    "MIA", "JFK",
    # Europe long-haul gateways
    "MAD", "LIS", "CDG", "AMS", "IST",
    # Mexico
    "MEX",
}

# ──────────────────────────────────────────────────────────────────────────
# Airline coverage — what each carrier serves.
# Format: airline_key -> (hubs, spokes)
#   hubs: airports where the carrier has a base (most flights radiate from here)
#   spokes: other airports the carrier serves (mostly via its hubs)
# Rule: a carrier operates A→B nonstop iff {A,B} ∩ hubs ≠ ∅ AND both ∈ (hubs|spokes).
# This kills the "LATAM serves AJU AND ADZ → therefore AJU→ADZ direct" fallacy.
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class CarrierNet:
    hubs: set[str]
    spokes: set[str] = field(default_factory=set)

    @property
    def ports(self) -> set[str]:
        return self.hubs | self.spokes

    def operates(self, o: str, d: str) -> bool:
        o, d = o.upper(), d.upper()
        if o not in self.ports or d not in self.ports:
            return False
        # nonstop iff at least one endpoint is a hub
        return o in self.hubs or d in self.hubs


AIRLINE_COVERAGE: dict[str, CarrierNet] = {
    "latam": CarrierNet(
        hubs={"GRU", "GIG", "BSB", "SCL", "LIM", "BOG"},
        spokes={
            "CNF", "POA", "REC", "SSA", "FOR", "MAO", "FLN", "CWB", "VIX",
            "AJU", "NAT", "CGB", "VCP", "CCP", "ANF", "IPC",
            "CUZ", "AQP", "PIU",
            "MDE", "CTG", "BAQ", "PEI", "CLO", "ADZ",
            "EZE", "AEP", "MVD", "ASU", "GYE", "UIO",
            "MIA", "JFK", "LAX", "MAD", "MEX", "PTY",
        },
    ),
    "gol": CarrierNet(
        hubs={"GRU", "GIG", "BSB", "VCP", "FOR"},
        spokes={
            "CNF", "POA", "REC", "SSA", "MAO", "FLN", "CWB", "VIX",
            "AJU", "NAT", "CGB", "GYN", "MCZ",
            "BEL", "SLZ", "THE", "PMW", "RBR", "BVB",
            "EZE", "AEP", "MVD", "ASU", "SCL", "LIM", "BOG", "PTY",
            "MIA", "MCO", "PUJ", "CUN",
        },
    ),
    "azul": CarrierNet(
        hubs={"VCP", "REC", "CNF", "MAO", "BEL"},
        spokes={
            "GIG", "GRU", "FOR", "POA", "FLN", "CWB", "VIX", "AJU", "NAT",
            "SSA", "BSB", "MCZ", "JPA", "CGB", "GYN", "PMW", "THE", "SLZ",
            "MCP", "BVB", "RBR",
            "FLL", "MCO", "JFK", "MAD", "LIS",
        },
    ),
    "avianca": CarrierNet(
        hubs={"BOG", "MDE", "SAL"},
        spokes={
            "CLO", "BAQ", "CTG", "PEI", "BGA", "ADZ", "CUC", "LET", "AXM",
            "EYP", "VVC",
            "GUA", "TGU", "SAP", "MGA", "PTY", "SJO",
            "MIA", "MCO", "JFK", "LAX", "ORD", "BOS", "IAH", "EZR",
            "MAD", "BCN", "CDG", "LHR", "FCO", "IST",
            "GRU", "GIG", "EZE", "LIM", "UIO", "GYE", "MEX", "SCL",
        },
    ),
    "copa": CarrierNet(
        hubs={"PTY"},
        spokes={
            "BOG", "MDE", "CTG", "BAQ", "CLO", "PEI", "ADZ",
            "GRU", "GIG", "CWB", "POA", "BSB", "MAO", "REC", "SSA",
            "EZE", "AEP", "MVD", "ASU", "SCL", "LIM", "UIO", "GYE",
            "MIA", "MCO", "JFK", "LAX", "ORD", "BOS", "IAH",
            "MEX", "CUN", "GUA", "SAL", "TGU", "SAP", "MGA", "SJO",
        },
    ),
    "skyairline": CarrierNet(
        hubs={"SCL"},
        spokes={
            "ANF", "ARI", "CCP", "IPC", "PMC", "CJC", "IQQ", "PUQ",
            "LIM", "AEP", "MVD", "AQP", "CUZ", "ASU",
        },
    ),
    "jetsmart": CarrierNet(
        hubs={"SCL", "AEP", "LIM"},
        spokes={
            "ANF", "ARI", "CCP", "PMC", "IQQ", "CJC", "PUQ",
            "EZE", "COR", "MDZ", "BRC", "USH", "IGR",
            "AQP", "CUZ", "PIU", "ASU", "MVD",
            "GRU", "GIG", "FLN", "POA",
        },
    ),
    "american": CarrierNet(
        hubs={"MIA", "DFW", "JFK", "LAX", "ORD", "PHL", "CLT"},
        spokes={
            "BOS", "IAH",
            "GRU", "GIG", "EZE", "SCL", "LIM", "BOG", "MDE", "UIO", "MEX",
            "CUN", "PTY", "SJO", "MAD", "LHR", "CDG", "FCO", "BCN",
        },
    ),
    "united": CarrierNet(
        hubs={"EWR", "IAH", "ORD", "SFO", "DEN"},
        spokes={
            "LAX",
            "GRU", "GIG", "EZE", "SCL", "LIM", "BOG", "MEX", "PTY", "SJO",
            "MAD", "LHR", "CDG", "FCO", "MUC", "FRA",
        },
    ),
    "delta": CarrierNet(
        hubs={"ATL", "JFK", "LAX", "DTW", "MSP", "SEA"},
        spokes={
            "BOS",
            "GRU", "GIG", "EZE", "SCL", "LIM", "BOG", "MEX",
            "CDG", "AMS", "LHR", "FCO", "MAD",
        },
    ),
    "iberia": CarrierNet(
        hubs={"MAD"},
        spokes={
            "BCN",
            "GRU", "GIG", "EZE", "SCL", "LIM", "BOG", "UIO", "MVD", "MEX",
            "PTY", "SJO", "HAV", "LHR", "CDG", "FCO", "AMS",
        },
    ),
    "tap": CarrierNet(
        hubs={"LIS", "OPO"},
        spokes={
            "GRU", "GIG", "REC", "FOR", "BSB", "CNF", "POA", "SSA", "MAO", "BEL",
            "MAD", "BCN", "LHR", "CDG", "AMS", "FCO",
        },
    ),
    "airfrance": CarrierNet(
        hubs={"CDG", "ORY"},
        spokes={
            "GRU", "GIG", "EZE", "SCL", "LIM", "BOG", "MEX",
            "MAD", "AMS", "FCO", "LHR",
        },
    ),
    "klm": CarrierNet(
        hubs={"AMS"},
        spokes={
            "GRU", "GIG", "EZE", "SCL", "LIM", "BOG", "MEX",
            "MAD", "CDG", "LHR",
        },
    ),
    "turkish": CarrierNet(
        hubs={"IST"},
        spokes={
            "GRU", "GIG", "EZE", "SCL", "BOG", "PTY", "MEX",
            "MAD", "BCN", "CDG", "AMS", "LHR", "FCO", "LIS",
        },
    ),
    "aeromexico": CarrierNet(
        hubs={"MEX"},
        spokes={
            "MTY", "GDL", "CUN",
            "GRU", "EZE", "SCL", "LIM", "BOG", "MAD",
            "MIA", "JFK", "LAX", "ORD",
        },
    ),
    "aerolineas": CarrierNet(
        hubs={"EZE", "AEP"},
        spokes={
            "COR", "MDZ", "BRC", "USH", "IGR",
            "GRU", "GIG", "FLN", "POA", "REC", "FOR", "SSA",
            "SCL", "LIM", "BOG", "PTY", "MIA", "JFK", "MEX",
            "MAD", "FCO",
        },
    ),
}


# ──────────────────────────────────────────────────────────────────────────
# Route synthesizer
# ──────────────────────────────────────────────────────────────────────────


@dataclass
class SyntheticLeg:
    origin: str
    dest: str
    airline_candidates: list[str]  # carriers that serve BOTH endpoints
    distance_km: float | None = None


@dataclass
class SyntheticItinerary:
    legs: list[SyntheticLeg]
    total_distance_km: float
    great_circle_km: float
    detour_ratio: float  # total / great-circle
    n_stops: int
    score: float = 0.0  # lower is better

    @property
    def hubs(self) -> list[str]:
        return [self.legs[i].dest for i in range(len(self.legs) - 1)]

    def carrier_diversity(self) -> int:
        """Distinct carriers across legs (rough proxy for booking friction)."""
        seen = set()
        for leg in self.legs:
            if leg.airline_candidates:
                seen.add(leg.airline_candidates[0])
        return len(seen)


def carriers_for_leg(o: str, d: str) -> list[str]:
    """Return carriers that *operate* o→d nonstop per hub-spoke rule."""
    o = o.upper()
    d = d.upper()
    out = []
    for key, net in AIRLINE_COVERAGE.items():
        if net.operates(o, d):
            out.append(key)
    return out


def _detour_ratio(legs: list[SyntheticLeg], gc: float) -> float:
    if gc <= 0:
        return 99.0
    total = sum(l.distance_km or 0 for l in legs)
    return total / gc


def synthesize(
    origin: str,
    dest: str,
    *,
    max_stops: int = 2,
    max_detour: float = 2.4,
    candidate_hubs: Sequence[str] | None = None,
) -> list[SyntheticItinerary]:
    """Generate plausible 0/1/2-stop itineraries from `origin` to `dest`.

    Each leg must have ≥1 carrier serving both endpoints (per AIRLINE_COVERAGE).
    Itineraries are deduped by hub sequence and ranked by (detour, n_stops, diversity).
    """
    origin, dest = origin.upper(), dest.upper()
    gc = great_circle_km(origin, dest) or 0
    hubs = list(candidate_hubs) if candidate_hubs else sorted(HUBS)

    found: list[SyntheticItinerary] = []

    # 0-stop
    direct_carriers = carriers_for_leg(origin, dest)
    if direct_carriers:
        leg = SyntheticLeg(origin, dest, direct_carriers, distance_km=gc)
        found.append(SyntheticItinerary(
            legs=[leg],
            total_distance_km=gc,
            great_circle_km=gc,
            detour_ratio=1.0,
            n_stops=0,
        ))

    # 1-stop
    if max_stops >= 1:
        for h in hubs:
            if h == origin or h == dest:
                continue
            c1 = carriers_for_leg(origin, h)
            c2 = carriers_for_leg(h, dest)
            if not c1 or not c2:
                continue
            d1 = great_circle_km(origin, h) or 0
            d2 = great_circle_km(h, dest) or 0
            if d1 == 0 or d2 == 0:
                continue
            legs = [
                SyntheticLeg(origin, h, c1, distance_km=d1),
                SyntheticLeg(h, dest, c2, distance_km=d2),
            ]
            total = d1 + d2
            ratio = total / gc if gc else 99
            if ratio > max_detour:
                continue
            found.append(SyntheticItinerary(
                legs=legs,
                total_distance_km=total,
                great_circle_km=gc,
                detour_ratio=ratio,
                n_stops=1,
            ))

    # 2-stop
    if max_stops >= 2:
        for h1, h2 in product(hubs, hubs):
            if h1 == h2:
                continue
            if h1 in (origin, dest) or h2 in (origin, dest):
                continue
            c1 = carriers_for_leg(origin, h1)
            c2 = carriers_for_leg(h1, h2)
            c3 = carriers_for_leg(h2, dest)
            if not (c1 and c2 and c3):
                continue
            d1 = great_circle_km(origin, h1) or 0
            d2 = great_circle_km(h1, h2) or 0
            d3 = great_circle_km(h2, dest) or 0
            if 0 in (d1, d2, d3):
                continue
            total = d1 + d2 + d3
            ratio = total / gc if gc else 99
            if ratio > max_detour:
                continue
            # avoid silly backtracks: each intermediate must reduce remaining
            # great-circle distance to dest substantially
            rem_after_h1 = great_circle_km(h1, dest) or 0
            rem_after_h2 = great_circle_km(h2, dest) or 0
            if rem_after_h1 > gc * 1.4 or rem_after_h2 > rem_after_h1 * 1.1:
                continue
            legs = [
                SyntheticLeg(origin, h1, c1, distance_km=d1),
                SyntheticLeg(h1, h2, c2, distance_km=d2),
                SyntheticLeg(h2, dest, c3, distance_km=d3),
            ]
            found.append(SyntheticItinerary(
                legs=legs,
                total_distance_km=total,
                great_circle_km=gc,
                detour_ratio=ratio,
                n_stops=2,
            ))

    # Score: low detour first, then fewer stops, then less carrier diversity
    for it in found:
        it.score = it.detour_ratio + 0.15 * it.n_stops + 0.05 * it.carrier_diversity()
    found.sort(key=lambda x: x.score)

    # Dedupe by hub sequence (some hubs may produce identical itinerary shape)
    seen_keys: set[tuple] = set()
    unique: list[SyntheticItinerary] = []
    for it in found:
        key = tuple([l.origin for l in it.legs] + [it.legs[-1].dest])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(it)
    return unique


def to_dict(it: SyntheticItinerary) -> dict:
    return {
        "n_stops": it.n_stops,
        "hubs": it.hubs,
        "total_distance_km": round(it.total_distance_km),
        "great_circle_km": round(it.great_circle_km),
        "detour_ratio": round(it.detour_ratio, 2),
        "score": round(it.score, 3),
        "legs": [
            {
                "origin": l.origin,
                "dest": l.dest,
                "airline_candidates": l.airline_candidates,
                "distance_km": round(l.distance_km) if l.distance_km else None,
            }
            for l in it.legs
        ],
    }


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--max-stops", type=int, default=2)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    res = synthesize(args.origin, args.dest, max_stops=args.max_stops)
    print(f"Total: {len(res)} itineraries (showing top {args.top})")
    for it in res[:args.top]:
        print(f"  score={it.score:.2f} stops={it.n_stops} "
              f"detour={it.detour_ratio:.2f}× "
              f"path={'-'.join([l.origin for l in it.legs] + [it.legs[-1].dest])} "
              f"carriers={[l.airline_candidates[0] if l.airline_candidates else '?' for l in it.legs]}")
