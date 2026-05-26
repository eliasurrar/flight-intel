"""price_cache.py — local TTL cache for per-segment airline prices.

A synthesized itinerary like AJU→GRU→BOG→ADZ touches 3 segments, and other
itineraries (AJU→GRU→BOG→ADZ, AJU→GIG→BOG→ADZ, AJU→BSB→BOG→ADZ) share the
BOG→ADZ leg. Without a cache we'd re-scrape the same (airline, O, D, date)
combo many times per snapshot.

Cache layout:
  ~/projects/flight-intel/data/price_cache/<airline>-<O>-<D>-<date>.json
Each file holds the AirlineQuote dict + `cached_at` ISO timestamp.
TTL default: 6h (prices move intraday but not faster than that for our purposes).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "price_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TTL_S = 6 * 3600


def _key(airline: str, origin: str, dest: str, date: str) -> Path:
    safe = f"{airline.lower()}-{origin.upper()}-{dest.upper()}-{date}.json"
    return CACHE_DIR / safe


def get(
    airline: str, origin: str, dest: str, date: str, ttl_s: int = DEFAULT_TTL_S
) -> dict | None:
    p = _key(airline, origin, dest, date)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    cached_at = d.get("cached_at")
    if not cached_at:
        return None
    try:
        ts = datetime.fromisoformat(cached_at.replace("Z", "+00:00"))
    except Exception:
        return None
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > ttl_s:
        return None
    d["_cache_age_s"] = int(age)
    return d


def put(airline: str, origin: str, dest: str, date: str, quote_obj: Any) -> None:
    if is_dataclass(quote_obj):
        d = asdict(quote_obj)
    elif isinstance(quote_obj, dict):
        d = dict(quote_obj)
    else:
        d = {"repr": str(quote_obj)}
    d["cached_at"] = datetime.now(timezone.utc).isoformat()
    p = _key(airline, origin, dest, date)
    p.write_text(json.dumps(d, indent=2, default=str))


def prune(max_age_s: int = 7 * 24 * 3600) -> int:
    """Delete cache files older than max_age. Returns deleted count."""
    now = time.time()
    n = 0
    for p in CACHE_DIR.glob("*.json"):
        if now - p.stat().st_mtime > max_age_s:
            p.unlink()
            n += 1
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", type=int, help="max age in seconds")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.prune:
        print(f"Pruned {prune(args.prune)} files")
    if args.list:
        for p in sorted(CACHE_DIR.glob("*.json")):
            print(f"  {p.name} ({p.stat().st_size}B, age {int(time.time()-p.stat().st_mtime)}s)")
