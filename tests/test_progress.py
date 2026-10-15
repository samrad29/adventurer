from datetime import date, timedelta

import pytest

from adventurer.challenges import get_challenge
from adventurer.progress import (
    clean_entry,
    format_meters,
    opened_between,
    ordinal,
    view_model,
)


def test_everest_is_a_strict_climb_to_the_summit():
    everest = get_challenge("everest")
    heights = [stop["elevation_m"] for stop in everest["stops"]]
    assert heights == sorted(set(heights))
    assert heights[-1] == everest["summit_m"] == 8849
    assert everest["stops"][0]["slug"] == "lukla"


def test_camps_open_on_the_meter_and_not_before():
    everest = get_challenge("everest")
    below = view_model(everest, 2859)
    exact = view_model(everest, 2860)
    assert below["stops"][0]["unlocked"] is False
    assert below["current"] is None
    assert exact["stops"][0]["unlocked"] is True
    assert exact["stops"][1]["unlocked"] is False
    assert exact["current"]["slug"] == "lukla"
    assert exact["next"]["slug"] == "namche"
    assert view_model(everest, 8848)["summited"] is False
    assert view_model(everest, 8849)["summited"] is True
    assert view_model(everest, 9000)["percent"] == 100


def test_a_big_jump_opens_every_camp_it_passes():
    everest = get_challenge("everest")
    opened = [stop["slug"] for stop in opened_between(everest, 2000, 4000)]
    assert opened == ["lukla", "namche", "tengboche"]


def test_locked_model_hides_the_writing():
    everest = get_challenge("everest")
    model = view_model(everest, 0)
    assert model["stops"][1]["unlocks"] == []
    assert "Dal bhat" not in str(model)


def test_entry_rules():
    today = date(2026, 10, 15)
    cleaned = clean_entry("1,200", "", "2026-10-01", "  hill   repeats ", today)
    assert cleaned["elevation_m"] == 1200
    assert cleaned["distance_km"] is None
    assert cleaned["note"] == "hill repeats"

    with pytest.raises(ValueError, match="3,000"):
        clean_entry("3001", "", "", "", today)
    with pytest.raises(ValueError, match="future"):
        clean_entry("10", "", (today + timedelta(days=1)).isoformat(), "", today)
    with pytest.raises(ValueError, match="number"):
        clean_entry("lots", "", "", "", today)


def test_formatting():
    assert format_meters(2860) == "2,860 m"
    assert format_meters(12.5) == "12.5 m"
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(11) == "11th"
    assert ordinal(22) == "22nd"
