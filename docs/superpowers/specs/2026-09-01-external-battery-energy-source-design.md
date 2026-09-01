# External battery-energy source

**Status:** design approved 2026-09-01, implementation deferred until real MQTT
data exists.

## Problem

Every energy figure the integration produces is currently derived, and each
derivation has a known failure mode:

- **Charge energy** comes from integrating a power sensor. Sampling holes lose
  energy outright — `_MAX_POWER_TRAPEZOID_DT_H` discards any segment longer than
  20 minutes rather than interpolating it. Charge id74 came out 32 % short.
- **Trip energy** is quantised by SoC. One percent is capacity/100, about
  0.825 kWh or 4 km. This is the origin of the impossible efficiency records and
  of the noise in the capacity samples.
- **Pack capacity** is calibrated from kWh over ΔSoC, measured at the charge
  inlet. That reads high — roughly 4 % on sessions that end near full — and the
  calibration has no independent anchor, which is how it drifted to 85.16 kWh
  and published a SoH of 103 % to ABRP.

A vehicle that reports the usable energy remaining in its pack removes all
three. The delta between two readings is the energy, with no integration to go
wrong, no quantisation, and no inlet bias.

## Source

The immediate source is the byd-trip-stats Android app, which publishes over
MQTT. On DiLink 5 it feeds `battery_remain_power_ev` (usable kWh, decimal, from
the BMS) and `statistic_soh`.

The integration must not depend on that app. The feature is a new optional
entity input, in the same shape as every existing one:

```
CONF_BATTERY_ENERGY_SENSOR = "battery_energy_sensor"   # kWh remaining in pack
```

Any sensor exposing usable kWh satisfies it. byd-trip-stats is one producer.

## Threat model

The publisher is not trusted. Three distinct reasons, which need the same
defence:

1. The MQTT credential is recoverable. byd-trip-stats stores it with
   `getSharedPreferences(..., MODE_PRIVATE)` and no `EncryptedSharedPreferences`
   — protected by the Android uid sandbox, not by encryption.
2. Any other client on the broker can publish to the same topic.
3. A sensor that is merely broken produces the same shapes as a hostile one, so
   the guards earn their keep even with no attacker.

What makes this worth defending is the blast radius: these readings feed cost
accounting and capacity calibration, both of which persist. Undoing a capacity
drift already cost fifteen releases.

## Ingest guards

Applied before any value reaches the database:

| Guard | Rule |
|---|---|
| Range | kWh within `0 .. declared_capacity × 1.2`; SoH `50..110`; power `0..250 kW` |
| Freshness | reject readings older than the staleness window, and any timestamp in the future |
| Physical slope | between samples, kWh cannot move by more than `250 kW × dt` |
| Cross-check | `usable_kwh / capacity` must agree with `soc/100` within a band |
| Locks | never overwrite a row with `energy_locked` or `cost_locked` set |

The cross-check is the load-bearing one: two independent fields have to agree,
so falsifying a single field does not get through.

Bounds are deliberately in the same spirit as the ones the producing app already
applies to itself (`usableKwh in 0.0..200.0`, `sohPct in 50.0..110.0`).

## Broker hardening

Out of scope for the integration, but part of the same design. The vehicle gets
a dedicated Mosquitto account restricted to a single topic:

```
user byd_car
topic write byd-trip-stats/#
```

No subscribe, and no access to `homeassistant/#`. Without the second
restriction, a leaked credential could create entities via discovery, delete
them by publishing empty retained payloads, or publish to the command topics of
any MQTT device in the installation.

Because the app has no switch to disable discovery, the entities are created
once under a permissive ACL — discovery messages are retained, so they survive —
and the ACL is then tightened. The app tolerates the subsequent denials: it
catches the failure and carries on publishing state.

Transport is Tailscale, so the broker is not reachable from the internet at all.
The Mosquitto ACL is the second layer, for when the first one fails.

## Known limitation

The head unit drops its network a few minutes after the vehicle switches off.
Parked telemetry stays as blind as it is today. No broker or transport choice
changes this.

## Implementation gate

Not to be built against an imagined payload. Required first:

1. MQTT configured on the vehicle, entities present in Home Assistant.
2. Several days of captured data confirming the head unit stays connected
   through trips and charges.
3. `battery_remain_power_ev` reconciled against the pack figure already
   established independently (79.8–83.4 kWh, central 81.6).

Separately blocking: the options flow renders optional fields as `None` and
blanks them on submit, so adding a config key needs that fixed first or the user
loses existing settings.
