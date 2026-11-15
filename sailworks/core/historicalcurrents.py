"""Acquire and validate the shared target-date current snapshot.

The historical workbench routes every ensemble member against one current
snapshot taken at the target departure.  This module acquires that snapshot
from the marine manager, persists the resulting fields, and records what the
binding covers so a later reader can tell a real forecast from a held field.

Coverage is validated here rather than at sample time: sampling does not
extrapolate, so an acquisition that ends before the horizon must report the
hold explicitly instead of silently producing wind-only routes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sailworks.core.historicalbinding import CurrentBinding
from sailworks.core.marine.chs import DownloadCancelledError
from sailworks.core.marine.fields import Area, CurrentField

MAX_CURRENT_HORIZON_HOURS = 720
# Spans must touch or overlap; any positive gap is unsamplable.
MAX_CURRENT_GAP_SECONDS = 0.0


class CurrentDataError(ValueError):
    """A current request or acquisition could not be satisfied."""


class CurrentCoverageError(CurrentDataError):
    """Acquired currents do not continuously cover the requested window."""


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime):
        raise CurrentDataError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CurrentDataError(f"{name} must be an aware UTC datetime")


def _provider_name(field: CurrentField) -> str:
    return str(field.manifest().get("provider", ""))


@dataclass(frozen=True)
class CurrentRequest:
    """What the target-date snapshot must cover and how it is acquired."""

    target_departure_utc: datetime
    horizon_hours: int
    area: tuple[float, float, float, float]
    priority: str = "finest"
    provider: str = ""

    def __post_init__(self) -> None:
        _require_utc("target_departure_utc", self.target_departure_utc)
        if isinstance(self.horizon_hours, bool) or not isinstance(self.horizon_hours, int):
            raise CurrentDataError("horizon_hours must be an integer")
        if not 1 <= self.horizon_hours <= MAX_CURRENT_HORIZON_HOURS:
            raise CurrentDataError(
                f"horizon_hours must be between 1 and {MAX_CURRENT_HORIZON_HOURS}"
            )
        try:
            west, south, east, north = (float(value) for value in self.area)
        except (TypeError, ValueError):
            raise CurrentDataError("area must be four numbers (west, south, east, north)") from None
        if not (west < east and south < north):
            raise CurrentDataError("area must be ordered west < east and south < north")
        if not self.priority:
            raise CurrentDataError("priority must not be empty")
        if not isinstance(self.provider, str):
            raise CurrentDataError("provider must be a string")


@dataclass(frozen=True)
class CurrentAcquisition:
    """The persisted snapshot and the binding that points at it."""

    binding: CurrentBinding
    field_ids: tuple[str, ...]
    field_hashes: tuple[str, ...]
    coverage_end_utc: datetime
    hold_seconds: float
    provider: str


def validate_current_coverage(
    fields: Iterable[CurrentField], start_utc: datetime, horizon_hours: int
) -> tuple[datetime, float]:
    """Return ``(coverage_end_utc, hold_seconds)`` for the acquired fields.

    The union of the field spans must include ``start_utc``, and spans must
    touch or overlap: a positive gap is rejected because the sampler never
    extrapolates between fields.  Nothing is extrapolated past the last
    sample either; time beyond it is reported as a hold.
    """

    _require_utc("start_utc", start_utc)
    fields = tuple(fields)
    if not fields:
        raise CurrentCoverageError("no current fields were acquired")

    spans = []
    for field in fields:
        if len(field.times) == 0:
            raise CurrentCoverageError(f"current field {field.id} has no times")
        spans.append((float(field.times[0]), float(field.times[-1]), field.id))
    spans.sort()

    start = start_utc.timestamp()
    latest_end = max(end for _, end, _ in spans)
    if start < spans[0][0] or start > latest_end:
        raise CurrentCoverageError(
            f"current coverage does not include start {start_utc.isoformat()}"
        )

    cursor = spans[0][1]
    for span_start, span_end, field_id in spans[1:]:
        if span_start > cursor:
            gap = span_start - cursor
            if gap > MAX_CURRENT_GAP_SECONDS:
                gap_start = datetime.fromtimestamp(cursor, timezone.utc)
                gap_end = datetime.fromtimestamp(span_start, timezone.utc)
                raise CurrentCoverageError(
                    "current coverage has a "
                    f"{gap:.0f} s gap between {gap_start.isoformat()} and "
                    f"{gap_end.isoformat()} before field {field_id}"
                )
        cursor = max(cursor, span_end)

    coverage_end = datetime.fromtimestamp(cursor, timezone.utc)
    hold_seconds = max(0.0, horizon_hours * 3600.0 - (cursor - start))
    return coverage_end, hold_seconds


def acquire_target_currents(
    manager,
    request: CurrentRequest,
    *,
    cancel=None,
    progress=None,
) -> CurrentAcquisition:
    """Download, persist and validate the shared target-date snapshot.

    The coverage planner enforces its own download-window limit; any provider
    error is translated into ``CurrentDataError`` with the original message.
    Cancellation propagates unchanged so callers can stop the acquisition.
    """

    start = request.target_departure_utc
    end = start + timedelta(hours=request.horizon_hours)
    # The public request carries a plain tuple; the planner and providers take
    # a named Area.  Area's own bounds check is the validation, so its message
    # is preserved as a CurrentDataError.
    try:
        area = Area(*request.area)
    except ValueError as error:
        raise CurrentDataError(str(error)) from error
    try:
        downloaded = manager.coverage_planner.download(area, start, end, cancel, progress)
    except DownloadCancelledError:
        raise
    except Exception as error:
        raise CurrentDataError(str(error)) from error

    fields = tuple(downloaded)
    if not fields:
        raise CurrentDataError("current provider returned no fields")

    for field in fields:
        manager.fields.add(field)

    coverage_end, hold_seconds = validate_current_coverage(fields, start, request.horizon_hours)
    field_ids = tuple(field.id for field in fields)
    provider = request.provider or _provider_name(fields[0])
    binding = CurrentBinding(
        enabled=True,
        target_departure_utc=start,
        coverage_end_utc=coverage_end,
        priority=request.priority,
        field_ids=field_ids,
        # Field ids are content-addressed digests of the stored arrays and
        # metadata, so they are the artifact hashes as well.
        artifact_hashes=field_ids,
        provider=provider,
    )
    return CurrentAcquisition(
        binding=binding,
        field_ids=field_ids,
        field_hashes=field_ids,
        coverage_end_utc=coverage_end,
        hold_seconds=hold_seconds,
        provider=provider,
    )


def current_field_ids(manager, binding: CurrentBinding) -> tuple[str, ...]:
    """Return the binding's field ids, refusing ids the store no longer has."""

    store = getattr(manager.fields, "fields", manager.fields)
    present = set(store)
    field_ids = tuple(binding.field_ids)
    missing = tuple(field_id for field_id in field_ids if field_id not in present)
    if missing:
        raise CurrentDataError(
            "missing current fields: " + ", ".join(str(value) for value in missing)
        )
    return field_ids


def binding_from_selection(
    selection,
    *,
    target_departure_utc: datetime,
    horizon_hours: int,
) -> CurrentBinding:
    """Build a binding for a caller that already holds a ``FieldSelection``."""

    _require_utc("target_departure_utc", target_departure_utc)
    fields = tuple(getattr(selection, "fields", ()) or ())
    priority = str(getattr(selection, "priority", "finest"))
    if not fields:
        return CurrentBinding(
            enabled=False,
            target_departure_utc=None,
            coverage_end_utc=None,
            priority=priority,
        )

    coverage_end, _hold_seconds = validate_current_coverage(
        fields, target_departure_utc, horizon_hours
    )
    field_ids = tuple(field.id for field in fields)
    return CurrentBinding(
        enabled=True,
        target_departure_utc=target_departure_utc,
        coverage_end_utc=coverage_end,
        priority=priority,
        field_ids=field_ids,
        artifact_hashes=field_ids,
        provider=_provider_name(fields[0]),
    )
