"""Elevation lookup + trip-level elevation statistics.

v0.7.5 — surfaces `elevation_gain_m`, `elevation_loss_m`, and
`elevation_variance_m2` per trip. Chalmers 2024 QRNN paper on 91 932
real-world EV trips ranked elevation variance as the 3rd-strongest
feature for trip-level consumption prediction (Spearman ρ ≈ 0.39),
just behind wind variance and distance. Free elevation data is
available from open-elevation.com (default), opentopodata.org, or a
user-hostable OpenTopoData instance; the integration keeps a single
async HTTP path with a hard timeout so a slow / down provider never
blocks trip close.

Privacy: enabling this feature sends the trip's GPS route points to
the configured external service. The default provider is
open-elevation.com. Users can point at their own OpenTopoData
instance via `CONF_ELEVATION_PROVIDER_URL` to keep data local.
"""
from __future__ import annotations

import itertools
import logging
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

# Free public endpoints. All return `{"results": [{"elevation": m, ...},
# ...]}` in input order, but the REQUEST shape differs: open-elevation
# wants a list of {"latitude", "longitude"} dicts, OpenTopoData wants a
# single pipe-delimited "lat,lon|lat,lon|..." string (see
# `_build_payload` below) — sending the open-elevation shape to
# OpenTopoData gets a 400 `INVALID_REQUEST` on every request (#11).
_PROVIDER_URLS: dict[str, str] = {
    "open-elevation": "https://api.open-elevation.com/api/v1/lookup",
    "opentopodata-eudem": "https://api.opentopodata.org/v1/eudem25m",
    "opentopodata-srtm": "https://api.opentopodata.org/v1/srtm30m",
}

# Public APIs cap query size. Downsampling is cheap and 30 evenly-
# spaced points captures the elevation profile of any real trip well
# (median segment ≈ 500 m over a 15 km drive). Keeps the payload
# under any provider's per-request point cap.
_MAX_POINTS_PER_REQUEST: int = 30

# Hard timeout — better a NULL elevation stat than a blocked close.
_HTTP_TIMEOUT = ClientTimeout(total=8.0)


def _resolve_provider_url(
    provider: str, override_url: str | None,
) -> str | None:
    """Pick the endpoint. Explicit override wins so users can point
    at a self-hosted OpenTopoData instance (`https://my.tile/v1/eudem25m`)
    without waiting for a code change to add it to the built-in list.
    Returns None when neither is available."""
    if override_url:
        return override_url
    return _PROVIDER_URLS.get(provider)


def _build_payload(
    provider: str, points: list[tuple[float, float]],
) -> dict[str, Any]:
    """Per-provider request body (#11). OpenTopoData — including a
    self-hosted instance reached via `provider_url`, since the
    `provider` select still says which API shape it speaks — takes a
    single pipe-delimited "lat,lon|lat,lon|..." string; every other
    provider (open-elevation) takes the list-of-dicts shape.
    """
    if provider.startswith("opentopodata"):
        return {
            "locations": "|".join(f"{lat},{lon}" for lat, lon in points),
        }
    return {
        "locations": [
            {"latitude": lat, "longitude": lon} for lat, lon in points
        ],
    }


def downsample_route(
    points: list[tuple[float, float]], *, max_points: int = _MAX_POINTS_PER_REQUEST,
) -> list[tuple[float, float]]:
    """Evenly-spaced downsample of a GPS route to at most `max_points`.

    Preserves the first and last points (start / end of trip) so the
    elevation delta between start and end is always exact. Between
    them we pick indices via integer stride; on very short routes
    the result may be < max_points which is fine.
    """
    if not points:
        return []
    n = len(points)
    if n <= max_points:
        return list(points)
    # Stride sampling: always keep index 0 and n-1.
    step = (n - 1) / (max_points - 1)
    sampled: list[tuple[float, float]] = []
    for i in range(max_points):
        idx = round(i * step)
        idx = min(idx, n - 1)
        sampled.append(points[idx])
    # Dedup (stride collisions can produce consecutive identical rows).
    unique: list[tuple[float, float]] = []
    for p in sampled:
        if not unique or unique[-1] != p:
            unique.append(p)
    return unique


async def fetch_elevations(
    points: list[tuple[float, float]],
    *,
    provider: str,
    provider_url: str | None,
    session: ClientSession,
) -> list[float] | None:
    """POST `points` to the elevation provider; return metres for
    each input point in the same order.

    None on any failure (network, non-200, malformed response) — the
    caller decides how to degrade (typically: skip the elevation
    columns for this trip, keep the rest of the close path healthy).
    """
    url = _resolve_provider_url(provider, provider_url)
    if not url or not points:
        return None
    payload = _build_payload(provider, points)
    try:
        async with session.post(
            url, json=payload, timeout=_HTTP_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                # v0.8.11 — a 4xx is a config/request-shape bug, not a
                # transient failure, and repeats on every trip close
                # forever (#11's opentopodata payload mismatch went
                # unnoticed for a month at INFO level). WARNING so it
                # surfaces without custom logger config.
                log = _LOGGER.warning if 400 <= resp.status < 500 else _LOGGER.info
                log(
                    "Elevation provider %s returned HTTP %s; skipping",
                    provider, resp.status,
                )
                return None
            body = await resp.json()
    except (ClientError, TimeoutError, Exception) as exc:
        _LOGGER.info(
            "Elevation fetch failed for provider=%s: %s (skipped)",
            provider, exc,
        )
        return None
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or len(results) != len(points):
        _LOGGER.info(
            "Elevation provider %s returned unexpected shape: %s items "
            "for %d points (skipped)",
            provider, len(results) if isinstance(results, list) else "?",
            len(points),
        )
        return None
    elevations: list[float] = []
    for r in results:
        try:
            elevations.append(float(r.get("elevation")))
        except (TypeError, ValueError):
            return None
    return elevations


def compute_elevation_stats(
    elevations: list[float] | None,
) -> tuple[float | None, float | None, float | None]:
    """Return `(gain_m, loss_m, variance_m2)` from an elevation profile.

    `gain_m` sums positive segment deltas (Σ max(0, e[i+1] − e[i]));
    `loss_m` sums the magnitude of negative deltas. `variance_m2` is
    the sample variance of the profile itself — Chalmers' feature.
    Returns `(None, None, None)` when the profile is empty or has a
    single point.
    """
    if not elevations or len(elevations) < 2:
        return (None, None, None)
    gain = 0.0
    loss = 0.0
    for a, b in itertools.pairwise(elevations):
        delta = b - a
        if delta > 0:
            gain += delta
        elif delta < 0:
            loss += -delta
    # Sample variance (Bessel-corrected).
    mean = sum(elevations) / len(elevations)
    var = (
        sum((e - mean) ** 2 for e in elevations) / (len(elevations) - 1)
        if len(elevations) > 1 else 0.0
    )
    return (round(gain, 1), round(loss, 1), round(var, 1))
