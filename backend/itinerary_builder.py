"""itinerary_builder.py — synthesize itineraries, price each leg, totalize.

This is the heart of the inversion Elias asked for: don't trust packs from
GF/Kayak/Booking blindly. Instead:
  1. Synthesize plausible O→D paths through curated hubs.
  2. For each leg of each path, scrape the operating airline's site for a
     price (cached). Multiple itineraries share legs → cache hits cut work.
  3. Sum the leg prices (USD-normalized) and rank itineraries.
  4. Expose every itinerary, even partial ones (with leg-by-leg detail).

Parallelism: legs across distinct (airline, O, D, date) tuples are scraped
concurrently with a bounded ThreadPool (browser launches are heavy).
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import network, price_cache, fx, sanity  # noqa: E402
from backend.scrapers import airlines  # noqa: E402


@dataclass
class PricedLeg:
    origin: str
    dest: str
    date: str
    airline_candidates: list[str]
    chosen_airline: str = ""
    price_value: float | None = None
    price_currency: str = ""
    price_display: str = ""
    price_usd: float | None = None
    url: str = ""
    scrape_status: str = "pending"  # pending|scraped|cached|no_url|blocked|failed|no_flights
    cache_age_s: int = 0
    error: str = ""
    screenshot_path: str = ""


@dataclass
class PricedItinerary:
    path: list[str]
    legs: list[PricedLeg]
    n_stops: int
    detour_ratio: float
    total_distance_km: int
    total_usd: float | None = None
    is_complete: bool = False
    n_legs_priced: int = 0
    score: float = 0.0


# ──────────────────────────────────────────────────────────────────────────
# Per-leg pricing with cache
# ──────────────────────────────────────────────────────────────────────────


def _price_leg(
    airline: str,
    origin: str,
    dest: str,
    date: str,
    *,
    headless: bool = False,
    timeout_ms: int = 25_000,
    ttl_s: int = 6 * 3600,
) -> dict:
    """Scrape (or hit cache) for one (airline, O, D, date) tuple. Returns dict."""
    cached = price_cache.get(airline, origin, dest, date, ttl_s=ttl_s)
    if cached and cached.get("ok"):
        return {
            "ok": True,
            "price_value": cached.get("price_value"),
            "price_currency": cached.get("price_currency", ""),
            "price_display": cached.get("price_display", ""),
            "url": cached.get("url", ""),
            "cache_age_s": cached.get("_cache_age_s", 0),
            "from_cache": True,
            "error": "",
        }
    url = airlines.url_for(airline, origin, dest, date)
    if not url:
        return {"ok": False, "url": "", "error": "no_url_template", "from_cache": False}
    try:
        q = airlines.fetch_price(
            airline, origin, dest, date,
            headless=headless, timeout_ms=timeout_ms, vision_fallback=True,
        )
        d = asdict(q)
        # Save to cache (even failures, with shorter effective TTL via cached_at)
        price_cache.put(airline, origin, dest, date, q)
        d["from_cache"] = False
        return d
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)[:200], "from_cache": False}


# ──────────────────────────────────────────────────────────────────────────
# Itinerary building
# ──────────────────────────────────────────────────────────────────────────


def build(
    origin: str,
    dest: str,
    date: str,
    *,
    max_stops: int = 2,
    top_n_itineraries: int = 12,
    max_workers: int = 3,
    max_detour: float = 2.2,
    headless: bool = False,
    progress=None,
) -> list[PricedItinerary]:
    """Synthesize → price → totalize. Returns ranked itineraries."""
    synth = network.synthesize(origin, dest, max_stops=max_stops, max_detour=max_detour)
    chosen = synth[:top_n_itineraries]
    if progress:
        progress(f"synth {len(synth)} → top {len(chosen)}")

    # Build the set of UNIQUE (airline, O, D, date) tuples we need to scrape.
    # For each itin leg pick its top-1 carrier (cheapest probable). We could
    # try all candidates, but that explodes work.
    unique_jobs: dict[tuple, dict] = {}
    for it in chosen:
        for leg in it.legs:
            for carrier in leg.airline_candidates[:2]:  # try top-2 carriers per leg
                key = (carrier, leg.origin, leg.dest, date)
                unique_jobs.setdefault(key, {"airline": carrier,
                                              "origin": leg.origin,
                                              "dest": leg.dest,
                                              "date": date})
    if progress:
        progress(f"unique (airline, O, D, date) jobs: {len(unique_jobs)}")

    # Scrape concurrently
    results: dict[tuple, dict] = {}
    t0 = time.time()

    def _job(key, params):
        r = _price_leg(**params, headless=headless)
        return key, r

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_job, k, v) for k, v in unique_jobs.items()]
        done = 0
        for fut in as_completed(futs):
            try:
                key, r = fut.result()
            except Exception as e:
                continue
            results[key] = r
            done += 1
            if progress and done % 3 == 0:
                progress(f"  scraped {done}/{len(unique_jobs)} ({time.time()-t0:.0f}s)")

    if progress:
        progress(f"all scrapes done in {time.time()-t0:.1f}s")

    # Build PricedItinerary for each candidate, picking cheapest carrier per leg
    out: list[PricedItinerary] = []
    for it in chosen:
        priced_legs: list[PricedLeg] = []
        for leg in it.legs:
            # Pick the cheapest carrier among candidates that produced a price
            best_carrier = ""
            best_usd = None
            best_res = None
            for carrier in leg.airline_candidates[:2]:
                r = results.get((carrier, leg.origin, leg.dest, date))
                if not r or not r.get("ok") or r.get("price_value") is None:
                    continue
                usd = fx.convert(r["price_value"], r.get("price_currency", "USD"), "USD")
                if usd is None:
                    continue
                if best_usd is None or usd < best_usd:
                    best_usd = usd
                    best_carrier = carrier
                    best_res = r
            if best_res:
                priced_legs.append(PricedLeg(
                    origin=leg.origin, dest=leg.dest, date=date,
                    airline_candidates=leg.airline_candidates,
                    chosen_airline=best_carrier,
                    price_value=best_res.get("price_value"),
                    price_currency=best_res.get("price_currency", ""),
                    price_display=best_res.get("price_display", ""),
                    price_usd=best_usd,
                    url=best_res.get("url", ""),
                    scrape_status="cached" if best_res.get("from_cache") else "scraped",
                    cache_age_s=best_res.get("cache_age_s", 0),
                ))
            else:
                # No price → carry first candidate's URL for manual lookup
                first_carrier = leg.airline_candidates[0] if leg.airline_candidates else ""
                r = results.get((first_carrier, leg.origin, leg.dest, date), {})
                priced_legs.append(PricedLeg(
                    origin=leg.origin, dest=leg.dest, date=date,
                    airline_candidates=leg.airline_candidates,
                    chosen_airline=first_carrier,
                    url=r.get("url", "") or airlines.url_for(first_carrier, leg.origin, leg.dest, date),
                    scrape_status=(
                        "blocked" if "block" in (r.get("error") or "").lower()
                        else "no_flights" if r.get("error") == "airline_no_flights"
                        else "failed" if r else "no_url"
                    ),
                    error=r.get("error", ""),
                ))

        n_priced = sum(1 for l in priced_legs if l.price_usd is not None)
        is_complete = n_priced == len(priced_legs)
        total_usd = sum(l.price_usd for l in priced_legs if l.price_usd) if n_priced else None

        out.append(PricedItinerary(
            path=[priced_legs[0].origin] + [l.dest for l in priced_legs],
            legs=priced_legs,
            n_stops=it.n_stops,
            detour_ratio=round(it.detour_ratio, 2),
            total_distance_km=round(it.total_distance_km),
            total_usd=total_usd,
            is_complete=is_complete,
            n_legs_priced=n_priced,
        ))

    # Rank: complete itineraries by total_usd ascending; partial after, by n_priced desc + detour
    def _rank_key(it: PricedItinerary):
        if it.is_complete:
            return (0, it.total_usd or 9e9)
        return (1, -it.n_legs_priced, it.detour_ratio, it.n_stops)
    out.sort(key=_rank_key)

    # Score for display: complete itineraries get total; partial get penalized
    for i, it in enumerate(out):
        it.score = i + 1
    return out


def to_dict(it: PricedItinerary) -> dict:
    d = asdict(it)
    return d


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--max-stops", type=int, default=2)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    def prog(msg):
        print(f"  [build] {msg}", file=sys.stderr)

    res = build(args.origin, args.dest, args.date,
                top_n_itineraries=args.top, max_stops=args.max_stops,
                headless=args.headless, progress=prog)
    print(f"\n=== {len(res)} itineraries ===")
    for it in res:
        path = "→".join(it.path)
        if it.is_complete:
            print(f"  ✅ ${it.total_usd:6.0f}  {path} ({it.n_stops}stop)")
        else:
            print(f"  ⚠  ${(it.total_usd or 0):6.0f}  {path} ({it.n_legs_priced}/{len(it.legs)} legs priced)")
        for l in it.legs:
            tag = f"{l.chosen_airline:10s}"
            price = f"${l.price_usd:.0f}" if l.price_usd else "  ?  "
            print(f"      {tag} {l.origin}→{l.dest}  {price}  ({l.scrape_status})")
