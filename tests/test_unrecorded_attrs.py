"""The big list attributes must be excluded from the recorder.

With a large recent window the trips/journeys/charges JSON blob exceeds the
recorder's 16 KB per-state attribute limit; declaring them unrecorded keeps the
entity state in history without the recorder warning / dropped attributes.
"""
from __future__ import annotations

from custom_components.ev_trip_logger.sensor import (
    RecentChargesSensor,
    RecentJourneysSensor,
    RecentTripsSensor,
)


def test_recent_list_attributes_are_unrecorded() -> None:
    assert "trips" in RecentTripsSensor._unrecorded_attributes
    assert "journeys" in RecentJourneysSensor._unrecorded_attributes
    assert "charges" in RecentChargesSensor._unrecorded_attributes
