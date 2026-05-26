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
import re
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
from backend.scrapers import gf_detail  # noqa: E402
from backend import segments as seg_extractor  # noqa: E402
from backend import sanity  # noqa: E402
from backend import price_cache  # noqa: E402

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
    ("BR", "CO"): ["latam", "avianca", "gol", "copa"],
    ("CO", "BR"): ["latam", "avianca", "gol", "copa"],
    ("CO", "CO"): ["avianca", "latam", "satena"],
    ("CL", "AR"): ["latam", "jetsmart", "skyairline"],
    ("AR", "CL"): ["latam", "jetsmart", "skyairline"],
    ("BR", "AR"): ["latam", "gol", "azul"],
    ("AR", "BR"): ["latam", "gol", "azul"],
    ("CL", "PE"): ["latam", "skyairline", "jetsmart"],
    ("PE", "CL"): ["latam", "skyairline", "jetsmart"],
    ("CL", "PA"): ["copa", "latam"],
    ("PA", "CL"): ["copa", "latam"],
    ("CL", "US"): ["latam", "american", "united", "delta"],
    ("US", "CL"): ["latam", "american", "united", "delta"],
}

# IATA → country (cover the trips we care about + common hubs/stops)
IATA_COUNTRY = {
    # Chile
    "SCL": "CL", "IPC": "CL", "ANF": "CL", "PMC": "CL", "CCP": "CL",
    # Brasil
    "GRU": "BR", "GIG": "BR", "FLN": "BR", "AJU": "BR", "SSA": "BR",
    "BSB": "BR", "VCP": "BR", "CWB": "BR", "POA": "BR", "REC": "BR",
    "MAO": "BR", "FOR": "BR", "BEL": "BR", "GYN": "BR", "CGB": "BR",
    "NAT": "BR", "MCZ": "BR",
    # Colombia
    "BOG": "CO", "MDE": "CO", "ADZ": "CO", "CTG": "CO", "CLO": "CO", "BAQ": "CO",
    # Argentina
    "EZE": "AR", "AEP": "AR", "MDZ": "AR", "COR": "AR", "BRC": "AR",
    # Perú
    "LIM": "PE", "CUZ": "PE", "AQP": "PE",
    # Panamá
    "PTY": "PA",
    # USA
    "MIA": "US", "JFK": "US", "LAX": "US", "ORD": "US", "DFW": "US", "ATL": "US",
    # Uruguay
    "MVD": "UY",
    # Paraguay
    "ASU": "PY",
    # Bolivia
    "VVI": "BO", "LPB": "BO",
    # Ecuador
    "UIO": "EC", "GYE": "EC",
    # México
    "MEX": "MX", "CUN": "MX",
}


def _carrier_candidates(origin: str, dest: str) -> list[str]:
    co = IATA_COUNTRY.get(origin, "??")
    cd = IATA_COUNTRY.get(dest, "??")
    if (co, cd) in REGION_CARRIERS:
        return REGION_CARRIERS[(co, cd)]
    # Generic LATAM region fallback
    LATAM_REGION = {"CL", "BR", "CO", "AR", "PE", "PA", "UY", "PY", "BO", "EC", "MX"}
    if co in LATAM_REGION and cd in LATAM_REGION:
        return ["latam", "avianca", "gol", "copa"]
    return ["latam"]


# ──────────────────────────────────────────────────────────────────────────


def snapshot_trip(trip: Trip, fetch_airline_prices: bool = False,
                  scrape_segment_limit: int = 8) -> dict:
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
        # Strip ephemeral booking_url tokens (expire in <24h, return 403).
        # Frontend should use search_url as canonical "ver en Kayak" link.
        ky_dicts = []
        for q in ky[:10]:
            d = asdict(q)
            # Token-style /book/flight?code=... links expire fast; drop them.
            bu = d.get("booking_url", "")
            if "/book/flight?code=" in bu or bu == "":
                d["booking_url"] = kayak.build_url(legs_tuples)
                d["booking_url_note"] = "search_link (specific token expired/missing)"
            ky_dicts.append(d)
        result["sources"]["kayak"] = {
            "ok": True,
            "duration_s": round(time.time() - t0, 1),
            "search_url": kayak.build_url(legs_tuples),
            "pack_url": kayak.build_url(legs_tuples),
            "quotes": ky_dicts,
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

    # --- Unique directo segments discovered across all sources ---
    # Each entry: { origin, dest, date, airline, dep_time, arr_time, source,
    #               url (if we have an airline URL template),
    #               price_*, ok, error, scrape_status }
    try:
        unique_segs = seg_extractor.collect_all_segments(result)
        seg_rows = []
        for s in unique_segs:
            airline_key = _airline_key(s.airline)
            url = airlines.url_for(airline_key, s.origin, s.dest, s.date) if airline_key else ""
            seg_rows.append({
                "origin": s.origin, "dest": s.dest, "date": s.date,
                "airline": s.airline, "airline_key": airline_key,
                "dep_time": s.dep_time, "arr_time": s.arr_time,
                "source": s.source, "url": url,
                "price_value": None, "price_currency": "", "price_display": "",
                "scrape_status": "pending" if (url and airline_key) else "no_url",
                "screenshot_path": "",
                "error": "",
            })

        # Scrape per-segment prices when requested (headful + OCR fallback)
        if fetch_airline_prices:
            scrapeable = [r for r in seg_rows if r["scrape_status"] == "pending"][:scrape_segment_limit]
            print(f"  [segments] scraping {len(scrapeable)}/{len(seg_rows)} segments per-airline",
                  file=sys.stderr)
            from concurrent.futures import ThreadPoolExecutor, as_completed
            def _scrape_one(row):
                t0 = time.time()
                q = airlines.fetch_price(
                    row["airline_key"], row["origin"], row["dest"], row["date"],
                    headless=False, timeout_ms=30_000, vision_fallback=True,
                )
                row["price_value"] = q.price_value
                row["price_currency"] = q.price_currency
                row["price_display"] = q.price_display
                row["error"] = q.error
                row["scrape_status"] = "scraped" if q.ok else (
                    "blocked" if "block" in (q.error or "").lower()
                    else "no_flights" if q.error == "airline_no_flights"
                    else "ocr_failed" if "screenshot:" in (q.price_display or "")
                    else "failed"
                )
                if "screenshot:" in (q.price_display or ""):
                    row["screenshot_path"] = q.price_display.replace("screenshot:", "")
                print(f"    {row['airline_key']:10s} {row['origin']}→{row['dest']} {row['date']}: "
                      f"{row['scrape_status']} ({time.time()-t0:.1f}s) "
                      f"{row.get('price_display','')}", file=sys.stderr)
                return row
            with ThreadPoolExecutor(max_workers=2) as ex:
                # Browser launching is heavy; limit concurrency to avoid OOM
                futs = [ex.submit(_scrape_one, r) for r in scrapeable]
                for _ in as_completed(futs):
                    pass

        result["unique_segments"] = seg_rows
        n_scraped = sum(1 for r in seg_rows if r["scrape_status"] == "scraped")
        print(f"  [segments] {len(seg_rows)} unique · {n_scraped} with price", file=sys.stderr)
    except Exception as e:
        result["unique_segments"] = []
        print(f"  [segments] error: {e}", file=sys.stderr)

    # --- Sanity-annotate all source quotes (flag impossible "nonstops") ---
    n_suspect = 0
    for src in ("google_flights", "kayak", "booking"):
        block = result["sources"].get(src)
        if not isinstance(block, dict):
            continue
        quotes = block.get("quotes") or []
        sanity.annotate_quotes(quotes)
        n_suspect += sum(1 for q in quotes if q.get("sanity", {}).get("suspect"))
    if n_suspect:
        print(f"  [sanity] {n_suspect} suspect quotes flagged", file=sys.stderr)

    # --- GF detail per leg: extract physical segments + price each via airline scrape ---
    # This is the core flow: per leg, fetch the actual GF result cards (with
    # intermediate hubs), then for each unique physical segment go to the
    # operating carrier's site and scrape the price. Sum and present.
    gf_detail_per_leg = []
    try:
        for leg in trip.legs:
            t0 = time.time()
            itins = gf_detail.fetch_itineraries(
                [(leg.origin, leg.destination, leg.leg_date.isoformat())],
                headless=True, top_n=10,
            )
            # Build a set of unique (airline_key, O, D, date) jobs across all itineraries
            jobs: dict[tuple, dict] = {}
            for it in itins:
                for s in it.segments:
                    akey = _airline_key(s.airline)
                    if not akey:
                        continue
                    key = (akey, s.origin, s.dest, leg.leg_date.isoformat())
                    jobs.setdefault(key, {
                        "airline_key": akey,
                        "airline_name": s.airline,
                        "origin": s.origin,
                        "dest": s.dest,
                        "date": leg.leg_date.isoformat(),
                    })

            # Price each unique segment (cache + headful + OCR fallback)
            seg_prices: dict[tuple, dict] = {}
            if fetch_airline_prices and jobs:
                print(f"  [gf_detail {leg.origin}→{leg.destination}] "
                      f"{len(itins)} itins, scraping {len(jobs)} unique segments",
                      file=sys.stderr)
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _price_segment(key, params):
                    cached = price_cache.get(
                        params["airline_key"], params["origin"], params["dest"], params["date"]
                    )
                    if cached and cached.get("ok"):
                        return key, {
                            "ok": True,
                            "price_value": cached.get("price_value"),
                            "price_currency": cached.get("price_currency", ""),
                            "price_display": cached.get("price_display", ""),
                            "url": cached.get("url", ""),
                            "from_cache": True,
                            "cache_age_s": cached.get("_cache_age_s", 0),
                        }
                    try:
                        q = airlines.fetch_price(
                            params["airline_key"], params["origin"], params["dest"], params["date"],
                            headless=False, timeout_ms=30_000, vision_fallback=True,
                        )
                        price_cache.put(
                            params["airline_key"], params["origin"], params["dest"], params["date"], q
                        )
                        return key, {**asdict(q), "from_cache": False}
                    except Exception as e:
                        return key, {"ok": False, "error": str(e)[:200], "from_cache": False}

                with ThreadPoolExecutor(max_workers=3) as ex:
                    futs = [ex.submit(_price_segment, k, v) for k, v in jobs.items()]
                    for fut in as_completed(futs):
                        try:
                            k, r = fut.result()
                            seg_prices[k] = r
                        except Exception:
                            pass

            # Attach per-segment price info to each itinerary's segments
            itins_out = []
            for it in itins:
                segs_priced = []
                total_usd = 0.0
                n_priced = 0
                for s in it.segments:
                    akey = _airline_key(s.airline)
                    url = airlines.url_for(akey, s.origin, s.dest, leg.leg_date.isoformat()) if akey else ""
                    r = seg_prices.get((akey, s.origin, s.dest, leg.leg_date.isoformat()), {}) if akey else {}
                    usd = None
                    if r.get("ok") and r.get("price_value") is not None:
                        from backend.fx import convert
                        try:
                            usd = convert(r["price_value"], r.get("price_currency", "USD"), "USD")
                        except Exception:
                            usd = None
                    if usd is not None:
                        total_usd += usd
                        n_priced += 1
                    segs_priced.append({
                        "origin": s.origin,
                        "dest": s.dest,
                        "airline": s.airline,
                        "airline_key": akey,
                        "depart_time": s.depart_time,
                        "arrive_time": s.arrive_time,
                        "layover_min": s.layover_min,
                        "operated_by": s.operated_by,
                        "url": url or r.get("url", ""),
                        "price_value": r.get("price_value"),
                        "price_currency": r.get("price_currency", ""),
                        "price_display": r.get("price_display", ""),
                        "price_usd": round(usd, 2) if usd is not None else None,
                        "scrape_status": (
                            "cached" if r.get("from_cache") and r.get("ok")
                            else "scraped" if r.get("ok") and r.get("price_value") is not None
                            else "no_flights" if r.get("error") == "airline_no_flights"
                            else "blocked" if "block" in (r.get("error") or "").lower()
                            else "failed" if r
                            else ("pending" if fetch_airline_prices else "skipped")
                        ),
                        "error": r.get("error", ""),
                        "cache_age_s": r.get("cache_age_s", 0),
                    })
                itins_out.append({
                    "price_value": it.price_value,
                    "price_currency": it.price_currency,
                    "price_display": it.price_display,
                    "total_duration_min": it.total_duration_min,
                    "n_stops": it.n_stops,
                    "headline_airline": it.headline_airline,
                    "operated_by": it.operated_by,
                    "segments": segs_priced,
                    "scraped_total_usd": round(total_usd, 2) if n_priced else None,
                    "scraped_n_priced": n_priced,
                    "scraped_complete": n_priced == len(segs_priced) and n_priced > 0,
                })
            gf_detail_per_leg.append({
                "origin": leg.origin,
                "dest": leg.destination,
                "date": leg.leg_date.isoformat(),
                "itineraries": itins_out,
                "duration_s": round(time.time() - t0, 1),
            })
            print(f"  [gf_detail {leg.origin}→{leg.destination}] done in {time.time()-t0:.0f}s, "
                  f"{len(itins_out)} itineraries", file=sys.stderr)
    except Exception as e:
        print(f"  [gf_detail] error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    result["gf_detail_per_leg"] = gf_detail_per_leg

    return result


_AIRLINE_NAME_TO_KEY = [
    (re.compile(r"\blatam\b", re.I), "latam"),
    (re.compile(r"\bgol\b", re.I), "gol"),
    (re.compile(r"\bazul\b", re.I), "azul"),
    (re.compile(r"\bavianca\b", re.I), "avianca"),
    (re.compile(r"\bjet\s*smart\b", re.I), "jetsmart"),
    (re.compile(r"\bcopa\b", re.I), "copa"),
    (re.compile(r"\bsky\b", re.I), "skyairline"),
]


def _airline_key(name: str) -> str:
    for pat, key in _AIRLINE_NAME_TO_KEY:
        if pat.search(name or ""):
            return key
    return ""


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
