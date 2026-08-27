"""A Better Route Planner (ABRP / Iternio) telemetry push + next-charge read.

Optional and credential-gated: only active when the user supplies an ABRP
generic *user token* (and an *api_key*). Telemetry is pushed off the existing
coordinator updates — we never start an independent timer that would force
extra upstream polls. Most cloud-polled EV integrations (BYD's shared account,
Tesla Fleet, OVMS) have rate limits or shared quotas; piggybacking on the
coordinator's existing fetches keeps this integration a passive consumer of
whatever the user's upstream source already produces.

The next-charge target SoC is read from ``/tlm/get_next_charge``, which only
returns data while an ABRP route is active.

API reference: https://documenter.getpostman.com/view/7396339/SWTK5a8w
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

ABRP_BASE = "https://api.iternio.com/1"
SEND_ENDPOINT = f"{ABRP_BASE}/tlm/send"
NEXT_CHARGE_ENDPOINT = f"{ABRP_BASE}/tlm/get_next_charge"

#: Above this charge power we flag the session as DC fast charging.
DC_FAST_THRESHOLD_KW = 11.0

#: A bad token (HTTP 401) is unrecoverable without user action — back off hard.
_AUTH_FAIL_PAUSE_S = 600.0
#: Start backing off after this many consecutive transient failures.
_BACKOFF_THRESHOLD = 10
_BACKOFF_MAX_S = 300.0
#: HTTP request timeout. Without it, a hung TLS handshake on the first
#: push after a network blip or after the switch turns on can stall the
#: caller for tens of seconds (aiohttp's default is no total timeout).
_HTTP_TIMEOUT = ClientTimeout(total=10.0)


def build_tlm(
    *,
    soc: float | None,
    power_w: float | None,
    speed: float | None,
    lat: float | None,
    lon: float | None,
    is_charging: bool | None,
    is_parked: bool | None,
    ext_temp: float | None,
    est_range: float | None,
    odometer: float | None,
    car_model: str | None,
    heading: float | None = None,
    soh: float | None = None,
    capacity: float | None = None,
    kwh_charged: float | None = None,
    cabin_temp: float | None = None,
    hvac_setpoint: float | None = None,
    tire_pressure_fl: float | None = None,
    tire_pressure_fr: float | None = None,
    tire_pressure_rl: float | None = None,
    tire_pressure_rr: float | None = None,
) -> dict[str, Any]:
    """Build the ABRP ``tlm`` payload from primitives; ``None`` values dropped.

    Sign note: ABRP ``power`` is **+discharge / -charge** (kW). We negate
    the watts we receive so the caller can pass in the standard EV
    convention (+charging / -discharging) regardless of vendor — that
    matches what some cloud sources emit natively (e.g. BYD's
    ``realtime.gl``). For sensors that report the opposite sign, the
    user enables ``CONF_POWER_SIGN_INVERTED`` upstream so this function
    keeps a single negation rule. Verify the sign once against a live
    drive/charge on first deploy.
    """
    tlm: dict[str, Any] = {"utc": int(time.time())}
    if soc is not None:
        tlm["soc"] = round(float(soc), 1)
    if power_w is not None:
        tlm["power"] = round(-float(power_w) / 1000.0, 3)
    if speed is not None:
        tlm["speed"] = round(float(speed), 1)
    if lat is not None and lon is not None:
        tlm["lat"] = lat
        tlm["lon"] = lon
    if is_charging is not None:
        tlm["is_charging"] = 1 if is_charging else 0
        if is_charging and power_w is not None:
            kw = abs(float(power_w)) / 1000.0
            tlm["is_dcfc"] = 1 if kw > DC_FAST_THRESHOLD_KW else 0
    if is_parked is not None:
        tlm["is_parked"] = 1 if is_parked else 0
    if ext_temp is not None:
        tlm["ext_temp"] = round(float(ext_temp), 1)
    if est_range is not None:
        tlm["est_battery_range"] = round(float(est_range), 1)
    if odometer is not None:
        tlm["odometer"] = round(float(odometer), 1)
    # heading: GPS course 0-360°; only meaningful while moving but harmless
    # parked. Normalised into range so a stray -1/361 sentinel can't leak.
    if heading is not None:
        tlm["heading"] = round(float(heading) % 360.0, 1)
    # soh: state of health %. NOTE this is a *modelled/estimated* value on
    # cloud-only vehicles (no BMS SoH over the API), sent as a best-effort
    # hint for ABRP's range model — not a measured figure.
    if soh is not None:
        tlm["soh"] = round(float(soh), 1)
    # capacity: usable pack capacity (kWh), from the calibrated estimate.
    if capacity is not None and capacity > 0:
        tlm["capacity"] = round(float(capacity), 2)
        # soe: present energy (kWh) = soc% x capacity. Free to derive from
        # two fields we already send; ABRP accepts it as a lower-priority
        # telemetry field alongside soc/capacity.
        if soc is not None:
            tlm["soe"] = round(float(soc) / 100.0 * float(capacity), 2)
    # kwh_charged: energy added so far in the active charge session.
    if kwh_charged is not None and kwh_charged > 0:
        tlm["kwh_charged"] = round(float(kwh_charged), 2)
    # v0.8.7 — cabin temp / HVAC setpoint (°C) and tire pressures (kPa,
    # already converted by the caller from whatever unit the source
    # sensor reports). All optional/car-dependent.
    if cabin_temp is not None:
        tlm["cabin_temp"] = round(float(cabin_temp), 1)
    if hvac_setpoint is not None:
        tlm["hvac_setpoint"] = round(float(hvac_setpoint), 1)
    if tire_pressure_fl is not None:
        tlm["tire_pressure_fl"] = round(float(tire_pressure_fl), 1)
    if tire_pressure_fr is not None:
        tlm["tire_pressure_fr"] = round(float(tire_pressure_fr), 1)
    if tire_pressure_rl is not None:
        tlm["tire_pressure_rl"] = round(float(tire_pressure_rl), 1)
    if tire_pressure_rr is not None:
        tlm["tire_pressure_rr"] = round(float(tire_pressure_rr), 1)
    if car_model:
        tlm["car_model"] = car_model
    return tlm


class AbrpClient:
    """Thin async client for the ABRP telemetry API with simple backoff."""

    def __init__(self, session: ClientSession, api_key: str, token: str) -> None:
        self._session = session
        self._api_key = api_key
        self._token = token
        self._fail_count = 0
        self._suppress_until = 0.0
        #: Last next-charge target SoC read from ABRP (None when no active route).
        self.next_charge_soc: int | None = None
        #: v0.8.47 — WHY there is no next-charge value. Until now the
        #: getter returned None for "ABRP answered, no route planned" and
        #: for "the call never got through", and a UI cannot tell those
        #: apart — it ends up asserting "no active route" when the real
        #: answer is a dead token. One of: "ok", "no_route",
        #: "http_<code>", "network", "unparsed", or None before the first
        #: attempt.
        self.next_charge_status: str | None = None
        self.next_charge_checked_at: float | None = None
        #: Timestamp of the last successful send (for diagnostics).
        self.last_sent_at: float | None = None
        #: Last rejection reported by ABRP in a 200 response body, or None
        #: when the most recent send was accepted (for diagnostics).
        self.last_error: str | None = None

    @property
    def _suppressed(self) -> bool:
        return time.monotonic() < self._suppress_until

    def _note_failure(self) -> None:
        self._fail_count += 1
        if self._fail_count >= _BACKOFF_THRESHOLD:
            over = self._fail_count - _BACKOFF_THRESHOLD
            delay = min(_BACKOFF_MAX_S, 30.0 * (2.0**over))
            self._suppress_until = time.monotonic() + delay
            _LOGGER.debug(
                "ABRP backing off %.0fs after %d failures", delay, self._fail_count
            )

    async def send(self, tlm: dict[str, Any]) -> bool:
        """POST one telemetry sample. Returns True on HTTP 200."""
        if self._suppressed:
            return False
        params = {
            "api_key": self._api_key,
            "token": self._token,
            "tlm": json.dumps(tlm, separators=(",", ":")),
        }
        try:
            async with self._session.post(
                SEND_ENDPOINT, params=params, timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status == 401:
                    self._suppress_until = time.monotonic() + _AUTH_FAIL_PAUSE_S
                    _LOGGER.warning(
                        "ABRP rejected the token (HTTP 401) — pausing %.0f min; "
                        "check the ABRP user token",
                        _AUTH_FAIL_PAUSE_S / 60.0,
                    )
                    return False
                if resp.status != 200:
                    _LOGGER.debug("ABRP send HTTP %s", resp.status)
                    self._note_failure()
                    return False
                # The API only uses HTTP status codes for serious errors
                # (bad key, malformed call). A *rejected sample* comes back
                # as 200 with {"status": "error", "errors": [...]} — so a
                # client that stops at the status code silently reports
                # undelivered telemetry as delivered, never backs off, and
                # leaves the user with no signal at all. Read the body.
                try:
                    body = await resp.json(content_type=None)
                except (ClientError, ValueError) as exc:
                    # Unparseable/absent body (proxy stripped it, empty
                    # response). We only fail on a positively-read non-ok
                    # status, so treat this as delivered.
                    _LOGGER.debug("ABRP send: unreadable 200 body: %s", exc)
                    body = None
                error = _reject_reason(body)
                if error is not None:
                    self.last_error = error
                    _LOGGER.warning("ABRP rejected the telemetry: %s", error)
                    self._note_failure()
                    return False
                self._fail_count = 0
                self.last_error = None
                self.last_sent_at = time.time()
                return True
        except ClientError as exc:
            _LOGGER.debug("ABRP send failed: %s", exc)
            self._note_failure()
            return False

    async def refresh_next_charge(self) -> int | None:
        """Read the next-charge target SoC (only set while a route is active).

        v0.8.47 — every exit now records `next_charge_status` as well.
        Previously a failed call and an idle planner both returned None,
        so the dashboard rendered "no active route in ABRP" for a dead
        token, a network drop and a genuine absence of route alike. That
        is a claim we had no basis for: only the `no_route` branch knows
        anything about the planner.

        The value is deliberately NOT cleared on a transient failure — a
        target read a minute ago is better than a blank while the network
        hiccups — but the status says the reading is stale.
        """
        if self._suppressed:
            self.next_charge_status = "suppressed"
            return self.next_charge_soc
        params = {"api_key": self._api_key, "token": self._token}
        try:
            async with self._session.get(
                NEXT_CHARGE_ENDPOINT, params=params, timeout=_HTTP_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    self.next_charge_status = f"http_{resp.status}"
                    self.next_charge_checked_at = time.time()
                    _LOGGER.debug(
                        "ABRP get_next_charge: HTTP %s", resp.status
                    )
                    return self.next_charge_soc
                data = await resp.json(content_type=None)
        except (ClientError, ValueError) as exc:
            self.next_charge_status = "network"
            self.next_charge_checked_at = time.time()
            _LOGGER.debug("ABRP get_next_charge failed: %s", exc)
            return self.next_charge_soc
        self.next_charge_checked_at = time.time()
        parsed = _parse_next_charge(data)
        if parsed is not None:
            self.next_charge_soc = parsed
            self.next_charge_status = "ok"
        elif isinstance(data, dict):
            # A well-formed answer carrying no target: the planner really
            # has no route running. This is the ONLY branch entitled to
            # say so, and it is the one that clears a stale value.
            self.next_charge_soc = None
            self.next_charge_status = "no_route"
        else:
            self.next_charge_status = "unparsed"
        return self.next_charge_soc


def _reject_reason(data: Any) -> str | None:
    """Return why ABRP rejected a sample, or None when it accepted it.

    The documented envelope is ``{"status": "ok" | "error", ...}`` with an
    ``errors`` property when the status is not ok. We deliberately only
    report a rejection when we can positively read a non-ok status: an
    absent or unrecognised body must not turn a working push into a
    permanent failure.
    """
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if not isinstance(status, str) or status.lower() == "ok":
        return None
    errors = data.get("errors")
    if isinstance(errors, (list, tuple)) and errors:
        detail = "; ".join(str(e) for e in errors)
    elif errors:
        detail = str(errors)
    else:
        detail = str(data.get("error") or "no detail given")
    # `status` is almost always the literal "error", which reads as noise
    # in front of the reason when a UI renders this string verbatim. Keep
    # it only when it carries information of its own.
    if status.lower() == "error":
        return detail
    return f"{status}: {detail}"


def _parse_next_charge(data: Any) -> int | None:
    """Extract the next-charge SoC from a get_next_charge response.

    The result is a number (target SoC %) or an object containing it; we parse
    defensively and return None when no active route / no value.
    """
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if isinstance(result, (int, float)):
        return int(result)
    if isinstance(result, dict):
        for key in ("next_charge", "soc", "target_soc"):
            value = result.get(key)
            if isinstance(value, (int, float)):
                return int(value)
    return None
