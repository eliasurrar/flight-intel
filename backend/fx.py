"""FX converter: BRL como moneda base, soporta USD/CLP/EUR/BRL/ARS/COP/PEN/MXN.

Source: exchangerate.host (free, no key). Cache diaria en data/fx_cache.json.
Fallback: rates hardcoded conservadores si API falla.

Uso:
    from fx import to_brl, convert
    brl = to_brl(100, "USD")              # 100 USD → BRL
    eur = convert(100, "USD", "EUR")
    rates = get_rates()                   # {USD: 5.2, CLP: 0.0055, ...}
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
CACHE_FP = ROOT / "data" / "fx_cache.json"

BASE = "BRL"
SUPPORTED = ["BRL", "USD", "CLP", "EUR", "ARS", "COP", "PEN", "MXN", "GBP"]
CACHE_TTL_SEC = 6 * 3600  # 6h

# Conservative fallback rates (BRL per 1 unit of currency). Refreshed manually.
FALLBACK_RATES_BRL_PER = {
    "BRL": 1.0,
    "USD": 5.20,
    "EUR": 5.65,
    "CLP": 0.0055,
    "ARS": 0.005,
    "COP": 0.0012,
    "PEN": 1.40,
    "MXN": 0.26,
    "GBP": 6.55,
}


def _load_cache() -> dict | None:
    if not CACHE_FP.exists():
        return None
    try:
        d = json.loads(CACHE_FP.read_text())
        ts = datetime.fromisoformat(d["fetched_at"])
        if datetime.now(timezone.utc) - ts < timedelta(seconds=CACHE_TTL_SEC):
            return d
    except Exception:
        return None
    return None


def _save_cache(rates: dict, source: str):
    CACHE_FP.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "base": BASE,
        "source": source,
        "rates_brl_per": rates,
    }, indent=2))


def _fetch_remote() -> dict | None:
    """Try exchangerate.host (free). Returns dict {CCY: BRL_per_1_CCY} or None."""
    try:
        url = f"https://api.exchangerate.host/latest?base=BRL&symbols={','.join(c for c in SUPPORTED if c != 'BRL')}"
        req = urllib.request.Request(url, headers={"User-Agent": "flight-intel/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        if not data.get("success", True):
            return None
        rates_per_brl = data.get("rates", {})
        if not rates_per_brl:
            return None
        # API returns CCY per 1 BRL; invert to BRL per 1 CCY
        out = {"BRL": 1.0}
        for ccy, per_brl in rates_per_brl.items():
            if per_brl and per_brl > 0:
                out[ccy] = 1.0 / per_brl
        return out
    except Exception as e:
        print(f"[fx] remote failed: {e}")
        return None


def get_rates() -> dict:
    """Return {CCY: BRL_per_1_CCY}. Use cache if fresh, else fetch, else fallback."""
    c = _load_cache()
    if c and c.get("rates_brl_per"):
        return c["rates_brl_per"]
    remote = _fetch_remote()
    if remote:
        _save_cache(remote, "exchangerate.host")
        return remote
    # Fallback
    _save_cache(FALLBACK_RATES_BRL_PER, "fallback_hardcoded")
    return FALLBACK_RATES_BRL_PER


def to_brl(amount: float, currency: str) -> float:
    currency = (currency or "USD").upper()
    rates = get_rates()
    if currency not in rates:
        return amount  # unknown, return as-is
    return amount * rates[currency]


def from_brl(brl_amount: float, currency: str) -> float:
    currency = (currency or "USD").upper()
    if currency == "BRL":
        return brl_amount
    rates = get_rates()
    if currency not in rates or rates[currency] == 0:
        return brl_amount
    return brl_amount / rates[currency]


def convert(amount: float, from_ccy: str, to_ccy: str) -> float:
    return from_brl(to_brl(amount, from_ccy), to_ccy)


if __name__ == "__main__":
    rates = get_rates()
    print(f"Base: {BASE}")
    for c, r in sorted(rates.items()):
        print(f"  1 {c} = {r:>10.4f} BRL  →  1 BRL = {1/r if r else 0:>10.4f} {c}")
    print()
    print("Test conversions:")
    for amt, frm, to in [(100, "USD", "BRL"), (100, "CLP", "BRL"), (500, "BRL", "USD"), (1000, "BRL", "CLP")]:
        print(f"  {amt} {frm} → {convert(amt, frm, to):>12.2f} {to}")
