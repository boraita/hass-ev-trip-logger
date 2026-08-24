"""Seeded per-model battery baselines.

Split out of `coordinator.py` in v0.8.31. `config_flow` needs `cohort_baseline_options()` to
populate a dropdown, and used to reach for it through a lazy in-function
import meant to keep `coordinator` out of memory during entry restore.
That never worked: `__init__.py` imports `coordinator` at module level,
so it was always loaded before any config flow ran. This module is a leaf
— one JSON read and two functions — so the import can just be an import,
this time for a reason that holds.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final


def _load_cohort_baselines() -> dict[str, dict[str, Any]]:
    """v0.6.3 — read the seeded `cohort_baselines.json` once at import.

    Returns the {model_key: {label, chemistry, nameplate_kwh,
    cohort_new_kwh, source}} mapping. The "_meta" key is filtered out
    so callers can iterate values as model rows. Any read/parse error
    degrades gracefully to an empty dict — the SoH model then keeps
    its v0.5.x nameplate behaviour, no warnings spamming the log.
    """
    path = Path(__file__).with_name("cohort_baselines.json")
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # pragma: no cover — defensive
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}


COHORT_BASELINES: Final = _load_cohort_baselines()


def cohort_baseline_options() -> list[tuple[str, str]]:
    """[(model_key, human_label), …] — used by the config flow to
    populate the optional `CONF_VEHICLE_MODEL` dropdown."""
    rows = [
        (k, str(v.get("label") or k))
        for k, v in COHORT_BASELINES.items()
    ]
    rows.sort(key=lambda kv: kv[1])
    return rows
