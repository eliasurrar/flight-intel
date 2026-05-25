"""trips.py — Fechas pre-definidas de viajes a monitorear.

Importa de ../flight-monitor/patterns.py para mantener una sola fuente de verdad.
Si flight-monitor no está disponible, fallback a una lista mínima.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# Permitir importar desde flight-monitor sin moverlo
_FM = Path("/Users/openclaw/projects/flight-monitor")
if _FM.exists() and str(_FM) not in sys.path:
    sys.path.insert(0, str(_FM))

try:
    from patterns import FIXED_TRIPS, PATTERNS, pattern_pairs, ORIGIN, DESTINATION  # type: ignore
    _HAVE_FM = True
except Exception:  # pragma: no cover
    _HAVE_FM = False


@dataclass(frozen=True)
class TripLeg:
    origin: str
    destination: str
    leg_date: date


@dataclass(frozen=True)
class Trip:
    key: str
    name: str
    legs: tuple[TripLeg, ...]  # 1=one-way, 2=round-trip, N=multi-city


def all_trips() -> list[Trip]:
    """Todos los viajes pre-definidos (fixed + patrones)."""
    out: list[Trip] = []
    if _HAVE_FM:
        for ft in FIXED_TRIPS:
            out.append(Trip(
                key=ft.key,
                name=ft.name,
                legs=(
                    TripLeg(ft.out.origin, ft.out.destination, ft.out.leg_date),
                    TripLeg(ft.ret.origin, ft.ret.destination, ft.ret.leg_date),
                ),
            ))
        # Patrones recurrentes: tomar los siguientes 4 pares de cada uno
        # para no inundar el dashboard
        for p in PATTERNS:
            pairs = pattern_pairs(p)[:4]
            for dep, ret in pairs:
                out.append(Trip(
                    key=f"PAT-{p.key}-{dep.isoformat()}",
                    name=f"{p.name} · {ORIGIN}↔{DESTINATION} {dep}→{ret}",
                    legs=(
                        TripLeg(ORIGIN, DESTINATION, dep),
                        TripLeg(DESTINATION, ORIGIN, ret),
                    ),
                ))
    else:
        # Fallback mínimo
        out.append(Trip(
            key="GRU-2026-06",
            name="SCL↔GRU 18→29 jun 2026",
            legs=(
                TripLeg("SCL", "GRU", date(2026, 6, 18)),
                TripLeg("GRU", "SCL", date(2026, 6, 29)),
            ),
        ))
    return out


def trip_by_key(k: str) -> Trip | None:
    for t in all_trips():
        if t.key == k:
            return t
    return None


if __name__ == "__main__":
    for t in all_trips():
        legs = " · ".join(f"{l.origin}→{l.destination} {l.leg_date}" for l in t.legs)
        print(f"[{t.key}] {t.name}\n  {legs}")
