"""Elevation math for a challenge. No database and no Flask."""

from __future__ import annotations

from datetime import date, datetime

from .challenges import display_name

MAX_ENTRY_FT = 10000
MAX_DISTANCE_KM = 120
MAX_NOTE = 280


class EntryError(ValueError):
    pass


def view_model(challenge: dict, total_ft: float, adventurer_id: int | None = None) -> dict:
    total = max(0.0, float(total_ft))
    current = None
    nxt = None
    stops = []
    for stop in challenge["stops"]:
        unlocked = total >= stop["elevation_ft"]
        item = {
            "slug": stop["slug"],
            "name": display_name(stop, adventurer_id, unlocked=unlocked),
            "elevation_ft": stop["elevation_ft"],
            "place": stop["place"],
            "summary": stop["summary"],
            "unlocked": unlocked,
            "unlocks": stop["unlocks"] if unlocked else [],
        }
        if unlocked:
            current = item
        elif nxt is None:
            nxt = item
        stops.append(item)
    summit = challenge["summit_ft"]
    return {
        "slug": challenge["slug"],
        "name": challenge["name"],
        "local_names": list(challenge["local_names"]),
        "region": challenge["region"],
        "route": challenge["route"],
        "summit_ft": summit,
        "summary": challenge["summary"],
        "total_ft": total,
        "percent": min(100.0, (total / summit) * 100 if summit else 0),
        "summited": total >= summit,
        "remaining_ft": max(0.0, summit - total),
        "current": current,
        "next": nxt,
        "to_next_ft": None if nxt is None else nxt["elevation_ft"] - total,
        "stops": stops,
    }


def opened_between(challenge: dict, before_ft: float, after_ft: float) -> list[dict]:
    opened = []
    for stop in challenge["stops"]:
        if before_ft < stop["elevation_ft"] <= after_ft:
            opened.append(stop)
    return opened


def clean_entry(
    elevation_raw: str,
    distance_raw: str,
    happened_raw: str,
    note_raw: str,
    today: date,
) -> dict:
    elevation = _parse_number(elevation_raw, "Elevation")
    if elevation <= 0:
        raise EntryError("Elevation has to be more than zero.")
    if elevation > MAX_ENTRY_FT:
        raise EntryError(
            f"Log one outing at a time, up to {MAX_ENTRY_FT:,} ft. Split a bigger day."
        )

    distance = None
    if distance_raw and distance_raw.strip():
        distance = _parse_number(distance_raw, "Distance")
        if distance <= 0:
            raise EntryError("Distance has to be more than zero, or leave it blank.")
        if distance > MAX_DISTANCE_KM:
            raise EntryError(f"Distance looks too long for one outing (max {MAX_DISTANCE_KM} km).")

    happened_text = (happened_raw or "").strip() or today.isoformat()
    try:
        happened = date.fromisoformat(happened_text)
    except ValueError as exc:
        raise EntryError("Use a real date.") from exc
    if happened > today:
        raise EntryError("That date is still in the future.")
    if happened.year < 2000:
        raise EntryError("Start at the year 2000 or later.")

    note = " ".join((note_raw or "").split())
    if len(note) > MAX_NOTE:
        raise EntryError(f"Keep the note under {MAX_NOTE} characters.")

    return {
        "elevation_ft": elevation,
        "distance_km": distance,
        "happened_on": happened.isoformat(),
        "note": note,
    }


def format_feet(value: float) -> str:
    number = float(value)
    if abs(number - round(number)) < 0.05:
        return f"{round(number):,} ft"
    return f"{number:,.1f} ft"


def format_km(value: float | None) -> str:
    if value is None:
        return ""
    number = float(value)
    if abs(number - round(number)) < 0.05:
        return f"{round(number)} km"
    return f"{number:.1f} km"


def ordinal(number: int) -> str:
    value = int(number)
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def format_day(value: str) -> str:
    if not value:
        return ""
    parsed = datetime.strptime(value[:10], "%Y-%m-%d").date()
    return f"{parsed.day} {parsed.strftime('%b %Y')}"


def join_names(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _parse_number(raw: str, label: str) -> float:
    text = (raw or "").strip().replace(",", "")
    if not text:
        raise EntryError(f"Add {label.lower()}.")
    try:
        value = float(text)
    except ValueError as exc:
        raise EntryError(f"{label} should be a number.") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise EntryError(f"{label} should be a number.")
    return value
