"""airport_registry.py — Build/maintain global airport + airline + route registry.

Strategy (all free, no rate limits):
  1. OurAirports.com CSV   — community-maintained, daily-updated authoritative source
                              (76,000+ airports, IATA + ICAO + coords + country + type)
  2. OpenFlights routes.dat — legacy 2017 baseline for route adjacency (fallback)
  3. Wikipedia airport pages — on-demand enrichment for "airlines and destinations"
                                when needed (single-airport refresh)

Output:
  data/airports.json    {IATA: {name, city, country, region, lat, lon, type, updated_at}}
  data/airlines.json    {IATA: {name, country, callsign, updated_at}}
  data/routes.json      {origin_IATA: {dest_IATA: [airline_IATA, ...]}}
  data/registry_meta.json   {region: {last_refresh, n_airports, sources}}

Usage:
  python backend/airport_registry.py --region SA
  python backend/airport_registry.py --region all
  python backend/airport_registry.py --refresh-base  # re-pull OurAirports CSVs
"""
from __future__ import annotations
import argparse, csv, io, json, sys, time
from datetime import datetime, timezone
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CACHE = ROOT / "data" / "_cache"
DATA.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)

OA_AIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OA_RUNWAYS = "https://davidmegginson.github.io/ourairports-data/runways.csv"
OA_COUNTRIES = "https://davidmegginson.github.io/ourairports-data/countries.csv"
OF_ROUTES = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/routes.dat"
OF_AIRLINES = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airlines.dat"

REGIONS = {
    "SA": ["AR","BO","BR","CL","CO","EC","GY","PY","PE","SR","UY","VE","FK"],
    "CA": ["BZ","CR","SV","GT","HN","NI","PA"],
    "CB": ["AG","BS","BB","CU","DM","DO","GD","HT","JM","KN","LC","VC","TT","AI","AW","KY","CW","SX","TC","VG","VI","PR","MQ","GP","BL","MF","MS"],
    "NA": ["US","CA","MX"],
    "EU": ["AL","AD","AT","BY","BE","BA","BG","HR","CY","CZ","DK","EE","FI","FR","DE","GR","HU","IS","IE","IT","LV","LI","LT","LU","MT","MD","MC","ME","NL","MK","NO","PL","PT","RO","RU","SM","RS","SK","SI","ES","SE","CH","UA","GB","VA","TR"],
    "AS": ["AF","AM","AZ","BD","BT","BN","KH","CN","GE","IN","ID","JP","KZ","KG","LA","MY","MV","MN","MM","NP","KP","PK","PH","SG","KR","LK","TW","TJ","TH","TL","TM","UZ","VN"],
    "OC": ["AU","FJ","KI","MH","FM","NR","NZ","PW","PG","WS","SB","TO","TV","VU","NC","PF"],
    "AF": ["DZ","AO","BJ","BW","BF","BI","CM","CV","CF","TD","KM","CD","CG","CI","DJ","EG","GQ","ER","SZ","ET","GA","GM","GH","GN","GW","KE","LS","LR","LY","MG","MW","ML","MR","MU","MA","MZ","NA","NE","NG","RW","ST","SN","SC","SL","SO","ZA","SS","SD","TZ","TG","TN","UG","ZM","ZW"],
    "ME": ["BH","IR","IQ","IL","JO","KW","LB","OM","PS","QA","SA","SY","AE","YE"],
}

UA = "flight-intel/0.1 (eliasurrar@gmail.com)"


def _download(url: str, cache_name: str, max_age_h: int = 24) -> bytes:
    cp = CACHE / cache_name
    if cp.exists():
        age_h = (time.time() - cp.stat().st_mtime) / 3600
        if age_h < max_age_h:
            return cp.read_bytes()
    print(f"  [dl] {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    cp.write_bytes(data)
    return data


def _load(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def _save(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))
    tmp.replace(path)


def _country_to_region() -> dict[str, str]:
    out = {}
    for region, countries in REGIONS.items():
        for c in countries:
            out[c] = region
    return out


def update_all_regions(filter_regions: list[str] | None = None) -> dict:
    print(f"[registry] fetching OurAirports CSVs (cached 24h)...")
    airports_csv = _download(OA_AIRPORTS, "ourairports_airports.csv").decode()
    print(f"[registry] fetching OpenFlights airlines.dat...")
    airlines_raw = _download(OF_AIRLINES, "openflights_airlines.dat").decode("utf-8", errors="ignore")

    cr_map = _country_to_region()
    if filter_regions:
        wanted_countries = set()
        for r in filter_regions:
            wanted_countries.update(REGIONS.get(r, []))
    else:
        wanted_countries = None  # all

    ts = datetime.now(timezone.utc).isoformat()
    airports = {}
    skipped_no_iata = 0
    skipped_closed = 0
    skipped_not_wanted = 0
    by_region = {}
    by_type = {}

    reader = csv.DictReader(io.StringIO(airports_csv))
    for row in reader:
        iata = (row.get("iata_code") or "").strip().upper()
        if not iata or len(iata) != 3:
            skipped_no_iata += 1
            continue
        atype = (row.get("type") or "").strip()
        if atype in ("closed", "heliport", "balloonport"):
            skipped_closed += 1
            continue
        country = (row.get("iso_country") or "").strip().upper()
        if wanted_countries is not None and country not in wanted_countries:
            skipped_not_wanted += 1
            continue
        region = cr_map.get(country, "??")
        airports[iata] = {
            "iata": iata,
            "icao": (row.get("ident") or row.get("gps_code") or "").strip().upper(),
            "name": (row.get("name") or "").strip(),
            "city": (row.get("municipality") or "").strip(),
            "country": country,
            "region": region,
            "lat": float(row["latitude_deg"]) if row.get("latitude_deg") else None,
            "lon": float(row["longitude_deg"]) if row.get("longitude_deg") else None,
            "type": atype,  # large_airport, medium_airport, small_airport
            "updated_at": ts,
        }
        by_region[region] = by_region.get(region, 0) + 1
        by_type[atype] = by_type.get(atype, 0) + 1

    # Airlines from OpenFlights (legacy but only free authoritative IATA→name map)
    airlines = {}
    for line in airlines_raw.splitlines():
        parts = next(csv.reader([line]), None)
        if not parts or len(parts) < 8:
            continue
        try:
            _, name, alias, iata, icao, callsign, country, active = parts[:8]
        except ValueError:
            continue
        iata = iata.strip().strip('"').upper()
        if not iata or len(iata) != 2 or iata == "\\N":
            continue
        if active.strip().strip('"').upper() != "Y":
            continue
        airlines[iata] = {
            "iata": iata,
            "icao": icao.strip().strip('"').upper(),
            "name": name.strip().strip('"'),
            "callsign": callsign.strip().strip('"'),
            "country": country.strip().strip('"'),
            "updated_at": ts,
        }

    _save(DATA / "airports.json", airports)
    _save(DATA / "airlines.json", airlines)

    meta = _load(DATA / "registry_meta.json", {})
    meta["_global"] = {
        "last_refresh": ts,
        "n_airports": len(airports),
        "n_airlines": len(airlines),
        "by_region": by_region,
        "by_type": by_type,
        "skipped_no_iata": skipped_no_iata,
        "skipped_closed": skipped_closed,
        "skipped_not_wanted": skipped_not_wanted,
        "sources": ["ourairports.csv", "openflights/airlines.dat"],
    }
    _save(DATA / "registry_meta.json", meta)

    print(f"[registry] ✅ {len(airports)} airports, {len(airlines)} airlines")
    print(f"[registry] by region: {by_region}")
    print(f"[registry] by type:   {by_type}")
    return meta["_global"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", help="Comma-separated region codes or 'all'")
    ap.add_argument("--refresh-base", action="store_true",
                    help="Force re-download of OurAirports CSV (ignore 24h cache)")
    args = ap.parse_args()

    if args.refresh_base:
        for f in CACHE.glob("*.csv"):
            f.unlink()
        for f in CACHE.glob("*.dat"):
            f.unlink()

    if args.region:
        if args.region.strip().lower() == "all":
            update_all_regions(filter_regions=None)
        else:
            regions = [r.strip().upper() for r in args.region.split(",")]
            update_all_regions(filter_regions=regions)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
