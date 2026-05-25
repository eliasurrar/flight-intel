"""worker_runner.py — Poll CF Worker for queued search jobs and run snapshot pipeline.

Job params shape (sent from frontend):
{
  "legs": [
    {"origin": "SCL", "dest": "GRU", "date": "2026-06-18"},
    {"origin": "GRU", "dest": "FLN", "date": "2026-06-22"},  # stopover leg
    {"origin": "FLN", "dest": "SCL", "date": "2026-06-29"}
  ],
  "name": "SCL→GRU→FLN→SCL con 4 días en FLN"  # optional, for display
}

Pipeline:
  1. queued → in_progress
  2. resolving (validate legs)
  3. scraping_google_flights
  4. scraping_kayak
  5. scraping_booking (vision fallback if scrape fails)
  6. building_airline_links
  7. composing (if N>2 and aggregators returned no pack, smart-split)
  8. done

Snapshot is also persisted to data/snapshots/adhoc/<job_id>.json so the dashboard
can render it like any pre-defined trip.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trips import Trip, TripLeg  # noqa: E402
from backend import snapshot as snap  # noqa: E402

WORKER_URL = os.environ.get("FLIGHT_INTEL_WORKER_URL", "")
BACKEND_TOKEN = os.environ.get("FLIGHT_INTEL_BACKEND_TOKEN", "")
POLL_INTERVAL_S = 5

ADHOC_DIR = ROOT / "data" / "snapshots" / "adhoc"


def _post(path: str, body: dict) -> dict | None:
    try:
        r = httpx.post(
            f"{WORKER_URL}{path}", json=body,
            headers={"X-Backend-Auth": BACKEND_TOKEN}, timeout=20,
        )
        if r.status_code != 200:
            print(f"[worker_runner] POST {path} → {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
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


def _trip_from_params(params: dict, job_id: str) -> Trip:
    legs_raw = params.get("legs", [])
    if not legs_raw:
        raise ValueError("job has no legs")
    legs = tuple(
        TripLeg(
            origin=l["origin"].upper().strip(),
            destination=l["dest"].upper().strip(),
            leg_date=date.fromisoformat(l["date"]),
        )
        for l in legs_raw
    )
    name = params.get("name") or " → ".join(
        [legs[0].origin] + [l.destination for l in legs]
    ) + f" ({legs[0].leg_date} → {legs[-1].leg_date})"
    return Trip(key=f"adhoc-{job_id}", name=name, legs=legs)


def run_pipeline(job: dict) -> None:
    jid = job["job_id"]
    try:
        p = job["params"]
        trip = _trip_from_params(p, jid)
        n_legs = len(trip.legs)
        is_stopover = n_legs >= 3

        _stage(jid, "resolving", 5, f"{n_legs} legs: " + " → ".join(
            [trip.legs[0].origin] + [l.destination for l in trip.legs]
        ))

        # Stage updates are emitted inside snapshot_trip via stderr; we can also
        # do coarse-grained stage emits here.
        _stage(jid, "scraping", 15, "running google flights, kayak, booking, airlines in sequence…")

        result = snap.snapshot_trip(trip, fetch_airline_prices=False)

        # If multi-leg (3+) and no source returned a usable pack, surface that
        # split-by-stopover hint is the way.
        if is_stopover:
            any_pack = False
            for src in ("google_flights", "kayak"):
                quotes = (result["sources"].get(src) or {}).get("quotes") or []
                if quotes and any(q.get("total_price_value") for q in quotes):
                    any_pack = True
                    break
            if not any_pack:
                _stage(jid, "composing", 80,
                       "no aggregator pack — split-by-stopover applied: per-leg airline links surfaced")
                result["composition_note"] = "split_by_stopover"

        # Persist result snapshot for the dashboard
        ADHOC_DIR.mkdir(parents=True, exist_ok=True)
        (ADHOC_DIR / f"{jid}.json").write_text(json.dumps(result, indent=2, default=str))

        # Build a compact "results" payload for the worker KV
        compact = {
            "trip_key": result["trip_key"],
            "trip_name": result["trip_name"],
            "legs": result["legs"],
            "sources": result["sources"],
            "per_leg_airlines": result["per_leg_airlines"],
            "composition_note": result.get("composition_note", ""),
            "snapshot_at": result["snapshot_at"],
            "snapshot_path": f"snapshots/adhoc/{jid}.json",
        }
        _update(jid, stage="done", progress_pct=100, results=compact,
                log_append=f"done: GF={len((compact['sources'].get('google_flights') or {}).get('quotes', []) or [])} "
                           f"KY={len((compact['sources'].get('kayak') or {}).get('quotes', []) or [])} "
                           f"BK={len((compact['sources'].get('booking') or {}).get('quotes', []) or [])}")
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        _update(jid, stage="error", error=f"{type(e).__name__}: {e}", progress_pct=100)


def main():
    if not BACKEND_TOKEN or not WORKER_URL:
        print("ERROR: FLIGHT_INTEL_WORKER_URL or FLIGHT_INTEL_BACKEND_TOKEN not set", file=sys.stderr)
        sys.exit(2)
    print(f"[worker_runner] polling {WORKER_URL} every {POLL_INTERVAL_S}s …", file=sys.stderr)
    while True:
        try:
            res = _post("/api/_internal/dequeue", {})
            if res and res.get("job"):
                job = res["job"]
                print(f"[worker_runner] picked up {job['job_id']}", file=sys.stderr)
                run_pipeline(job)
            time.sleep(POLL_INTERVAL_S)
        except KeyboardInterrupt:
            print("[worker_runner] interrupted")
            return
        except Exception as e:
            print(f"[worker_runner] poll error: {e}", file=sys.stderr)
            time.sleep(POLL_INTERVAL_S * 2)


if __name__ == "__main__":
    main()
