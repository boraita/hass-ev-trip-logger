"""Tests for the ABRP telemetry payload builder."""
from __future__ import annotations

import pytest

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


def test_soe_derived_from_soc_and_capacity() -> None:
    """v0.8.1 — soe (present energy, kWh) is free to derive from two
    fields we already send; ABRP accepts it as a lower-priority field.
    """
    tlm = _base(soc=55.0, capacity=80.0)
    assert tlm["soe"] == pytest.approx(44.0)


def test_soe_omitted_without_capacity() -> None:
    assert "soe" not in _base(soc=55.0)


def test_cabin_hvac_and_tire_fields_included_when_present() -> None:
    """v0.8.7 — cabin temp, HVAC setpoint, and tire pressures (already
    converted to kPa by the caller) pass through when supplied.
    """
    tlm = _base(
        cabin_temp=22.3, hvac_setpoint=21.0,
        tire_pressure_fl=220.5, tire_pressure_fr=219.8,
        tire_pressure_rl=225.1, tire_pressure_rr=224.7,
    )
    assert tlm["cabin_temp"] == 22.3
    assert tlm["hvac_setpoint"] == 21.0
    assert tlm["tire_pressure_fl"] == 220.5
    assert tlm["tire_pressure_fr"] == 219.8
    assert tlm["tire_pressure_rl"] == 225.1
    assert tlm["tire_pressure_rr"] == 224.7


def test_cabin_hvac_and_tire_fields_dropped_when_none() -> None:
    tlm = _base()
    for k in (
        "cabin_temp", "hvac_setpoint", "tire_pressure_fl",
        "tire_pressure_fr", "tire_pressure_rl", "tire_pressure_rr",
    ):
        assert k not in tlm


def test_power_sign_discharge_positive_charge_negative() -> None:
    """ABRP convention: +discharge / -charge. build_tlm's input is the
    opposite (-discharge / +charge) so it can negate once and land on
    ABRP's convention.
    """
    assert _base(power_w=-5000.0)["power"] == pytest.approx(5.0)  # discharge
    assert _base(power_w=3000.0)["power"] == pytest.approx(-3.0)  # charge
