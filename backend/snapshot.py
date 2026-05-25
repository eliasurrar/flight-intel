"""backend/snapshot.py — Orchestrator: run all sources for all pre-defined trips
and save the result as a JSON snapshot under data/snapshots/<date>/<key>.json.

For each trip we collect, in parallel-safe order:

  1. Google Flights (combo or per-leg sum)
  2. Booking Flights (multi-city)
  3. Kayak (multi-city)
  4. Per-airline links: for each airline mentioned by any of the above (or a
     curated default list for the region), we record the deep purchase URL
     per leg + best-effort price.

The output JSON is consumed by frontend/trips.html to render the dashboard.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trips import all_trips, Trip  # noqa: E402
from backend.scrapers import google_flights, booking, kayak, airlines  # noqa: E402

OUT_DIR = ROOT / "data" / "snapshots"


# Default airline candidates per region pair. We try these for per-leg deep-link
# enrichment when the aggregator does not surface a pack URL.
REGION_CARRIERS = {
    ("CL", "BR"): ["latam", "gol", "azul", "jetsmart"],
    ("BR", "CL"): ["latam", "gol", "azul", "jetsmart"],
    ("CL", "CL"): ["latam", "jetsmart", "skyairline"],
    ("BR", "BR"): ["latam", "gol", "azul"],
    ("CL", "CO"): ["latam", "avianca", "copa"],
    ("CO", "CL"): ["latam", "avianca", "copa"],
}

# IATA → country (minimal, covers the trips we care about)
IATA_COUNTRY = {
    "SCL": "CL", "GRU": "BR", "GIG": "BR", "FLN": "BR",
    "BOG": "CO", "MDE": "CO", "EZE": "AR", "AEP": "AR",
    "LIM": "PE", "PTY": "PA", "MIA": "US",
}


def _carrier_candidates(origin: str, dest: str) -> list[str]:
    co = IATA_COUNTRY.get(origin, "??")
    cd = IATA_COUNTRY.get(dest, "??")
    return REGION_CARRIERS.get((co, cd), ["latam"])


# ──────────────────────────────────────────────────────────────────────────


def snapshot_trip(trip: Trip, fetch_airline_prices: bool = False) -> dict:
    """Run all sources for one trip and return a serializable dict."""
    print(f"\n=== {trip.key}: {trip.name} ===", file=sys.stderr)
    legs_tuples = [(l.origin, l.destination, l.leg_date.isoformat()) for l in trip.legs]

    result: dict = {
        "trip_key": trip.key,
        "trip_name": trip.name,
        "legs": [
            {"origin": l.origin, "dest": l.destination, "date": l.leg_date.isoformat()}
            for l in trip.legs
        ],
        "snapshot_at": datetime.utcnow().isoformat() + "Z",
        "sources": {},
    }

    # --- Google Flights (with retry; fast_flights sometimes returns empty
    # transient "No flights found") ---
    gf = []
    gf_err = None
    for attempt in range(3):
        try:
            gf = google_flights.quote_trip(legs_tuples, top_n=5)
            if gf:
                break
            time.sleep(1.5)
        except Exception as e:
            gf_err = e
            time.sleep(2)
    t0 = time.time()
    if gf:
        result["sources"]["google_flights"] = {
            "ok": True,
            "duration_s": round(time.time() - t0, 1),
            "search_url": google_flights.build_search_url(legs_tuples),
            "quotes": [asdict(q) for q in gf],
        }
        print(f"  [GF] {len(gf)} quotes", file=sys.stderr)
    else:
        result["sources"]["google_flights"] = {
            "ok": False,
            "search_url": google_flights.build_search_url(legs_tuples),
            "error": str(gf_err)[:200] if gf_err else "no_flights",
        }
        print(f"  [GF] empty after retries", file=sys.stderr)

    # --- Booking (best-effort; SPA frequently hangs, we cap time) ---
    try:
        t0 = time.time()
        bk = booking.search_multicity(legs_tuples, headless=True, timeout_ms=12_000)
        result["sources"]["booking"] = {
            "ok": True,
            "duration_s": round(time.time() - t0, 1),
            "search_url": booking.build_url(legs_tuples),
            "quotes": [asdict(q) for q in bk[:10]],
            "note": "" if bk else "no_results_use_link",
        }
        print(f"  [Booking] {len(bk)} quotes in {time.time()-t0:.1f}s", file=sys.stderr)
    except Exception as e:
        result["sources"]["booking"] = {
            "ok": False,
            "search_url": booking.build_url(legs_tuples),
            "error": str(e)[:300],
        }
        print(f"  [Booking] error: {e}", file=sys.stderr)

    # --- Kayak ---
    try:
        t0 = time.time()
        ky = kayak.search(legs_tuples, headless=True)
        result["sources"]["kayak"] = {
            "ok": True,
            "duration_s": round(time.time() - t0, 1),
            "search_url": kayak.build_url(legs_tuples),
            "quotes": [asdict(q) for q in ky[:10]],
        }
        print(f"  [Kayak] {len(ky)} quotes in {time.time()-t0:.1f}s", file=sys.stderr)
    except kayak.KayakBlocked as e:
        result["sources"]["kayak"] = {"ok": False, "blocked": True, "error": str(e)}
        print(f"  [Kayak] BLOCKED: {e}", file=sys.stderr)
    except Exception as e:
        result["sources"]["kayak"] = {"ok": False, "error": str(e)[:300]}
        print(f"  [Kayak] error: {e}", file=sys.stderr)

    # --- Per-airline deep links per leg (links always; prices only if requested) ---
    per_leg_airlines = []
    for leg in trip.legs:
        candidates = _carrier_candidates(leg.origin, leg.destination)
        leg_block = {
            "origin": leg.origin, "dest": leg.destination, "date": leg.leg_date.isoformat(),
            "airlines": [],
        }
        for c in candidates:
            url = airlines.url_for(c, leg.origin, leg.destination, leg.leg_date.isoformat())
            entry: dict = {"airline": c, "url": url}
            if fetch_airline_prices and url:
                try:
                    q = airlines.fetch_price(c, leg.origin, leg.destination,
                                             leg.leg_date.isoformat(), headless=True)
                    entry.update({
                        "price_value": q.price_value,
                        "price_display": q.price_display,
                        "price_currency": q.price_currency,
                        "ok": q.ok,
                        "error": q.error,
                    })
                except Exception as e:
                    entry.update({"ok": False, "error": str(e)[:300]})
            leg_block["airlines"].append(entry)
        per_leg_airlines.append(leg_block)
    result["per_leg_airlines"] = per_leg_airlines

    return result


# ──────────────────────────────────────────────────────────────────────────


def run_all(trips=None, fetch_airline_prices: bool = False) -> Path:
    """Run snapshot for all trips. Returns the output directory."""
    trips = trips or all_trips()
    today = date.today().isoformat()
    out = OUT_DIR / today
    out.mkdir(parents=True, exist_ok=True)
    index: list[dict] = []
    for t in trips:
        try:
            d = snapshot_trip(t, fetch_airline_prices=fetch_airline_prices)
        except Exception as e:
            d = {"trip_key": t.key, "trip_name": t.name, "error": str(e)}
            print(f"  trip {t.key} fatal: {e}", file=sys.stderr)
        (out / f"{t.key}.json").write_text(json.dumps(d, indent=2, default=str))
        index.append({
            "key": t.key,
            "name": t.name,
            "legs": [{"origin": l.origin, "dest": l.destination, "date": l.leg_date.isoformat()}
                     for l in t.legs],
            "file": f"{t.key}.json",
        })
    (out / "index.json").write_text(json.dumps({
        "snapshot_at": datetime.utcnow().isoformat() + "Z",
        "trips": index,
    }, indent=2))
    # Also update latest pointer
    latest = OUT_DIR / "latest.json"
    latest.write_text(json.dumps({"date": today, "count": len(index)}, indent=2))
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--trip", help="Only run this trip key (default: all)")
    ap.add_argument("--limit", type=int, help="Only first N trips")
    ap.add_argument("--with-airline-prices", action="store_true",
                    help="Slow: actually scrape airline sites (default: only links)")
    args = ap.parse_args()
    trips = all_trips()
    if args.trip:
        trips = [t for t in trips if t.key == args.trip]
    if args.limit:
        trips = trips[: args.limit]
    out = run_all(trips, fetch_airline_prices=args.with_airline_prices)
    print(f"\nSnapshot written to: {out}", file=sys.stderr)
