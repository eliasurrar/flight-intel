# flight-intel

Multi-source flight search with persistent airport/airline/route registry.

## Architecture

```
User → eliasurrar.github.io/flight-intel/   (static frontend, GH Pages)
            │
            ▼
       CF Worker  →  CF KV (job queue)
                          ▲
                          │ poll every 5s
                          │
                  Mac launchd → Playwright scrapers
                          │
                          ▼
                  data/{airports,airlines,routes}.json
```

## Data sources (all free, no auth)

| Layer | Source | Cadence |
|---|---|---|
| Airports global | OurAirports CSV | 24h cache |
| Airlines global | OpenFlights airlines.dat | 24h cache |
| Routes seed | OpenFlights routes.dat (2017) | one-shot fallback |
| Routes live | Google Flights (Playwright) | per-search |
| Multi-leg compose | Kayak multi-city + per-airline direct | per-search |
| Airline destinations enrichment | Wikipedia per-airport pages | weekly |

## Registry status

Run: `python backend/airport_registry.py --region all`

Last refresh produced **8,958 airports** + **992 airlines** worldwide.

## Layout

```
flight-intel/
├── backend/
│   ├── airport_registry.py     # OurAirports + OpenFlights → JSON registry
│   ├── scrapers/               # Google Flights, Kayak, per-airline
│   ├── composer.py             # multi-leg independent combinations (min 1.5h connect)
│   └── ranker.py               # weighted price × time × stops sort
├── frontend/                   # static HTML+JS for GH Pages
├── worker/                     # CF Worker (job queue + result polling)
├── data/
│   ├── airports.json
│   ├── airlines.json
│   ├── routes.json
│   ├── registry_meta.json
│   └── _cache/                 # downloaded CSVs (gitignored)
└── scheduled/logs/
```

## Stage reporting

Backend writes progress to `KV:job:{id}:status` in stages so the frontend can show:
- `queued`
- `resolving_airports`
- `scraping_google_flights` (with %)
- `scraping_airline_LATAM` (with %)
- `composing_independent_legs`
- `ranking`
- `done` (or `error`)
