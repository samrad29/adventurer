from datetime import date, timedelta

import pytest

from adventurer.challenges import TRAIL_NAMES, display_name, get_challenge
from adventurer.progress import (
    clean_entry,
    format_feet,
    opened_between,
    ordinal,
    view_model,
)


def test_everest_is_a_strict_climb_to_the_summit():
    everest = get_challenge("everest")
    heights = [stop["elevation_ft"] for stop in everest["stops"]]
    slugs = [stop["slug"] for stop in everest["stops"]]
    assert heights == sorted(set(heights))
    assert heights[-1] == everest["summit_ft"] == 29032
    assert slugs[:4] == ["timms-hill", "four-thousand", "seven-thousand", "lukla"]
    assert heights[0] == 1951.5


def test_camps_open_on_the_foot_and_not_before():
    everest = get_challenge("everest")
    assert view_model(everest, 1951.4)["current"] is None
    hill = view_model(everest, 1951.5)
    assert hill["current"]["slug"] == "timms-hill"
    assert hill["next"]["slug"] == "four-thousand"
    assert view_model(everest, 4000)["current"]["slug"] == "four-thousand"
    below = view_model(everest, 9382)
    exact = view_model(everest, 9383)
    assert below["current"]["slug"] == "seven-thousand"
    assert below["current"]["name"] == "7,000 feet"
    assert exact["current"]["slug"] == "lukla"
    assert exact["next"]["slug"] == "namche"
    assert view_model(everest, 29031)["summited"] is False
    assert view_model(everest, 29032)["summited"] is True
    assert view_model(everest, 30000)["percent"] == 100


def test_the_seven_thousand_foot_trail_keeps_its_name():
    everest = get_challenge("everest")
    stop = everest["stops"][2]
    assert display_name(stop, None, unlocked=True) == "7,000 feet"
    assert display_name(stop, 4, unlocked=False) == "7,000 feet"
    chosen = display_name(stop, 4, unlocked=True)
    assert chosen in TRAIL_NAMES
    assert chosen == display_name(stop, 4, unlocked=True)
    opened = view_model(everest, 7000, adventurer_id=4)
    assert opened["current"]["name"] == chosen
    assert len({display_name(stop, account, unlocked=True) for account in range(1, 40)}) > 1


def test_a_big_jump_opens_every_camp_it_passes():
    everest = get_challenge("everest")
    opened = [stop["slug"] for stop in opened_between(everest, 7000, 13000)]
    assert opened == ["lukla", "namche", "tengboche"]


def test_locked_model_hides_the_writing():
    everest = get_challenge("everest")
    model = view_model(everest, 0)
    assert model["stops"][0]["unlocks"] == []
    assert "Price County" not in str(model)
    assert "Dal bhat" not in str(model)


def test_entry_rules():
    today = date(2026, 10, 15)
    cleaned = clean_entry("1,200", "", "2026-10-01", "  hill   repeats ", today)
    assert cleaned["elevation_ft"] == 1200
    assert cleaned["distance_km"] is None
    assert cleaned["note"] == "hill repeats"

    with pytest.raises(ValueError, match="10,000"):
        clean_entry("10001", "", "", "", today)
    with pytest.raises(ValueError, match="future"):
        clean_entry("10", "", (today + timedelta(days=1)).isoformat(), "", today)
    with pytest.raises(ValueError, match="number"):
        clean_entry("lots", "", "", "", today)


def test_formatting():
    assert format_feet(9383) == "9,383 ft"
    assert format_feet(12.5) == "12.5 ft"
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(11) == "11th"
    assert ordinal(22) == "22nd"


def test_every_stop_has_a_credited_photo():
    from pathlib import Path

    from adventurer.photos import photo_credits

    everest = get_challenge("everest")
    credits = photo_credits()
    folder = Path(__file__).resolve().parents[1] / "adventurer" / "static" / "photos"
    for stop in everest["stops"]:
        if stop["place"] in {"high-point", "checkpoint", "trail"}:
            continue
        assert (folder / f"{stop['slug']}.jpg").is_file()
        credit = credits[stop["slug"]]
        assert credit["author"]
        assert credit["alt"]
        assert credit["license_url"].startswith("https://creativecommons.org/")
        assert credit["source_url"].startswith("https://commons.wikimedia.org/")
