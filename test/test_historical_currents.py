"""Tests for target-date current acquisition and coverage validation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from sailworks.core.historicalcurrents import (
    CurrentCoverageError,
    CurrentDataError,
    CurrentRequest,
    acquire_target_currents,
    binding_from_selection,
    current_field_ids,
    validate_current_coverage,
)
from sailworks.core.marine.chs import DownloadCancelledError
from sailworks.core.marine.fields import CurrentField, FieldSelection
from sailworks.core.marine.models import canonical_json

T = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def current_field(start=T, offsets=(0, 1), provider="test"):
    """Minimal synthetic field, mirroring the current-routing test builder."""
    metadata = dict(
        schema_version=1,
        provider=provider,
        model="Analytical field",
        composition="total",
        vector_reference="true_east_north",
        sources=[],
        resolution_m=9000,
        run=start.isoformat(),
    )
    times = np.array([(start + timedelta(hours=offset)).timestamp() for offset in offsets])
    east = np.zeros((len(offsets), 2, 2))
    north = np.zeros((len(offsets), 2, 2))
    return CurrentField(
        canonical_json(metadata),
        times,
        np.array([-1.0, 1.0]),
        np.array([-1.0, 1.0]),
        east,
        north,
    )


class FakeFields:
    def __init__(self):
        self.fields = {}
        self.added = []

    def add(self, field):
        self.fields[field.id] = field
        self.added.append(field)
        return field.id


class FakePlanner:
    def __init__(self, fields):
        self.fields = fields
        self.calls = []

    def download(self, area, start, end, cancel, report):
        self.calls.append((area, start, end))
        return list(self.fields)


class FakeManager:
    def __init__(self, fields):
        self.fields = FakeFields()
        self.coverage_planner = FakePlanner(fields)


def request(horizon=2, area=(-1.0, -1.0, 1.0, 1.0)):
    return CurrentRequest(
        target_departure_utc=T,
        horizon_hours=horizon,
        area=area,
    )


def test_happy_path_persists_fields_and_binding():
    fields = [
        current_field(offsets=(0, 1)),
        current_field(offsets=(0, 2)),
        current_field(offsets=(0, 3)),
    ]
    manager = FakeManager(fields)

    acquisition = acquire_target_currents(manager, request(horizon=2))

    binding = acquisition.binding
    assert binding.enabled is True
    assert binding.target_departure_utc == T
    assert binding.coverage_end_utc == T + timedelta(hours=3)
    assert binding.field_ids == tuple(field.id for field in fields)
    assert binding.artifact_hashes == binding.field_ids
    assert binding.provider == "test"
    assert acquisition.hold_seconds == 0
    assert acquisition.provider == "test"
    assert len(manager.fields.added) == 3
    assert set(manager.fields.fields) == set(binding.field_ids)
    assert manager.coverage_planner.calls == [((-1.0, -1.0, 1.0, 1.0), T, T + timedelta(hours=2))]


def test_horizon_beyond_coverage_reports_hold_seconds():
    fields = [current_field(offsets=(0, 1)), current_field(offsets=(0, 2))]
    manager = FakeManager(fields)

    acquisition = acquire_target_currents(manager, request(horizon=10))

    assert acquisition.coverage_end_utc == T + timedelta(hours=2)
    assert acquisition.hold_seconds == 8 * 3600
    assert acquisition.binding.coverage_end_utc == T + timedelta(hours=2)


def test_start_uncovered_is_explicit():
    fields = [current_field(offsets=(1, 3))]
    manager = FakeManager(fields)

    with pytest.raises(CurrentCoverageError, match="does not include start") as raised:
        acquire_target_currents(manager, request(horizon=2))

    assert T.isoformat() in str(raised.value)


def test_interior_gap_is_explicit():
    fields = [current_field(offsets=(0, 1)), current_field(offsets=(3, 4))]

    with pytest.raises(CurrentCoverageError, match="gap") as raised:
        validate_current_coverage(fields, T, 2)

    message = str(raised.value)
    assert "7200 s gap" in message
    assert (T + timedelta(hours=1)).isoformat() in message


def test_touching_spans_are_accepted():
    fields = [current_field(offsets=(0, 1)), current_field(offsets=(1, 2))]

    coverage_end, hold = validate_current_coverage(fields, T, 2)

    assert coverage_end == T + timedelta(hours=2)
    assert hold == 0


def test_one_second_gap_is_rejected():
    fields = [current_field(offsets=(0, 1)), current_field(offsets=(1 + 1 / 3600, 2))]

    with pytest.raises(CurrentCoverageError, match="1 s gap"):
        validate_current_coverage(fields, T, 2)


def test_one_hour_gap_is_rejected_because_sampler_does_not_interpolate():
    fields = [current_field(offsets=(0, 1)), current_field(offsets=(2, 3))]

    with pytest.raises(CurrentCoverageError, match="3600 s gap"):
        validate_current_coverage(fields, T, 2)


def test_coverage_longer_than_horizon_never_yields_negative_hold():
    fields = [current_field(offsets=(0, 5))]

    coverage_end, hold = validate_current_coverage(fields, T, 2)

    assert coverage_end == T + timedelta(hours=5)
    assert hold == 0.0
    assert hold >= 0.0


def test_empty_field_list_and_empty_times_are_explicit():
    manager = FakeManager([])
    with pytest.raises(CurrentDataError, match="returned no fields"):
        acquire_target_currents(manager, request())

    with pytest.raises(CurrentCoverageError, match="no current fields"):
        validate_current_coverage([], T, 2)

    empty = SimpleNamespace(times=np.array([]), id="empty-field")
    with pytest.raises(CurrentCoverageError, match="empty-field"):
        validate_current_coverage([empty], T, 2)


def test_binding_from_selection_without_fields_is_disabled():
    binding = binding_from_selection(
        FieldSelection(()),
        target_departure_utc=T,
        horizon_hours=2,
    )

    assert binding.enabled is False
    assert binding.target_departure_utc is None
    assert binding.coverage_end_utc is None
    assert binding.field_ids == ()


def test_binding_from_selection_with_fields_validates_coverage():
    fields = [current_field(offsets=(0, 2))]
    binding = binding_from_selection(
        FieldSelection(fields),
        target_departure_utc=T,
        horizon_hours=10,
    )

    assert binding.enabled is True
    assert binding.coverage_end_utc == T + timedelta(hours=2)
    assert binding.field_ids == tuple(field.id for field in fields)
    assert binding.provider == "test"


def test_current_field_ids_reports_missing_fields():
    fields = [current_field(offsets=(0, 2))]
    manager = FakeManager(fields)
    acquisition = acquire_target_currents(manager, request())

    assert current_field_ids(manager, acquisition.binding) == acquisition.field_ids

    removed = acquisition.field_ids[0]
    del manager.fields.fields[removed]
    with pytest.raises(CurrentDataError, match=removed):
        current_field_ids(manager, acquisition.binding)


def test_provider_exception_is_translated():
    class ExplodingPlanner(FakePlanner):
        def download(self, area, start, end, cancel, report):
            raise RuntimeError("provider exploded")

    manager = FakeManager([])
    manager.coverage_planner = ExplodingPlanner([])

    with pytest.raises(CurrentDataError, match="provider exploded") as raised:
        acquire_target_currents(manager, request())

    assert isinstance(raised.value, CurrentCoverageError) is False
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "provider exploded"


def test_cancellation_propagates_as_its_own_type():
    class CancellingPlanner(FakePlanner):
        def download(self, area, start, end, cancel, report):
            raise DownloadCancelledError("cancelled")

    manager = FakeManager([])
    manager.coverage_planner = CancellingPlanner([])

    with pytest.raises(DownloadCancelledError, match="cancelled"):
        acquire_target_currents(manager, request())


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "target_departure_utc": datetime(2026, 10, 11, 12),
            "horizon_hours": 2,
            "area": (-1.0, -1.0, 1.0, 1.0),
        },
        {"target_departure_utc": T, "horizon_hours": 0, "area": (-1.0, -1.0, 1.0, 1.0)},
        {
            "target_departure_utc": T,
            "horizon_hours": 721,
            "area": (-1.0, -1.0, 1.0, 1.0),
        },
        {"target_departure_utc": T, "horizon_hours": 2, "area": (1.0, -1.0, -1.0, 1.0)},
        {"target_departure_utc": T, "horizon_hours": 2, "area": (-1.0, 1.0, 1.0, -1.0)},
    ],
)
def test_current_request_validation(kwargs):
    with pytest.raises(CurrentDataError):
        CurrentRequest(**kwargs)


def test_binding_round_trip_preserves_coverage_and_ids():
    fields = [current_field(offsets=(0, 2))]
    acquisition = acquire_target_currents(FakeManager(fields), request(horizon=10))
    binding = acquisition.binding

    restored = type(binding).from_dict(binding.to_dict())

    assert restored == binding
    assert restored.coverage_end_utc == T + timedelta(hours=2)
    assert restored.field_ids == acquisition.field_ids
    assert restored.artifact_hashes == acquisition.field_hashes
