"""Pure statistics for historical-year ensemble routing results.

Completion is reported per year and pooled across years so the sample stays
descriptive: years are weighted equally, ``no_finish`` outcomes stay in the
denominator, and no interpolated tracks are ever synthesised.
"""

import math
import random
from dataclasses import dataclass
from typing import Mapping

COMPLETE_OUTCOMES = ("finished", "no_finish")
INDETERMINATE_OUTCOMES = ("failed", "cancelled", "blocked")

BOOTSTRAP_MIN_YEARS = 10


@dataclass(frozen=True)
class MemberResult:
    member_id: str
    year: int
    offset_days: int
    outcome: str
    duration_hours: float | None = None


def duration_hours(result: MemberResult) -> float | None:
    """Observed duration of a result; finished results must carry a duration."""
    value = result.duration_hours
    if value is None:
        if result.outcome == "finished":
            raise ValueError(f"{result.member_id}: finished results require duration hours.")
        return None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{result.member_id}: duration hours must be finite and non-negative.")
    return float(value)


def complete_years(
    results, offset_days: int
) -> tuple[list[int], dict[int, tuple[MemberResult, ...]]]:
    """Return sorted complete years and their members ordered by offset."""
    expected = 2 * offset_days + 1
    grouped: dict[int, list[MemberResult]] = {}
    for result in results:
        grouped.setdefault(result.year, []).append(result)
    years: list[int] = []
    mapping: dict[int, tuple[MemberResult, ...]] = {}
    for year in sorted(grouped):
        group = tuple(sorted(grouped[year], key=lambda item: item.offset_days))
        offsets = [item.offset_days for item in group]
        if offsets != list(range(-offset_days, offset_days + 1)) or len(group) != expected:
            continue
        if any(item.outcome not in COMPLETE_OUTCOMES for item in group):
            continue
        years.append(year)
        mapping[year] = group
    return years, mapping


def _finished_durations(group: tuple[MemberResult, ...]) -> list[float]:
    durations = []
    for result in group:
        if result.outcome != "finished":
            continue
        value = duration_hours(result)
        if value is not None:
            durations.append(value)
    return durations


def completion_curve(
    complete: Mapping[int, tuple[MemberResult, ...]],
    offset_days: int,
    horizon_hours: float,
    step_hours: float = 6.0,
) -> list[tuple[float, float]]:
    """Fraction of members in complete years finished by each time from 0 to the horizon.

    The final point is at ``horizon_hours`` and may be off the ``step_hours`` grid.
    """
    if not math.isfinite(horizon_hours):
        raise ValueError("horizon_hours must be finite.")
    if horizon_hours < 0:
        raise ValueError("horizon_hours must be non-negative.")
    if not math.isfinite(step_hours) or step_hours <= 0:
        raise ValueError("step_hours must be positive.")
    denominator = len(complete) * (2 * offset_days + 1)
    durations = [value for group in complete.values() for value in _finished_durations(group)]
    steps = int(horizon_hours // step_hours + 1e-9)
    times = [step_hours * index for index in range(steps + 1)]
    if not times or times[-1] < horizon_hours:
        times.append(float(horizon_hours))
    points: list[tuple[float, float]] = []
    for moment in times:
        if denominator == 0:
            points.append((moment, 0.0))
            continue
        finished = sum(1 for value in durations if value <= moment)
        points.append((moment, finished / denominator))
    return points


def target_completion(
    complete: Mapping[int, tuple[MemberResult, ...]], offset_days: int, target_hours: float
) -> float:
    """Mean per-year finished fraction at ``target_hours``; 0.0 without complete years."""
    if not complete:
        return 0.0
    expected = 2 * offset_days + 1
    fractions = []
    for group in complete.values():
        finished = sum(1 for value in _finished_durations(group) if value <= target_hours)
        fractions.append(finished / expected)
    return sum(fractions) / len(fractions)


def quantile(values, q: float) -> float | None:
    """Type-7 linear-interpolated quantile of ``values``; ``None`` when empty."""
    if not 0 <= q <= 1:
        raise ValueError("q must lie within [0, 1].")
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[int(position)]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def representative(
    complete: Mapping[int, tuple[MemberResult, ...]], offset_days: int, q: float
) -> MemberResult | None:
    """Observed finished member nearest the Type-7 quantile of finished durations."""
    finished = [
        result
        for group in complete.values()
        for result in group
        if result.outcome == "finished" and result.duration_hours is not None
    ]
    if not finished:
        return None
    target = quantile([duration_hours(result) for result in finished], q)
    if target is None:
        return None
    return min(
        finished,
        key=lambda result: (
            abs(duration_hours(result) - target),
            result.year,
            result.offset_days,
        ),
    )


def year_block_bootstrap(
    complete: Mapping[int, tuple[MemberResult, ...]],
    offset_days: int,
    target_hours: float,
    *,
    replicates: int = 2000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    """Percentile interval from resampling whole years with replacement.

    Returns ``None`` when there are fewer than ten complete years or when every
    year has the same completion fraction: an interval of zero width would be a
    false precision claim.
    """
    if replicates < 1:
        raise ValueError("replicates must be at least one.")
    if not 0 < alpha < 1:
        raise ValueError("alpha must lie strictly between 0 and 1.")
    years = sorted(complete)
    if len(years) < BOOTSTRAP_MIN_YEARS:
        return None
    expected = 2 * offset_days + 1
    fractions = []
    for year in years:
        finished = sum(1 for value in _finished_durations(complete[year]) if value <= target_hours)
        fractions.append(finished / expected)
    if max(fractions) == min(fractions):
        return None
    rng = random.Random(seed)
    samples = []
    for _ in range(replicates):
        total = 0.0
        for _ in range(len(fractions)):
            total += fractions[rng.randrange(len(fractions))]
        samples.append(total / len(fractions))
    samples.sort()
    lower = quantile(samples, alpha / 2)
    upper = quantile(samples, 1 - alpha / 2)
    if lower is None or upper is None:
        return None
    return lower, upper


def sample_guardrails(complete_years_count: int) -> str:
    """Describe what a sample of this many complete years may be used to claim."""
    if complete_years_count < 1:
        return "none"
    if complete_years_count <= 4:
        return "descriptive"
    if complete_years_count <= 9:
        return "target_only"
    if complete_years_count <= 19:
        return "interval"
    return "tails"


def completion_label(k: int, target_hours: float) -> str:
    """Human-readable summary label for a completion figure."""
    unit = "year" if k == 1 else "years"
    return f"Router completed within {target_hours:g} h across {k} complete {unit}"
