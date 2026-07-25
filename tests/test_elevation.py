"""Tests for the v0.7.5 elevation module — pure functions, no HTTP."""
from __future__ import annotations

import pytest

from custom_components.ev_trip_logger.elevation import (
    compute_elevation_stats,
    downsample_route,
)


def test_compute_elevation_stats_basic_gain_and_loss() -> None:
    """gain + loss are cumulative segment deltas, variance is Bessel."""
    profile = [100.0, 120.0, 150.0, 140.0, 160.0]
    gain, loss, var = compute_elevation_stats(profile)
    # +20, +30, -10, +20 → gain 70, loss 10
    assert gain == pytest.approx(70.0)
    assert loss == pytest.approx(10.0)
    # Sample variance (n-1) for [100, 120, 150, 140, 160]:
    # mean=134, deviations²=[1156, 196, 256, 36, 676], sum=2320,
    # /(5-1)=580 → 580.0
    assert var == pytest.approx(580.0, abs=0.1)


def test_compute_elevation_stats_empty_and_single_point() -> None:
    """Guardrails: 0 or 1 elevation samples → all None."""
    assert compute_elevation_stats([]) == (None, None, None)
    assert compute_elevation_stats(None) == (None, None, None)
    assert compute_elevation_stats([500.0]) == (None, None, None)


def test_compute_elevation_stats_flat_route_zero_gain_zero_var() -> None:
    """Elevation profile that never changes → gain=loss=var=0."""
    profile = [50.0] * 8
    gain, loss, var = compute_elevation_stats(profile)
    assert gain == 0.0
    assert loss == 0.0
    assert var == 0.0


def test_downsample_route_preserves_endpoints_and_caps_length() -> None:
    """Downsample must keep the exact first and last GPS points so the
    net elevation delta of the trip is always correctly bounded.
    """
    # 100 points along a diagonal.
    pts = [(37.0 + i * 0.001, -3.6 + i * 0.001) for i in range(100)]
    sampled = downsample_route(pts, max_points=20)
    assert len(sampled) <= 20
    assert sampled[0] == pts[0]
    assert sampled[-1] == pts[-1]


def test_downsample_route_short_input_returns_as_is() -> None:
    """If input already fits under the cap, don't touch it."""
    pts = [(37.0, -3.6), (37.001, -3.601), (37.002, -3.602)]
    assert downsample_route(pts, max_points=30) == pts


def test_downsample_route_dedups_consecutive_identical_hits() -> None:
    """Stride collisions on very small max_points can double-pick a
    point — the helper must not emit consecutive duplicates.
    """
    pts = [(37.0, -3.6)] * 10 + [(37.1, -3.7)]  # 10 identical + 1
    sampled = downsample_route(pts, max_points=5)
    # No two consecutive entries should be equal.
    for a, b in zip(sampled, sampled[1:]):
        assert a != b
