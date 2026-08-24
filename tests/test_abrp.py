"""Tests for the ABRP telemetry payload builder."""
from __future__ import annotations

import pytest

from custom_components.ev_trip_logger.abrp import build_tlm


def _base(**over):
    args = {
        "soc": 55.0, "power_w": None, "speed": None, "lat": None, "lon": None,
        "is_charging": None, "is_parked": None, "ext_temp": None, "est_range": None,
        "odometer": None, "car_model": None,
    }
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


# --------------------------------------------------------------------------
# AbrpClient.send() — response-body handling.
#
# The Iternio Telemetry API documents that HTTP status codes are only used
# for serious errors (bad API key, malformed call); everything else comes
# back as HTTP 200 with a JSON body carrying `status` ("ok" / "error") and,
# when it is not ok, an `errors` property. A client that only looks at the
# HTTP status therefore counts rejected samples as delivered.
# --------------------------------------------------------------------------


class _FakeResponse:
    """Minimal aiohttp-response stand-in usable as an async CM."""

    def __init__(self, status, payload=None, *, json_raises=False):
        self.status = status
        self._payload = payload
        self._json_raises = json_raises

    async def json(self, content_type=None):
        if self._json_raises:
            raise ValueError("not json")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def _next(self, url, params):
        self.calls.append((url, params))
        return self._responses.pop(0)

    def post(self, url, *, params=None, timeout=None):
        return self._next(url, params or {})

    def get(self, url, *, params=None, timeout=None):
        return self._next(url, params or {})


def _client(*responses):
    from custom_components.ev_trip_logger.abrp import AbrpClient

    session = _FakeSession(*responses)
    return AbrpClient(session, "key", "token"), session


async def test_send_accepts_status_ok() -> None:
    client, _ = _client(_FakeResponse(200, {"status": "ok"}))
    assert await client.send({"soc": 50.0}) is True
    assert client.last_error is None
    assert client.last_sent_at is not None


async def test_send_rejects_error_status_on_http_200() -> None:
    """A rejected sample must not be reported as delivered."""
    client, _ = _client(
        _FakeResponse(
            200,
            {"status": "error", "errors": ["Unknown car_model 'byd:sealion'"]},
        )
    )
    assert await client.send({"soc": 50.0}) is False
    assert client.last_sent_at is None
    assert client.last_error == "Unknown car_model 'byd:sealion'"


async def test_send_error_omits_the_redundant_error_prefix() -> None:
    """`last_error` is rendered verbatim by the dashboard card, so a bare
    "error:" in front of the reason is noise. A status that carries its own
    information is kept."""
    client, _ = _client(
        _FakeResponse(200, {"status": "error", "errors": ["bad slug"]}),
        _FakeResponse(200, {"status": "rate_limited", "errors": ["slow down"]}),
    )
    await client.send({"soc": 50.0})
    assert client.last_error == "bad slug"
    client._fail_count = 0
    await client.send({"soc": 50.0})
    assert client.last_error == "rate_limited: slow down"


async def test_send_error_status_feeds_the_backoff() -> None:
    """Consecutive rejections must eventually suppress sending, exactly as
    transport failures do — otherwise a permanently bad payload is retried
    at full rate forever."""
    from custom_components.ev_trip_logger.abrp import _BACKOFF_THRESHOLD

    bad = [
        _FakeResponse(200, {"status": "error", "errors": ["nope"]})
        for _ in range(_BACKOFF_THRESHOLD)
    ]
    client, session = _client(*bad)
    for _ in range(_BACKOFF_THRESHOLD):
        assert await client.send({"soc": 50.0}) is False
    assert len(session.calls) == _BACKOFF_THRESHOLD
    # Suppressed now: no further HTTP call is made.
    assert await client.send({"soc": 50.0}) is False
    assert len(session.calls) == _BACKOFF_THRESHOLD


async def test_send_treats_unreadable_body_as_delivered() -> None:
    """A 200 whose body we cannot parse (proxy stripped it, empty response)
    stays a success — we only fail on a positively-read non-ok status."""
    client, _ = _client(_FakeResponse(200, json_raises=True))
    assert await client.send({"soc": 50.0}) is True
    assert client.last_error is None


async def test_send_clears_last_error_after_recovery() -> None:
    client, _ = _client(
        _FakeResponse(200, {"status": "error", "errors": ["nope"]}),
        _FakeResponse(200, {"status": "ok"}),
    )
    assert await client.send({"soc": 50.0}) is False
    assert client.last_error is not None
    assert await client.send({"soc": 50.0}) is True
    assert client.last_error is None
