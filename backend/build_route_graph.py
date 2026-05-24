"""Route graph: aeropuertos OurAirports + rutas OpenFlights + overrides 2026.

Construye un grafo bidireccional de rutas plausibles (no precios). Optimiza
rutas via Dijkstra/A* SIN consultar precios. Después el composer pide precios
solo a las top-N rutas.

Sources:
- data/airports.json     (OurAirports, ~9k IATA)
- data/raw/openflights_routes.dat (OpenFlights 2014, ~67k rutas)
- data/routes_overrides.json (manual: agregar/quitar 2026)

Output:
- data/routes_graph.json  {airport: [{to, airlines:[], hops:1}, ...]}
- data/routes_meta.json
"""
from __future__ import annotations

import json
import csv
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
AIRPORTS_FP = ROOT / "data" / "airports.json"
OF_ROUTES_FP = ROOT / "data" / "raw" / "openflights_routes.dat"
OVERRIDES_FP = ROOT / "data" / "routes_overrides.json"
GRAPH_FP = ROOT / "data" / "routes_graph.json"
META_FP = ROOT / "data" / "routes_meta.json"


def load_airports() -> dict:
    return json.loads(AIRPORTS_FP.read_text())


def load_openflights_routes() -> list[tuple[str, str, str]]:
    """Return [(src_iata, dst_iata, airline_iata)] from OpenFlights."""
    out = []
    with open(OF_ROUTES_FP, newline="") as f:
        for row in csv.reader(f):
            if len(row) < 5:
                continue
            airline, _, src, _, dst = row[0], row[1], row[2], row[3], row[4]
            if not src or not dst or src == "\\N" or dst == "\\N":
                continue
            if len(src) != 3 or len(dst) != 3:
                continue
            out.append((src.upper(), dst.upper(), airline.upper()))
    return out


def load_overrides() -> dict:
    if not OVERRIDES_FP.exists():
        seed = {
            "_doc": "Edit routes_overrides.json to fix 2026 reality. add=new routes, remove=defunct ones.",
            "add": [
                {"src": "SCL", "dst": "GRU", "airlines": ["LA", "G3", "JA"]},
                {"src": "GRU", "dst": "SCL", "airlines": ["LA", "G3", "JA"]},
                {"src": "SCL", "dst": "GIG", "airlines": ["LA"]},
                {"src": "GIG", "dst": "SCL", "airlines": ["LA"]},
                {"src": "SCL", "dst": "EZE", "airlines": ["LA", "AR", "JA", "H2"]},
                {"src": "EZE", "dst": "SCL", "airlines": ["LA", "AR", "JA", "H2"]},
                {"src": "GIG", "dst": "ADZ", "airlines": ["AV", "LA", "CM"]},
                {"src": "ADZ", "dst": "GIG", "airlines": ["AV", "LA", "CM"]},
                {"src": "GRU", "dst": "ADZ", "airlines": ["AV", "CM"]},
                {"src": "ADZ", "dst": "GRU", "airlines": ["AV", "CM"]},
            ],
            "remove": [],
        }
        OVERRIDES_FP.write_text(json.dumps(seed, indent=2))
        return seed
    return json.loads(OVERRIDES_FP.read_text())


def build():
    airports = load_airports()
    valid_iata = set(airports.keys())
    print(f"[airports] {len(valid_iata)} valid IATA codes")

    raw = load_openflights_routes()
    print(f"[openflights] {len(raw)} raw route rows")

    graph: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    skipped = 0
    for src, dst, airline in raw:
        if src not in valid_iata or dst not in valid_iata:
            skipped += 1
            continue
        graph[src][dst].add(airline)

    print(f"[openflights] kept {sum(len(v) for v in graph.values())}, skipped {skipped} (unknown IATA)")

    overrides = load_overrides()
    added = 0
    removed = 0
    for edge in overrides.get("add", []):
        src, dst = edge["src"], edge["dst"]
        if src not in valid_iata or dst not in valid_iata:
            continue
        for a in edge.get("airlines", []):
            graph[src][dst].add(a)
        added += 1
    for edge in overrides.get("remove", []):
        src, dst = edge["src"], edge["dst"]
        if src in graph and dst in graph[src]:
            del graph[src][dst]
            removed += 1

    print(f"[overrides] +{added} routes, -{removed} routes")

    # Serialize
    out_graph = {
        src: [{"to": dst, "airlines": sorted(airs)} for dst, airs in dsts.items()]
        for src, dsts in graph.items()
    }
    GRAPH_FP.write_text(json.dumps(out_graph))

    # Hub detection
    out_degree = {src: len(dsts) for src, dsts in graph.items()}
    hubs = sorted(out_degree.items(), key=lambda x: -x[1])[:50]

    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "n_airports_in_graph": len(graph),
        "n_total_edges": sum(len(v) for v in graph.values()),
        "n_overrides_added": added,
        "n_overrides_removed": removed,
        "top_50_hubs": [{"iata": h, "destinations": d} for h, d in hubs],
        "sources": {
            "airports": "OurAirports (data/airports.json)",
            "routes_seed": "OpenFlights routes.dat (2014, jpatokal/openflights@master)",
            "routes_overrides": "data/routes_overrides.json (manual 2026 corrections)",
        },
    }
    META_FP.write_text(json.dumps(meta, indent=2))

    print(f"\n[graph] {len(graph)} airports, {meta['n_total_edges']} directed edges")
    print(f"[hubs] top 10: {[h for h, _ in hubs[:10]]}")
    print(f"[out] {GRAPH_FP.name} + {META_FP.name}")


if __name__ == "__main__":
    build()
