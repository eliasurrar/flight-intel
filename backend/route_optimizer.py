"""Route optimizer: encuentra top-N rutas O→D via grafo, SIN scrapear precios.

Algoritmo:
1. BFS hasta max_hops (default 2) sobre route_graph
2. Score por: hops (menos mejor), distance (haversine), hub bonus, airline overlap
3. Devuelve top-N rutas únicas: [{hops, path:[IATA,...], airlines_per_leg:[...], score}]

El composer luego pide precios solo a las top-N (mucho más eficiente que el
flujo actual donde scrapea por cada hub guess).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field

ROOT = Path(__file__).resolve().parents[1]
GRAPH_FP = ROOT / "data" / "routes_graph.json"
AIRPORTS_FP = ROOT / "data" / "airports.json"

_graph_cache = None
_airports_cache = None


def _load_graph() -> dict:
    global _graph_cache
    if _graph_cache is None:
        _graph_cache = json.loads(GRAPH_FP.read_text())
    return _graph_cache


def _load_airports() -> dict:
    global _airports_cache
    if _airports_cache is None:
        _airports_cache = json.loads(AIRPORTS_FP.read_text())
    return _airports_cache


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def route_distance_km(path: list[str]) -> float:
    apts = _load_airports()
    total = 0.0
    for a, b in zip(path[:-1], path[1:]):
        if a not in apts or b not in apts:
            return float("inf")
        total += haversine_km(apts[a]["lat"], apts[a]["lon"], apts[b]["lat"], apts[b]["lon"])
    return total


@dataclass(order=True)
class Route:
    score: float
    path: tuple[str, ...] = field(compare=False)
    airlines_per_leg: tuple[tuple[str, ...], ...] = field(compare=False)
    hops: int = field(compare=False)
    distance_km: float = field(compare=False)
    common_airline: str | None = field(compare=False, default=None)


def find_routes(origin: str, dest: str, max_hops: int = 2, top_n: int = 12,
                detour_factor: float = 2.5) -> list[Route]:
    """Find top-N candidate routes O→D.

    max_hops=1 → direct only
    max_hops=2 → direct + 1-stop
    max_hops=3 → up to 2-stops (expensive)

    detour_factor: discard paths whose total distance > direct_distance * factor.
    """
    graph = _load_graph()
    apts = _load_airports()
    origin, dest = origin.upper(), dest.upper()

    if origin not in apts or dest not in apts:
        return []

    direct_km = haversine_km(apts[origin]["lat"], apts[origin]["lon"],
                             apts[dest]["lat"], apts[dest]["lon"])
    max_total_km = direct_km * detour_factor if direct_km > 0 else float("inf")

    results: list[Route] = []
    seen_paths: set[tuple[str, ...]] = set()

    # BFS up to max_hops
    # frontier: [(current_path, airlines_per_leg_so_far)]
    frontier = [((origin,), ())]
    for hop in range(max_hops):
        next_frontier = []
        for path, airs_per_leg in frontier:
            current = path[-1]
            for edge in graph.get(current, []):
                nxt = edge["to"]
                if nxt in path:
                    continue  # no loops
                new_path = path + (nxt,)
                new_airs = airs_per_leg + (tuple(edge["airlines"]),)
                if nxt == dest:
                    d = route_distance_km(list(new_path))
                    if d <= max_total_km:
                        key = new_path
                        if key not in seen_paths:
                            seen_paths.add(key)
                            # Score: lower is better
                            # base hop penalty + distance ratio + airline continuity bonus
                            hops = len(new_path) - 1
                            common_airlines = set(new_airs[0])
                            for leg_airs in new_airs[1:]:
                                common_airlines &= set(leg_airs)
                            common = next(iter(common_airlines), None) if common_airlines else None
                            score = (
                                hops * 100
                                + (d / direct_km if direct_km > 0 else 1) * 10
                                - (50 if common else 0)
                            )
                            results.append(Route(
                                score=score, path=new_path, airlines_per_leg=new_airs,
                                hops=hops, distance_km=d, common_airline=common,
                            ))
                next_frontier.append((new_path, new_airs))
        frontier = next_frontier

    results.sort()
    return results[:top_n]


def find_routes_json(origin: str, dest: str, max_hops: int = 2, top_n: int = 12) -> list[dict]:
    routes = find_routes(origin, dest, max_hops=max_hops, top_n=top_n)
    return [
        {
            "path": list(r.path),
            "hops": r.hops,
            "distance_km": round(r.distance_km, 1),
            "airlines_per_leg": [list(a) for a in r.airlines_per_leg],
            "common_airline": r.common_airline,
            "score": round(r.score, 2),
        }
        for r in routes
    ]


if __name__ == "__main__":
    import sys
    o = sys.argv[1] if len(sys.argv) > 1 else "GIG"
    d = sys.argv[2] if len(sys.argv) > 2 else "ADZ"
    mh = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    print(f"Routes {o} → {d} (max_hops={mh}):")
    for r in find_routes_json(o, d, max_hops=mh, top_n=15):
        common = f" via {r['common_airline']}" if r["common_airline"] else ""
        print(f"  {'→'.join(r['path']):<25} {r['hops']}h {r['distance_km']:>6.0f}km  score={r['score']:>7.1f}{common}")
