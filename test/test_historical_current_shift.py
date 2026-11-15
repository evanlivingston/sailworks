"""Target-departure time mapping for historical-ensemble current fields.

The native engine evaluates wind and currents at the *target* departure epoch.
A historical member's current field lives on its own departure timeline, so the
worker shifts that field's time axis by ``member_departure - target_departure``
and holds the last valid instant once the shifted member coverage ends.
"""

import pickle
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from sailworks.core.marine.fields import CurrentField, FieldSelection
from sailworks.core.marine.models import canonical_json
from sailworks.core.marine.nativerequest import NativeRequest
from sailworks.core.marine.routingnative import (
    clamped_current_callable,
    shifted_current_fields,
)

START = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def current_field(east=0.0, end_east=None):
    """Two-timestamp (``START``, ``START + 1h``) 2x2 field, all available."""

    metadata = dict(
        schema_version=1,
        provider="test",
        model="Historical member field",
        composition="total",
        vector_reference="true_east_north",
        sources=[],
        resolution_m=9000,
        run=START.isoformat(),
    )
    x = np.full((2, 2, 2), float(east))
    if end_east is not None:
        x[1] = float(end_east)
    return CurrentField(
        canonical_json(metadata),
        np.array([START.timestamp(), (START + HOUR).timestamp()]),
        np.array([-1, 1]),
        np.array([-1, 1]),
        x,
        np.full((2, 2, 2), 0.0),
    )


def sample_at(selection, instant):
    return selection.sample(0.0, 0.0, instant)


class ShiftedCurrentFieldsTest(unittest.TestCase):
    def test_negative_day_shift_moves_the_lookup(self):
        field = current_field(east=5, end_east=7)
        shifted = shifted_current_fields((field,), -DAY.total_seconds())

        self.assertEqual(len(shifted), 1)
        self.assertIsNot(shifted[0], field)
        self.assertEqual(shifted[0].id, field.id)

        selection = FieldSelection(shifted)
        before = sample_at(selection, START - DAY)
        self.assertEqual(before.status, "available")
        self.assertEqual(tuple(before.values), (5.0, 0.0))

        after = sample_at(selection, START - DAY + HOUR)
        self.assertEqual(after.status, "available")
        self.assertEqual(tuple(after.values), (7.0, 0.0))

    def test_lookup_outside_the_shifted_range_is_unavailable(self):
        field = current_field(east=5, end_east=7)
        shifted = shifted_current_fields((field,), -DAY.total_seconds())
        selection = FieldSelection(shifted)

        too_early = sample_at(selection, START - DAY - HOUR)
        too_late = sample_at(selection, START - DAY + 2 * HOUR)
        for record in (too_early, too_late):
            self.assertNotEqual(record.status, "available")

        # The unshifted axis is also unavailable at the shifted instant: the
        # shift is what moves the lookup, not a widened coverage window.
        original = sample_at(FieldSelection((field,)), START - DAY)
        self.assertNotEqual(original.status, "available")

    def test_shifted_field_is_a_retimed_view_not_a_store_entry(self):
        field = current_field(east=5, end_east=7)
        shifted = shifted_current_fields((field,), -DAY.total_seconds())[0]

        # The dataset id is carried over so provenance still names the frozen
        # field, while the content key and validity window describe the retimed
        # arrays. That is exactly why the view must never enter a FieldStore.
        self.assertEqual(shifted.id, field.id)
        self.assertNotEqual(shifted.content_key, field.content_key)
        manifest = shifted.manifest()
        self.assertEqual(manifest["dataset_id"], field.id)
        self.assertEqual(
            datetime.fromisoformat(manifest["valid_start"]),
            datetime.fromtimestamp(field.times[0] - DAY.total_seconds(), timezone.utc),
        )

    def test_zero_shift_returns_the_input_objects(self):
        fields = (current_field(east=5), current_field(east=1))
        shifted = shifted_current_fields(fields, 0.0)

        self.assertIs(shifted, fields)
        for before, after in zip(fields, shifted):
            self.assertIs(before, after)
        self.assertEqual([f.id for f in shifted], [f.id for f in fields])

    def test_non_finite_shift_is_rejected(self):
        field = current_field()
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                shifted_current_fields((field,), bad)


class ClampedCurrentCallableTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def sample(latitude, longitude, epoch_s):
            self.calls.append((latitude, longitude, epoch_s))
            return (0.1, 0.2, True, "dataset-1")

        self.sample = sample

    def test_clamps_at_the_hold_epoch_and_passes_through_below(self):
        hold = START.timestamp() + 3600
        clamped = clamped_current_callable(self.sample, hold)

        self.assertEqual(clamped(1.0, 2.0, int(hold - 600)), (0.1, 0.2, True, "dataset-1"))
        self.assertEqual(clamped(1.0, 2.0, int(hold)), (0.1, 0.2, True, "dataset-1"))
        self.assertEqual(clamped(1.0, 2.0, int(hold + 86400)), (0.1, 0.2, True, "dataset-1"))
        self.assertEqual(
            [call[2] for call in self.calls],
            [int(hold - 600), int(hold), int(hold)],
        )

    def test_fractional_instants_clamp_to_integer_seconds(self):
        clamped = clamped_current_callable(self.sample, 1000.9)
        clamped(0.0, 0.0, 1001.7)
        clamped(0.0, 0.0, 999.9)
        self.assertEqual([call[2] for call in self.calls], [1000, 999])

    def test_none_returns_the_sampler_unchanged(self):
        self.assertIs(clamped_current_callable(self.sample, None), self.sample)

    def test_three_tuple_passes_through_verbatim(self):
        def three(latitude, longitude, epoch_s):
            return (0.5, -0.5, True)

        clamped = clamped_current_callable(three, 0.0)
        self.assertEqual(clamped(0.0, 0.0, 100), (0.5, -0.5, True))

    def test_non_finite_hold_is_rejected(self):
        with self.assertRaises(ValueError):
            clamped_current_callable(self.sample, float("nan"))


class DummyForecast:
    """Picklable stand-in; ``NativeRequest.__post_init__`` never inspects it."""


def native_request(**overrides):
    values = dict(
        version=1,
        engine="main",
        calculation_id="calc-1",
        name="member",
        start=(0.0, 0.0),
        destination=(1.0, 1.0),
        departure_epoch_s=int(START.timestamp()),
        waypoints=(),
        forecast=DummyForecast(),
        current_field_ids=("field-1",),
        current_priority="highest",
        marine_directory=None,
        polar_rows=(),
        polar_sha256="0" * 64,
        polar_efficiency=1.0,
        provider_descriptors=(),
        coastline_checks=False,
        coastline_clearance_distance_m=0.0,
        use_currents=True,
        auto_download=False,
        wind_speed_pct=0.0,
        wind_dir_offset=0.0,
        tws_max_kn=None,
        min_sailing_speed_kn=None,
        motor_speed_kn=0.0,
        wave_correct=False,
        legacy_algorithm="isochrones",
        unchanged_legacy_parameters=(),
    )
    values.update(overrides)
    return NativeRequest(**values)


class NativeRequestShiftFieldsTest(unittest.TestCase):
    def test_defaults_keep_today_behaviour(self):
        request = native_request()
        self.assertEqual(request.current_time_shift_s, 0.0)
        self.assertIsNone(request.current_hold_epoch_s)

    def test_shift_and_hold_survive_pickling(self):
        hold = START.timestamp() + 3600
        request = native_request(current_time_shift_s=-86400.0, current_hold_epoch_s=hold)

        restored = pickle.loads(pickle.dumps(request))

        self.assertEqual(restored.current_time_shift_s, -86400.0)
        self.assertEqual(restored.current_hold_epoch_s, hold)
        self.assertEqual(restored.departure_epoch_s, request.departure_epoch_s)
        self.assertEqual(restored.current_field_ids, request.current_field_ids)
