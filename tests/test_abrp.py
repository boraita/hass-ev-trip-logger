"""Tests for the ABRP telemetry payload builder."""
from __future__ import annotations

from custom_components.ev_trip_logger.abrp import build_tlm


def _base(**over):
    args = dict(
        soc=55.0, power_w=None, speed=None, lat=None, lon=None,
        is_charging=None, is_parked=None, ext_temp=None, est_range=None,
        odometer=None, car_model=None,
    )
    args.update(over)
    return build_tlm(**args)


def test_new_fields_included_when_present() -> None:
    tlm = _base(
        est_range=312.0, heading=181.4, soh=93.2, capacity=80.55,
        kwh_charged=4.318,
    )
    assert tlm["est_battery_range"] == 312.0
    assert tlm["heading"] == 181.4
    assert tlm["soh"] == 93.2
    assert tlm["capacity"] == 80.55
    assert tlm["kwh_charged"] == 4.32  # rounded to 2 dp


def test_new_fields_dropped_when_none_or_nonpositive() -> None:
    tlm = _base(heading=None, soh=None, capacity=0, kwh_charged=0)
    for k in ("heading", "soh", "capacity", "kwh_charged", "est_battery_range"):
        assert k not in tlm


def test_heading_normalised_into_0_360() -> None:
    assert _base(heading=365.0)["heading"] == 5.0
    assert _base(heading=-1.0)["heading"] == 359.0


def test_soc_always_present_baseline() -> None:
    assert _base()["soc"] == 55.0
    assert "capacity" not in _base()  # not sent unless provided
