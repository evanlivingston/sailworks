"""Assemble a bounded ERA5 window into one published, non-active tranche.

Windows are **half-open**: ``[start_utc, end_utc)``.  A four-hour window
starting at 00:00 covers 00, 01, 02 and 03 UTC and never 04.  Catalog chunks
carry inclusive bounds, so a chunk is only usable when its last hour is
strictly before ``end_utc``.

The window is validated before any byte is concatenated: every hour must be
covered exactly once, every chunk must share one area and declare the
variables the requested fields need.  The assembly itself is delegated to
:func:`sailworks.core.era5.build_history_display`, which produces exactly one
artifact and one entry; this module only publishes the result.  It never
activates it, so an analysis selection already in use is left untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import era5
from .forecasttranche import AnalysisSelection, ForecastTranche
from .historicalweather import era5_area
from .tranchestore import ResolvedForecast

MAX_WINDOW_HOURS = 384
MIN_WINDOW_HOURS = 2
WIND_VARIABLES = era5.FIELD_VARIABLES["wind"]
AREA_TOLERANCE_DEG = 1e-6

_CATALOG_NAME = "era5-history.json"
_HOUR = timedelta(hours=1)


class HistoricalTrancheError(ValueError):
    """A bounded ERA5 window cannot be assembled into a tranche."""


@dataclass
class _StoreCarrier:
    """Minimal ``state`` shim for :func:`era5.build_history_display`."""

    store: object


def read_era5_catalog(root: Path) -> list[dict]:
    """Load the ERA5 history catalog, or ``[]`` when it does not exist yet."""
    path = Path(root) / _CATALOG_NAME
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        raise HistoricalTrancheError(f"cannot read ERA5 catalog {path}: {exc}") from exc
    if not isinstance(entries, list):
        raise HistoricalTrancheError(f"ERA5 catalog {path} is not a list of entries")
    return [entry for entry in entries if isinstance(entry, dict)]


def hourly_window(start_utc: datetime, end_utc: datetime) -> tuple[datetime, ...]:
    """Return the half-open hourly stamps ``start .. end`` exclusive of ``end``."""
    start = _as_utc(start_utc, "window start")
    end = _as_utc(end_utc, "window end")
    stamps = []
    cursor = start
    while cursor < end:
        stamps.append(cursor)
        cursor += _HOUR
    return tuple(stamps)


def select_window_entries(
    catalog,
    start_utc: datetime,
    end_utc: datetime,
    area,
    *,
    fields=("wind",),
) -> list[dict]:
    """Pick the catalog chunks that exactly cover ``[start_utc, end_utc)``.

    ``area`` is in ERA5's ``(north, west, south, east)`` order, matching the
    catalog's stored entry areas; callers holding our ``(west, south, east,
    north)`` convention convert first with
    :func:`sailworks.core.historicalweather.era5_area`.

    Raises :class:`HistoricalTrancheError` when the window is shorter than
    ``MIN_WINDOW_HOURS`` or longer than ``MAX_WINDOW_HOURS``, when the bounds
    are not whole hours in UTC, when an hour is missing or covered twice, when
    the window spans more than one area, when no chunk matches ``area`` and
    when a chunk cannot supply the variables ``fields`` require.
    """
    start = _as_utc(start_utc, "window start")
    end = _as_utc(end_utc, "window end")
    expected = hourly_window(start, end)
    if len(expected) < MIN_WINDOW_HOURS:
        raise HistoricalTrancheError(f"window must cover at least {MIN_WINDOW_HOURS} hours")
    if len(expected) > MAX_WINDOW_HOURS:
        raise HistoricalTrancheError(f"window must cover at most {MAX_WINDOW_HOURS} hours")
    requested_area = _validate_area(area, "requested area")
    required = _required_variables(fields)

    contained = []
    for entry in catalog or ():
        if not (entry.get("artifact") and entry.get("start") and entry.get("end")):
            continue
        span = _entry_span(entry)
        if span[0] < start or span[1] >= end:
            continue
        contained.append((span, entry))
    if not contained:
        raise HistoricalTrancheError(
            f"no ERA5 catalog entries fall inside {expected[0].isoformat()} .. "
            f"{expected[-1].isoformat()}"
        )
    contained.sort(key=lambda item: item[0][0])

    distinct_areas: list[tuple[float, ...]] = []
    for _, entry in contained:
        entry_area = _validate_area(entry.get("area"), "catalog entry area")
        if not any(_areas_match(entry_area, known) for known in distinct_areas):
            distinct_areas.append(entry_area)
    if len(distinct_areas) > 1:
        raise HistoricalTrancheError("ERA5 window spans more than one area")

    matching = [
        (span, entry)
        for span, entry in contained
        if _areas_match(_validate_area(entry.get("area"), "catalog entry area"), requested_area)
    ]
    if not matching:
        raise HistoricalTrancheError(f"no ERA5 catalog entries match area {requested_area}")

    covered: dict[datetime, dict] = {}
    selected = []
    for span, entry in matching:
        variables = tuple(entry.get("variables") or era5.VARIABLES)
        missing = [name for name in required if name not in variables]
        if missing:
            raise HistoricalTrancheError(
                f"entry for {span[0].isoformat()} is missing variables: " f"{', '.join(missing)}"
            )
        cursor = span[0]
        while cursor <= span[1]:
            if cursor in covered:
                raise HistoricalTrancheError(
                    f"window hour {cursor.isoformat()} is covered by more than one entry"
                )
            covered[cursor] = entry
            cursor += _HOUR
        selected.append(
            {
                "artifact": str(entry["artifact"]),
                "start": span[0].isoformat(),
                "end": span[1].isoformat(),
                "area": list(_validate_area(entry.get("area"), "catalog entry area")),
                "variables": list(variables),
            }
        )

    for hour in expected:
        if hour not in covered:
            raise HistoricalTrancheError(
                f"window hour {hour.isoformat()} is not covered by any entry"
            )
    return selected


def build_wind_tranche(
    store,
    start_utc: datetime,
    end_utc: datetime,
    area,
    *,
    model_id="ERA5",
    catalog=None,
) -> ForecastTranche:
    """Build and publish one wind tranche for a half-open window.

    ``area`` uses our ``(west, south, east, north)`` convention and is
    converted with :func:`sailworks.core.historicalweather.era5_area` before
    the catalog is searched, because catalog entry areas are stored in ERA5's
    ``(north, west, south, east)`` order.

    The tranche is published but never activated.  A repeated call for the
    same window reuses the already published manifest, because the tranche id
    is derived from the concatenated bytes.
    """
    entries = read_era5_catalog(store.root) if catalog is None else list(catalog)
    try:
        search_area = era5_area(area)
    except ValueError as exc:
        raise HistoricalTrancheError(str(exc)) from exc
    selected = select_window_entries(entries, start_utc, end_utc, search_area)
    tranche = era5.build_history_display(_StoreCarrier(store), selected, model_id=model_id)
    try:
        store.publish(tranche)
    except FileExistsError:
        # Same bytes, same tranche id; only assembly timestamps differ.
        return store.load(tranche.tranche_id)
    return tranche


def wind_tranche_for_window(
    store,
    start_utc: datetime,
    end_utc: datetime,
    area,
    *,
    model_id="ERA5",
    catalog=None,
) -> ResolvedForecast:
    """Publish a window tranche and resolve it without changing the active one.

    ``area`` is our ``(west, south, east, north)`` region; see
    :func:`build_wind_tranche`.
    """
    tranche = build_wind_tranche(
        store, start_utc, end_utc, area, model_id=model_id, catalog=catalog
    )
    entry = tranche.entries[0]
    return store.resolve(AnalysisSelection(tranche.tranche_id, entry.entry_id))


def _as_utc(value, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise HistoricalTrancheError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise HistoricalTrancheError(f"{label} must be timezone-aware UTC")
    if value.minute or value.second or value.microsecond:
        raise HistoricalTrancheError(f"{label} must be a whole hour")
    return value.astimezone(timezone.utc)


def _parse_stamp(value, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise HistoricalTrancheError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalTrancheError(f"{label} is not an ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _as_utc(parsed, label)


def _entry_span(entry) -> tuple[datetime, datetime]:
    start = _parse_stamp(entry.get("start"), "entry start")
    end = _parse_stamp(entry.get("end"), "entry end")
    if end < start:
        raise HistoricalTrancheError(f"entry end precedes its start: {start.isoformat()}")
    return start, end


def _validate_area(area, label: str) -> tuple[float, ...]:
    try:
        values = tuple(float(value) for value in area)
    except (TypeError, ValueError) as exc:
        raise HistoricalTrancheError(f"{label} must be four numbers") from exc
    if len(values) != 4:
        raise HistoricalTrancheError(f"{label} must be four numbers")
    return values


def _areas_match(left, right) -> bool:
    return all(abs(a - b) <= AREA_TOLERANCE_DEG for a, b in zip(left, right))


def _required_variables(fields) -> tuple[str, ...]:
    names = []
    for field in fields:
        if field not in era5.FIELD_VARIABLES:
            raise HistoricalTrancheError(f"unsupported ERA5 field: {field}")
        names.extend(era5.FIELD_VARIABLES[field])
    if not names:
        raise HistoricalTrancheError("at least one field is required")
    return tuple(dict.fromkeys(names))
