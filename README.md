# flight-intel

Multi-source flight scraper + dashboard for pre-defined trip dates.

## What it does

For each trip in `trips.py` (imported from `flight-monitor/patterns.py`), runs:

1. **Google Flights** (`fast_flights` library) — round-trip combo price OR
   per-leg one-way sum for true multi-city.
2. **Booking Flights** — best-effort Playwright scrape; emits search URL
   when the SPA hangs (frequent).
3. **Kayak** multi-city — Playwright scrape, headless, ~8s, ~30 quotes/trip.
4. **Per-airline deep links** — LATAM, GOL, Azul, Avianca, JetSmart, Copa,
   Sky direct purchase URLs per leg (links always, price best-effort).

Output JSON snapshots → `data/snapshots/<YYYY-MM-DD>/<trip_key>.json`,
mirrored to `docs/snapshots/` for GitHub Pages.

## Dashboard

- `frontend/trips.html` (mirror at `docs/trips.html`) — reads the latest
  snapshot and renders one card per trip:
  - 3 columns: GF / Booking / Kayak
  - Cheapest pack highlighted (USD-normalized comparison)
  - Per-leg airline pills with deep purchase links
  - Totalizer Σ(min per-leg airline) vs best aggregator pack
- `frontend/index.html` — legacy ad-hoc search (currently not wired to
  worker_runner.py; the worker pipeline was simplified to focus on
  snapshots-only).

## Layout

```
flight-intel/
├── trips.py                    # imports FIXED_TRIPS + PATTERNS from flight-monitor
├── backend/
│   ├── scrapers/
│   │   ├── google_flights.py   # fast_flights-backed pricer + TFS URL builder
│   │   ├── booking.py          # Playwright (best-effort)
│   │   ├── kayak.py            # Playwright (reliable)
│   │   └── airlines.py         # per-airline URL templates + price scrape
│   └── snapshot.py             # orchestrator: runs all sources for all trips
├── frontend/
│   ├── index.html              # ad-hoc search (legacy)
│   └── trips.html              # snapshot dashboard
├── docs/                       # GitHub Pages mirror (auto-pushed by daily_snapshot.sh)
├── data/snapshots/             # JSON snapshots by date
└── scheduled/
    └── daily_snapshot.sh       # launchd-triggered runner + git push
```

## Schedule

- `com.elias.flight-intel.snapshot.plist` → 09:15 + 21:15 Chile.
- Runs `scheduled/daily_snapshot.sh` → snapshot all trips → mirror to
  `docs/` → commit + push.

## How to run manually

```bash
cd /Users/openclaw/projects/flight-intel
.venv/bin/python backend/snapshot.py                   # all trips
.venv/bin/python backend/snapshot.py --trip GRU-2026-06 # one trip
.venv/bin/python backend/snapshot.py --limit 3          # first 3 trips
```

Smoke per source:
```bash
.venv/bin/python backend/scrapers/google_flights.py
.venv/bin/python backend/scrapers/kayak.py
.venv/bin/python backend/scrapers/booking.py
.venv/bin/python backend/scrapers/airlines.py --airline latam --from SCL --to GRU --dep 2026-06-18 --ret 2026-06-29
```

## What we dropped (2026-05-25)

- `airports.json` (8958 OurAirports), `airlines.json` (OpenFlights),
  `routes_graph.json`, all under `.trash/` — recoverable.
- `backend/airport_registry.py`, `backend/route_optimizer.py`,
  `backend/build_route_graph.py` — registry approach abandoned.
- Worker queue pipeline (`worker/`, `backend/worker_runner.py`) is dormant;
  ad-hoc search not used. Could be re-enabled later if needed.

## Limitations & known pitfalls

- **Booking** is unreliable: their SPA hangs on `search_loading_overlay`
  for 30+ seconds in headless and yields no flight cards. The dashboard
  still surfaces the search URL for manual click-through.
- **Google Flights** via `fast_flights` does NOT return a combined "pack
  price" for true multi-city (3+ distinct legs). It only does for
  round-trip. For multi-city, we sum cheapest per-leg one-way prices.
- **Per-airline** sites (LATAM, GOL, Avianca, etc.) are SPAs that don't
  expose prices to a fast Playwright body-text scrape. We surface their
  search URLs deep-linked; price scraping is best-effort and often
  returns `price_not_visible`.
- Currency normalization to USD in the dashboard uses hardcoded FX rates
  in JS. For real comparisons, update `FX_TO_USD` in `trips.html` or
  consume `data/fx_cache.json` (TODO).
