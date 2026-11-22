"""Exact conversions for the measurements stored in Postgres.

Elevation is numeric(12, 5) meters. Distance is numeric(14, 3) meters.
The international foot is 0.3048 meters, so a tenth of a foot is 0.03048 m
and stays exact at five decimal places. Canonical values stay Decimal.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

FOOT = Decimal("0.3048")
ELEVATION_QUANTUM = Decimal("0.00001")
DISTANCE_QUANTUM = Decimal("0.001")


def feet_to_meters(feet: Decimal) -> Decimal:
    return (Decimal(feet) * FOOT).quantize(ELEVATION_QUANTUM, rounding=ROUND_HALF_UP)


def kilometers_to_meters(kilometers: Decimal) -> Decimal:
    return (Decimal(kilometers) * Decimal("1000")).quantize(
        DISTANCE_QUANTUM, rounding=ROUND_HALF_UP
    )


def meters_to_feet(meters: Decimal) -> Decimal:
    return Decimal(meters) / FOOT


def meters_to_kilometers(meters: Decimal) -> Decimal:
    return Decimal(meters) / Decimal("1000")


def feet_number(meters: Decimal) -> int | float:
    """Imperial presentation value. Whole feet stay integers."""
    feet = meters_to_feet(meters)
    whole = feet.to_integral_value()
    if feet == whole:
        return int(whole)
    return float(feet)


def kilometers_number(meters: Decimal) -> int | float:
    kilometers = meters_to_kilometers(meters)
    whole = kilometers.to_integral_value()
    if kilometers == whole:
        return int(whole)
    return float(kilometers)
