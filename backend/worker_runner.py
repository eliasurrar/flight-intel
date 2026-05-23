"""worker_runner.py — Mac-side runner that polls the CF Worker for queued jobs.

Reads a job from the worker, executes scrapers, composer, ranks results,
writes back stage updates and final results.

Loop:
  every 5s:
    job = worker.dequeue()
    if job:
      run_pipeline(job)

Pipeline stages (reported back to Worker for the frontend progress UI):
  1. resolving_airports    (check origin/dest in registry)
  2. scraping_google_flights (with %)
  3. scraping_kayak (with %)
  4. composing_independent_legs (with %)
  5. ranking
  6. done

If anything raises → stage='error', error=str(e).
"""
from __future__ import annotations
import json
import os
import sys
import time
import traceback
from pathlib import Path
from urllib import request as urlrequest, parse as urlparse, error as urlerr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.scrapers import google_flights, kayak
from backend.composer import compose_split_two, candidate_hubs, rank, CombinedItinerary, MIN_CONNECT_HR
from dataclasses import asdict

WORKER_URL = os.environ.get("FLIGHT_INTEL_WORKER_URL", "https://flight-intel-worker.example.workers.dev")
BACKEND_TOKEN = os.environ.get("FLIGHT_INTEL_BACKEND_TOKEN", "")
POLL_INTERVAL_S = 5


def _post(path: str, body: dict) -> dict | None:
    req = urlrequest.Request(
        f"{WORKER_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Backend-Auth": BACKEND_TOKEN},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urlerr.URLError as e:
        print(f"[worker_runner] POST {path} failed: {e}", file=sys.stderr)
        return None


def _update(job_id: str, **fields):
    fields["job_id"] = job_id
    _post("/api/_internal/update", fields)


def _stage(job_id: str, stage: str, pct: int, log: str | None = None):
    fields = {"stage": stage, "progress_pct": pct}
    if log:
        fields["log_append"] = log
    _update(job_id, **fields)


def run_pipeline(job: dict) -> None:
    jid = job["job_id"]
    p = job["params"]
    origin = p["origin"]; dest = p["dest"]
    date_out = p["date_out"]; date_back = p.get("date_back")
    max_stops = p.get("max_stops", 2)
    min_connect = p.get("min_connect_hr", MIN_CONNECT_HR)

    try:
        _stage(jid, "resolving_airports", 5, f"{origin} → {dest} on {date_out}")

        # ── 1. Single-ticket: Google Flights ─────────────────────
        _stage(jid, "scraping_google_flights", 15, "querying google flights…")
        gf_results = google_flights.search(origin, dest, date_out, date_back, headless=True)
        _stage(jid, "scraping_google_flights", 35, f"google flights: {len(gf_results)} itineraries")

        # ── 2. Single-ticket: Kayak ───────────────────────────────
        try:
            _stage(jid, "scraping_kayak", 45, "querying kayak…")
            kayak_results = kayak.search(origin, dest, date_out, date_back, headless=True)
            _stage(jid, "scraping_kayak", 55, f"kayak: {len(kayak_results)} itineraries")
        except kayak.KayakBlocked as e:
            _stage(jid, "scraping_kayak", 55, f"kayak blocked: {e}")
            kayak_results = []

        all_single = list(gf_results) + list(kayak_results)

        # ── 3. Composer: split tickets via hubs ──────────────────
        hubs = candidate_hubs(origin, dest, max_hubs=4)
        _stage(jid, "composing_independent_legs", 60, f"trying {len(hubs)} hub(s): {','.join(hubs)}")
        split_combos: list[CombinedItinerary] = []
        # For each hub: scrape origin→hub and hub→dest, compose pairs.
        # Limit hubs for runtime; v1 just does top 2.
        for i, hub in enumerate(hubs[:2]):
            try:
                _stage(jid, "composing_independent_legs", 65 + i*10,
                       f"hub {hub}: origin leg…")
                leg_a = google_flights.search(origin, hub, date_out, None, headless=True)
                if not leg_a:
                    continue
                _stage(jid, "composing_independent_legs", 70 + i*10,
                       f"hub {hub}: dest leg…")
                leg_b = google_flights.search(hub, dest, date_out, None, headless=True)
                if not leg_b:
                    continue
                combos = compose_split_two(leg_a[:5], leg_b[:5], min_connect_hr=min_connect)
                split_combos.extend(combos[:10])
            except Exception as e:
                _stage(jid, "composing_independent_legs", 70 + i*10,
                       f"hub {hub} error: {type(e).__name__}: {e}")
                continue

        # Convert single-ticket itineraries into a unified shape (CombinedItinerary)
        single_as_combined = [
            CombinedItinerary(
                legs=[asdict(leg) for leg in it.legs],
                total_price_usd=it.price_usd,
                total_duration_min=it.total_duration_min,
                n_stops=it.n_stops,
                n_tickets=1,
                booking_urls=[it.booking_url],
                sources=[it.source],
                composition_type="single_ticket",
            )
            for it in all_single
        ]

        # ── 4. Filter + rank ─────────────────────────────────────
        _stage(jid, "ranking", 90, f"filtering {len(single_as_combined) + len(split_combos)} options")
        all_options = single_as_combined + split_combos
        # Filter by max_stops
        if max_stops < 99:
            all_options = [o for o in all_options if o.n_stops <= max_stops]
        ranked = rank(all_options)[:50]

        results_json = [asdict(r) for r in ranked]
        _update(jid, stage="done", progress_pct=100,
                results=results_json,
                log_append=f"done: {len(ranked)} ranked itineraries")

    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        _update(jid, stage="error", error=f"{type(e).__name__}: {e}")


def main():
    if not BACKEND_TOKEN:
        print("ERROR: FLIGHT_INTEL_BACKEND_TOKEN env var not set", file=sys.stderr)
        sys.exit(2)
    print(f"[worker_runner] polling {WORKER_URL} every {POLL_INTERVAL_S}s…")
    while True:
        try:
            res = _post("/api/_internal/dequeue", {})
            if res and res.get("job"):
                job = res["job"]
                print(f"[worker_runner] picked up job {job['job_id']}")
                run_pipeline(job)
            time.sleep(POLL_INTERVAL_S)
        except KeyboardInterrupt:
            print("interrupted")
            return
        except Exception as e:
            print(f"[worker_runner] poll error: {e}", file=sys.stderr)
            time.sleep(POLL_INTERVAL_S * 2)


if __name__ == "__main__":
    main()
