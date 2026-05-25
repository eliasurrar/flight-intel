"""backend/segments.py — Extract individual directo flight segments from
aggregator quote raw_text.

The user requirement (2026-05-25): for ANY route shown by GF/Kayak/Booking,
even multi-stop ones, decompose it into individual directo segments. Each
segment is by definition directo (one airline, one O&D, one departure). Then
the dashboard lets the user buy each segment independently from the operating
airline's site.

This module parses raw_text strings into a list of Segment objects, then
deduplicates across all quotes so we only have to fetch each unique
(origin, dest, date, airline) once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Iterable


@dataclass
class Segment:
    origin: str          # IATA 3-letter
    dest: str            # IATA 3-letter
    date: str            # ISO YYYY-MM-DD (best effort)
    airline: str         # operating airline name as shown by aggregator
    dep_time: str = ""   # local "HH:MM" 24h, best effort
    arr_time: str = ""
    source: str = ""     # which aggregator surfaced this segment


# Kayak raw_text structure (observed 2026-05-25):
#
#   6:50 pm – 11:42 am+1
#   Copa Airlines
#   2 stops
#   GRU
#    7h 05m layover, Sao Paulo Guarulhos Intl
#   , PTY
#    0h 42m layover, Panama City Tocumen Intl
#   18h 52m
#   AJU
#   -
#   ADZ
#   12:41 pm – 12:25 pm+1
#   Copa Airlines
#   ...
#
# The structure is: per direction (outbound, return),
#   <dep_time> – <arr_time>[+N]
#   <airline>
#   <N stops|nonstop>
#   <hub_iata> ... <hub_iata> ...
#   <total_duration>
#   <origin>
#   -
#   <dest>


_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*(am|pm)?\b", re.I)
_DURATION_RE = re.compile(r"(\d+)h\s*(\d+)?m?", re.I)
_IATA_RE = re.compile(r"\b([A-Z]{3})\b")


def _norm_time(t: str, ampm: str | None) -> str:
    """'10:15' + 'am' → '10:15'  ;  '6:50' + 'pm' → '18:50'."""
    try:
        h, m = t.split(":")
        h = int(h); m = int(m)
        if ampm:
            ampm = ampm.lower()
            if ampm == "pm" and h != 12: h += 12
            elif ampm == "am" and h == 12: h = 0
        return f"{h:02d}:{m:02d}"
    except Exception:
        return t


def parse_kayak_quote(raw_text: str, planned_legs: list[dict]) -> list[Segment]:
    """Parse a Kayak card raw_text into segments.

    planned_legs: the trip's logical legs (origin, dest, date) as we sent them.
                  Used to assign dates to each direction and infer origin/dest
                  for the first/last hop.
    """
    if not raw_text:
        return []

    segments: list[Segment] = []
    # Split into directions by detecting "<IATA>\n-\n<IATA>" pattern.
    # Kayak puts the global O→D of each direction at the bottom of its block.
    # Easier approach: split on the long-duration line (e.g. "18h 52m") followed
    # by 3 codes.
    # We use a different approach: find all "<IATA>\n -\n <IATA>" blocks; each
    # is a direction marker. Before it sits the segment-by-segment text.

    # Normalize whitespace
    lines = [l.strip(",").strip() for l in raw_text.split("\n") if l.strip()]

    # Find direction blocks: indices where lines[i], lines[i+1], lines[i+2] = (IATA, '-', IATA)
    direction_anchors: list[tuple[int, str, str]] = []
    for i in range(len(lines) - 2):
        if (re.fullmatch(r"[A-Z]{3}", lines[i] or "")
            and lines[i + 1] == "-"
            and re.fullmatch(r"[A-Z]{3}", lines[i + 2] or "")):
            direction_anchors.append((i, lines[i], lines[i + 2]))

    # Each direction "owns" lines from previous anchor end to its own anchor
    bounds: list[tuple[int, int, str, str]] = []
    prev_end = 0
    for idx, (i, o, d) in enumerate(direction_anchors):
        bounds.append((prev_end, i, o, d))
        prev_end = i + 3

    if not bounds:
        return []

    # We expect bounds count == len(planned_legs) (one direction per leg).
    # If counts mismatch, try to map by O&D.
    for dir_idx, (start, end, dir_o, dir_d) in enumerate(bounds):
        block = lines[start:end]
        # Determine the planned date for this direction
        plan = None
        if dir_idx < len(planned_legs):
            plan = planned_legs[dir_idx]
        if plan and (plan["origin"] != dir_o or plan["dest"] != dir_d):
            # try to find a matching planned leg
            for p in planned_legs:
                if p["origin"] == dir_o and p["dest"] == dir_d:
                    plan = p
                    break
        leg_date = plan["date"] if plan else ""

        # Within block, identify the segment sequence:
        # First line: "<dep> – <arr>[+N]"
        # Second line: airline name
        # Third line: "N stops" or "nonstop"
        # Then alternating: "<hub IATA>", "<NhNm layover, <Airport name>"
        # Final: total "<duration>"
        if not block:
            continue
        # Parse dep/arr from first line
        first = block[0]
        times = _TIME_RE.findall(first)
        dep_t = _norm_time(*times[0]) if times else ""
        arr_t = _norm_time(*times[1]) if len(times) >= 2 else ""

        airline = block[1] if len(block) > 1 else ""

        # Parse stop count
        stops = 0
        if len(block) > 2:
            m = re.search(r"(\d+)\s+stops?", block[2], re.I)
            if m:
                stops = int(m.group(1))
            elif re.search(r"\bnonstop\b|\bdirect\b", block[2], re.I):
                stops = 0

        # Collect hub IATA codes from subsequent lines
        hubs: list[str] = []
        for ln in block[3:]:
            if re.fullmatch(r"[A-Z]{3}", ln):
                hubs.append(ln)

        # Build segment chain: O → hub1 → hub2 → ... → D
        chain = [dir_o] + hubs + [dir_d]
        # If stops=0, chain should just be [O, D]; the airline operates one segment.
        # If stops=N, chain has N+1 segments.
        # Important: when nonstop, we may still get 1 hub if Kayak prints the airline
        # base; trim duplicates.
        # Deduplicate consecutive identical codes
        clean_chain: list[str] = []
        for c in chain:
            if not clean_chain or clean_chain[-1] != c:
                clean_chain.append(c)

        # Validate: expected segments == stops + 1
        if stops + 1 != len(clean_chain) - 1 and stops > 0:
            # parse not fully reliable for this card; fall back to 1 segment O→D
            clean_chain = [dir_o, dir_d]

        for k in range(len(clean_chain) - 1):
            seg = Segment(
                origin=clean_chain[k],
                dest=clean_chain[k + 1],
                date=leg_date,
                airline=airline,
                dep_time=dep_t if k == 0 else "",
                arr_time=arr_t if k == len(clean_chain) - 2 else "",
                source="kayak",
            )
            segments.append(seg)

    return segments


def parse_gf_quote(quote: dict) -> list[Segment]:
    """Parse a GF TripQuote dict. GF doesn't expose intermediate hubs in
    fast_flights output, BUT our `_search_via_hubs` synthesizes legs with
    the airline like 'Gol → LATAM (vía GIG)'. We can pull the hub out.
    """
    segments: list[Segment] = []
    for leg in quote.get("legs", []):
        airline_label = leg.get("airline", "")
        # If contains 'vía <CODE>', split
        m = re.search(r"vía\s+([A-Z]{3})", airline_label)
        if m:
            hub = m.group(1)
            parts = re.split(r"\s*→\s*", airline_label.split("(")[0].strip())
            if len(parts) == 2:
                a, b = parts
                segments.append(Segment(
                    origin=leg["origin"], dest=hub, date=leg["leg_date"],
                    airline=a.strip(), source="google_flights",
                ))
                segments.append(Segment(
                    origin=hub, dest=leg["dest"], date=leg["leg_date"],
                    airline=b.strip(), source="google_flights",
                ))
                continue
        # Direct or unknown composition
        if leg.get("stops", 0) == 0:
            segments.append(Segment(
                origin=leg["origin"], dest=leg["dest"], date=leg["leg_date"],
                airline=airline_label, source="google_flights",
            ))
        else:
            # Direct but with N stops we don't know hubs for. Record the leg
            # as one "with stops" placeholder.
            segments.append(Segment(
                origin=leg["origin"], dest=leg["dest"], date=leg["leg_date"],
                airline=airline_label + f" ({leg['stops']} esc)",
                source="google_flights",
            ))
    return segments


def collect_all_segments(snapshot: dict) -> list[Segment]:
    """Walk an entire snapshot result dict and return all unique segments
    discovered across GF + Kayak + Booking."""
    all_segs: list[Segment] = []
    planned_legs = snapshot.get("legs", [])

    # GF
    gf = (snapshot.get("sources") or {}).get("google_flights") or {}
    for q in gf.get("quotes", []) or []:
        all_segs.extend(parse_gf_quote(q))

    # Kayak
    ky = (snapshot.get("sources") or {}).get("kayak") or {}
    for q in ky.get("quotes", []) or []:
        raw = q.get("raw_text") or ""
        all_segs.extend(parse_kayak_quote(raw, planned_legs))

    # Booking — usually 0 quotes, skip

    # Deduplicate by (origin, dest, date, airline)
    seen = set()
    unique: list[Segment] = []
    for s in all_segs:
        # Normalize airline name (strip whitespace, lowercase for matching)
        key = (s.origin, s.dest, s.date, s.airline.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(s)

    return unique


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) < 2:
        print("usage: segments.py <snapshot.json>")
        sys.exit(1)
    snap = json.load(open(sys.argv[1]))
    segs = collect_all_segments(snap)
    print(f"discovered {len(segs)} unique segments:")
    for s in segs:
        print(f"  {s.source:8s}  {s.date}  {s.origin}→{s.dest:3s}  {s.airline}  "
              f"{s.dep_time}-{s.arr_time}")
