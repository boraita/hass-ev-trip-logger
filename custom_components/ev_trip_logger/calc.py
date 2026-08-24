"""Pure helpers behind the trip/charge state machine.

Split out of `coordinator.py` in v0.8.31. Everything here is a function
of its arguments alone — no Home Assistant object, no coordinator state,
no I/O — which is exactly why it belongs in its own module: these are the
parts worth testing directly, and they were the only 135 lines of that
7 800-line file that could be read without holding the state machine in
your head.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from .const import DEFAULT_SECONDARY_HOME_RADIUS_M, DRIVER_NONE_STATES

#: Entity states that carry no usable reading. Lives here rather than in
#: `coordinator` because the helpers below need it and a constant should
#: sit with its most primitive consumer.
INVALID_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""}

# Tracker states that carry no zone information. When origin/destination
# resolves to one of these, the GPS-coords zone fallback kicks in
# (v0.5.44) so journey open/close logic isn't blinded by a stale tracker.
NON_ZONE_STATES = frozenset({"not_home", "unknown", "unavailable", "none", ""})


def is_zoneless(location: str | None) -> bool:
    """True when the tracker state names no zone (not_home/unknown/...)."""
    return not location or location.strip().casefold() in NON_ZONE_STATES


def haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance between two lat/lon pairs in km. Mean
    Earth radius 6371 km. Cheap (no external deps).
    """
    from math import radians, sin, cos, sqrt, atan2  # noqa: PLC0415
    r1 = radians(lat1)
    r2 = radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(r1) * cos(r2) * sin(dlam / 2) ** 2
    return 2 * 6371.0 * atan2(sqrt(a), sqrt(1 - a))


def parse_secondary_home_coords(
    raw: str | None,
) -> list[tuple[float, float, float, str]]:
    """Parse CONF_SECONDARY_HOME_COORDS free text into (lat, lon, radius_m, label).

    One entry per line (blank lines / '#' comments ignored):
    "lat,lon", "lat,lon,radius_m", or "lat,lon,radius_m,label". Radius
    defaults to DEFAULT_SECONDARY_HOME_RADIUS_M when omitted; label
    defaults to "secondary_home_<n>" (n = 1-based line position among
    valid entries) so a coordinate-matched trip still gets a real
    destination string instead of staying "not_home". Malformed lines
    are skipped rather than raising, since this is user-typed free text
    with no schema validation.
    """
    if not raw:
        return []
    out: list[tuple[float, float, float, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            lat = float(parts[0])
            lon = float(parts[1])
            radius = float(parts[2]) if len(parts) >= 3 and parts[2] else DEFAULT_SECONDARY_HOME_RADIUS_M
        except (TypeError, ValueError):
            continue
        label = parts[3] if len(parts) >= 4 and parts[3] else f"secondary_home_{len(out) + 1}"
        out.append((lat, lon, radius, label))
    return out


def route_distance_km(
    samples: Sequence[tuple[Any, float, float]],
) -> float | None:
    """Sum haversine segments across a sequence of (ts, lat, lon).
    None if fewer than 2 points. Accepts any indexable sequence (list,
    deque) of (ts, lat, lon)-shaped tuples.
    """
    if not samples or len(samples) < 2:
        return None
    total = 0.0
    for i in range(1, len(samples)):
        _, lat1, lon1 = samples[i - 1]
        _, lat2, lon2 = samples[i]
        total += haversine_km(lat1, lon1, lat2, lon2)
    return total
def pick_driver_for_window(
    timeline: Sequence[tuple[datetime, str]],
    start: datetime,
    end: datetime,
) -> str | None:
    """Pick the driver active during [start, end] from sensor history.

    `timeline` is the driver sensor's (timestamp, state) changes sorted
    ascending; it may begin before `start` (the state already active at
    window open). Returns the valid driver name with the longest overlap
    with the window, or None when nobody valid was connected.

    v0.5.44 — needed because on cloud-polled cars (BYD) vehicle_on
    rarely flips, so most trips take the synthetic path which never ran
    the live driver capture.
    """
    overlap: dict[str, float] = {}
    for i, (ts, raw) in enumerate(timeline):
        seg_start = max(ts, start)
        seg_end = min(timeline[i + 1][0], end) if i + 1 < len(timeline) else end
        if seg_end <= seg_start:
            continue
        cleaned = (raw or "").strip()
        if (
            not cleaned
            or cleaned in INVALID_STATES
            or cleaned.casefold() in DRIVER_NONE_STATES
        ):
            continue
        overlap[cleaned] = (
            overlap.get(cleaned, 0.0) + (seg_end - seg_start).total_seconds()
        )
    if not overlap:
        return None
    return max(overlap, key=lambda k: overlap[k])


def speed_stats(
    samples: Sequence[float],
    *,
    highway_threshold_kmh: float,
) -> tuple[float | None, float | None]:
    """v0.7.3 — return `(v95_speed_kmh, highway_ratio_pct)` from the
    live-tick speed sample deque.

    * `v95_speed_kmh` — 95th-percentile of samples ≥ 0. Uses the
      classic linear-interpolation nearest-rank definition: for n
      samples, idx = ceil(0.95 × n) − 1, clamped to n − 1. Robust
      to a single sensor spike (max_speed_kmh is already elsewhere).
    * `highway_ratio_pct` — fraction of samples ≥ `highway_threshold_kmh`
      × 100. Useful for the dashboard's "urban vs autopista" split.

    Both return None when the deque is empty (no speed sensor wired,
    or the trip closed before the first live-tick fired).
    """
    if not samples:
        return (None, None)
    ordered = sorted(s for s in samples if s is not None and s >= 0)
    if not ordered:
        return (None, None)
    n = len(ordered)
    idx = max(0, min(n - 1, int(0.95 * n) - (1 if 0.95 * n == int(0.95 * n) else 0)))
    # Simpler & bias-free: nearest-rank on the ceil convention.
    idx = min(n - 1, max(0, math.ceil(0.95 * n) - 1))
    v95 = ordered[idx]
    highway = sum(1 for s in ordered if s >= highway_threshold_kmh)
    highway_pct = highway / n * 100.0
    return (round(v95, 1), round(highway_pct, 1))
