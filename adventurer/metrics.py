"""Canonical meters for elevation and distance.

Storage is meters at numeric(16, 6) for both metrics. Elevation is entered in
international feet and distance in statute miles. Conversion uses Decimal.
A null input is not supplied. Zero is a known measurement. Display quantities
are for presentation and are not the completion comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

FOOT_IN_METERS = Decimal("0.3048")
MILE_IN_METERS = Decimal("1609.344")
STORAGE_QUANTUM = Decimal("0.000001")
_PERCENT = Decimal("100")
_PERCENT_STEP = Decimal("0.01")
_ALMOST_COMPLETE = Decimal("99.99")


class MetricError(ValueError):
    pass


@dataclass(frozen=True)
class _Metric:
    code: str
    storage_unit: str
    display_unit: str
    meters_per_display: Decimal
    display_step: Decimal
    label: str


_METRICS = {
    "elevation": _Metric(
        "elevation",
        "m",
        "ft",
        FOOT_IN_METERS,
        Decimal("0.01"),
        "Elevation",
    ),
    "distance": _Metric(
        "distance",
        "m",
        "mi",
        MILE_IN_METERS,
        Decimal("0.001"),
        "Distance",
    ),
}


@dataclass(frozen=True)
class MetricProgress:
    code: str
    storage_unit: str
    display_unit: str
    target_m: Decimal
    credited_m: Decimal
    remaining_m: Decimal
    complete: bool
    percent: Decimal
    target_display: Decimal
    credited_display: Decimal
    remaining_display: Decimal


def parse_measurement(metric: str, raw: object) -> Decimal | None:
    """Return canonical meters, or None when the measurement was not supplied.

    Zero returns Decimal('0'). A negative value, a binary float, and a value
    finer than the metric's input step are rejected. The accepted steps
    convert exactly onto numeric(16, 6).
    """
    spec = _spec(metric)
    if raw is None:
        return None
    if isinstance(raw, bool) or isinstance(raw, float):
        raise MetricError(f"{spec.label} should be a decimal number.")
    if isinstance(raw, str):
        text = raw.strip().replace(",", "")
        if not text:
            return None
    elif isinstance(raw, int):
        text = str(raw)
    elif isinstance(raw, Decimal):
        text = format(raw, "f")
    else:
        raise MetricError(f"{spec.label} should be a decimal number.")
    try:
        display = Decimal(text)
    except Exception as exc:
        raise MetricError(f"{spec.label} should be a decimal number.") from exc
    if not display.is_finite():
        raise MetricError(f"{spec.label} should be a decimal number.")
    if display < 0:
        raise MetricError(f"{spec.label} cannot be negative.")
    if display != display.quantize(spec.display_step):
        raise MetricError(
            f"{spec.label} is accepted to {spec.display_step} {spec.display_unit}."
        )
    meters = display * spec.meters_per_display
    stored = meters.quantize(STORAGE_QUANTUM)
    if stored != meters:
        raise MetricError(f"{spec.label} does not fit the stored meter scale.")
    return stored


def assess_metric(code: str, target_m: Decimal, credited_m: Decimal) -> MetricProgress:
    """One required metric. Completion uses stored meters, capped at the target."""
    spec = _spec(code)
    target = _decimal(target_m, f"{spec.label} target")
    credited = _decimal(credited_m, f"{spec.label} credit")
    if target <= 0:
        raise MetricError("A route target must be greater than zero.")
    if credited < 0:
        raise MetricError("A credited amount cannot be negative.")
    complete = credited >= target
    shown = target if credited > target else credited
    remaining = target - shown
    target_display = _display(spec, target)
    if target_display <= 0:
        target_display = spec.display_step
    if complete:
        credited_display = target_display
        remaining_display = Decimal("0")
        percent = _PERCENT
    else:
        credited_display = _display(spec, shown)
        remaining_display = _display(spec, remaining)
        if remaining_display <= 0:
            remaining_display = spec.display_step
        if credited_display >= target_display:
            credited_display = target_display - spec.display_step
        if credited_display < 0:
            credited_display = Decimal("0")
        percent = (shown / target * _PERCENT).quantize(_PERCENT_STEP, rounding=ROUND_HALF_UP)
        if percent >= _PERCENT:
            percent = _ALMOST_COMPLETE
    return MetricProgress(
        code=spec.code,
        storage_unit=spec.storage_unit,
        display_unit=spec.display_unit,
        target_m=target,
        credited_m=shown,
        remaining_m=remaining,
        complete=complete,
        percent=percent,
        target_display=target_display,
        credited_display=credited_display,
        remaining_display=remaining_display,
    )


def _spec(metric: str) -> _Metric:
    spec = _METRICS.get(metric)
    if spec is None:
        raise MetricError("That progress metric does not exist.")
    return spec


def _display(spec: _Metric, meters: Decimal) -> Decimal:
    return (meters / spec.meters_per_display).quantize(spec.display_step, rounding=ROUND_HALF_UP)


def _decimal(value: Decimal, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, Decimal):
        raise MetricError(f"{label} must be a Decimal number of meters.")
    if not value.is_finite():
        raise MetricError(f"{label} must be a Decimal number of meters.")
    return value
