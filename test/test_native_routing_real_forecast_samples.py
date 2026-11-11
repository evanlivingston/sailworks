"""Sampler, current-selection and edge checks for the real GFS artifact.

Companion to ``test_native_routing_real_forecast.py``. These tests exercise the
established sampler and selection seam directly so interpolation, cell edges,
masks and unavailable samples are covered independently of the native bridge
differential. No sampling or routing policy is changed here.
"""

from __future__ import annotations

import math
import sys
from datetime import timedelta
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST_DIR))

from test_native_routing_real_forecast import (  # noqa: E402
    ARTIFACT_BOUNDS,
    CELL_CENTERS,
    CYCLE,
    CYCLE_EPOCH_S,
    ENVELOPE_CORNER,
    KNOTS_PER_METRE_PER_SECOND,
    _current_field,
    _grid_index,
    _open_source,
    _raw_uv,
    _strong_current_selection,
    _wind_components,
)

from sailworks.core.gribsampling import WeatherSampler, wind_value  # noqa: E402
from sailworks.core.marine.fields import FieldSelection  # noqa: E402
from sailworks.core.marine.routingnative import (  # noqa: E402
    current_callable_from_selection,
    wind_callable_from_sampler,
)


def test_direct_sampler_reads_cell_centers_and_interpolates_between_hours():
    source = _open_source()
    try:
        sampler = WeatherSampler([source])
        u0, v0 = _raw_uv(source.dataset, 0)
        u3, v3 = _raw_uv(source.dataset, 3)

        for latitude, longitude in CELL_CENTERS:
            row, column = _grid_index(source.dataset, latitude, longitude)
            expected = wind_value((u0[row, column], v0[row, column]))
            sampled = sampler.wind_at(CYCLE, latitude, longitude)
            assert (
                sampled is not None
            ), "a cell center inside the artifact is unavailable"
            assert sampled[0] == pytest.approx(expected[0], abs=1e-12)
            assert sampled[1] == pytest.approx(expected[1], abs=1e-12)

        # 13:30Z is halfway between the 12Z and 15Z frames; sampling blends the
        # U/V components linearly in time (see docs/weather-sampling.md).
        half = CYCLE + timedelta(minutes=90)
        row, column = _grid_index(source.dataset, *CELL_CENTERS[0])
        expected = wind_value(
            (
                u0[row, column] + 0.5 * (u3[row, column] - u0[row, column]),
                v0[row, column] + 0.5 * (v3[row, column] - v0[row, column]),
            )
        )
        sampled = sampler.wind_at(half, *CELL_CENTERS[0])
        assert sampled is not None
        assert sampled[0] == pytest.approx(expected[0], abs=1e-12)
        assert sampled[1] == pytest.approx(expected[1], abs=1e-12)
    finally:
        source.close()


def test_wind_callable_matches_sampler_and_reports_unavailable_cells():
    source = _open_source()
    try:
        sampler = WeatherSampler([source])
        wind = wind_callable_from_sampler(sampler)

        for instant in (CYCLE, CYCLE + timedelta(minutes=90)):
            epoch_s = int(instant.timestamp())
            for latitude, longitude in CELL_CENTERS:
                expected = sampler.wind_at(instant, latitude, longitude)
                assert expected is not None
                # Both the adapter and the sampler use the
                # (from_degrees, speed_ms) contract; the adapter appends
                # availability as a third element.
                wind_from, wind_speed, available = wind(latitude, longitude, epoch_s)
                assert available is True
                assert (wind_from, wind_speed) == pytest.approx(expected, abs=1e-12)
                assert _wind_components((wind_from, wind_speed)) == pytest.approx(
                    _wind_components(expected), abs=1e-12
                )

        # The envelope corner is outside the cell-centred stencil; it is
        # unavailable rather than clamped or assumed calm, and the callable
        # does not fabricate zero wind as available.
        assert sampler.wind_at(CYCLE, *ENVELOPE_CORNER) is None
        assert wind(*ENVELOPE_CORNER, CYCLE_EPOCH_S) == (0.0, 0.0, False)

        # Before the first valid hour and past the last one the artifact is
        # unavailable; a request edge must not silently reuse an endpoint.
        assert sampler.wind_at(CYCLE - timedelta(hours=1), *CELL_CENTERS[0]) is None
        assert wind(*CELL_CENTERS[0], CYCLE_EPOCH_S - 3600) == (0.0, 0.0, False)
        # The artifact carries the complete 120 h horizon; its last frame is
        # available and anything beyond it is not extended or clamped.
        horizon = CYCLE + timedelta(hours=120)
        assert sampler.wind_at(horizon, *CELL_CENTERS[0]) is not None
        assert sampler.wind_at(horizon + timedelta(hours=1), *CELL_CENTERS[0]) is None
        assert sampler.wind_at(horizon + timedelta(hours=3), *CELL_CENTERS[0]) is None
        assert wind(*CELL_CENTERS[0], CYCLE_EPOCH_S + 121 * 3600) == (
            0.0,
            0.0,
            False,
        )

        # The northern envelope edge is outside the cell-centred stencil.
        assert sampler.wind_at(CYCLE, ARTIFACT_BOUNDS["north"], -124.5) is None
    finally:
        source.close()


def test_current_callable_matches_the_real_selection_with_strong_currents():
    selection = _strong_current_selection()
    current = current_callable_from_selection(selection)

    for instant in (
        CYCLE,
        CYCLE + timedelta(minutes=90),
        CYCLE + timedelta(hours=120),
    ):
        epoch_s = int(instant.timestamp())
        for latitude, longitude in CELL_CENTERS:
            sample = selection.sample(latitude, longitude, instant)
            assert sample.status == "available"
            east, north = sample.values
            assert (
                math.hypot(east, north) * KNOTS_PER_METRE_PER_SECOND > 2.0
            ), "the strong-current case is not strong"
            assert current(latitude, longitude, epoch_s)[:2] == pytest.approx(
                (float(east), float(north)), abs=1e-12
            )
            assert current(latitude, longitude, epoch_s)[2] is True

        outside = selection.sample(*CELL_CENTERS[0], CYCLE - timedelta(hours=1))
        assert outside.status != "available"
        assert current(*CELL_CENTERS[0], CYCLE_EPOCH_S - 3600) == (
            0.0,
            0.0,
            False,
        )

    # Unlike wind, the current selection does not bridge a three-hour gap: the
    # adapter reports it unavailable instead of inventing an interpolation.
    coarse = FieldSelection(
        [
            _current_field(
                2.6,
                -1.4,
                [CYCLE_EPOCH_S, CYCLE_EPOCH_S + 3 * 3600, CYCLE_EPOCH_S + 6 * 3600],
                [41.9, 47.1],
                [-126.1, -121.9],
                run=CYCLE.isoformat(),
            )
        ]
    )
    gap = coarse.sample(*CELL_CENTERS[0], CYCLE + timedelta(minutes=90))
    assert gap.status == "time_gap"
    coarse_current = current_callable_from_selection(coarse)
    assert coarse_current(*CELL_CENTERS[0], CYCLE_EPOCH_S + 90 * 60) == (
        0.0,
        0.0,
        False,
    )


def test_artifact_bounds_admit_the_override_route_and_reject_edges():
    source = _open_source()
    try:
        sampler = WeatherSampler([source])
        assert sampler.wind_at(CYCLE, *CELL_CENTERS[0]) is not None
        assert sampler.wind_at(CYCLE, *CELL_CENTERS[1]) is not None
        # Between the two centres, still inside the covered window.
        assert sampler.wind_at(CYCLE, 46.125, -124.25) is not None
        # Outside the pixel span east/west and north/south.
        assert sampler.wind_at(CYCLE, 46.0, ARTIFACT_BOUNDS["east"]) is None
        assert sampler.wind_at(CYCLE, 46.0, ARTIFACT_BOUNDS["west"]) is None
        assert sampler.wind_at(CYCLE, ARTIFACT_BOUNDS["north"], -124.5) is None
        assert sampler.wind_at(CYCLE, ARTIFACT_BOUNDS["south"], -124.5) is None
        # wind_value takes U/V components, so positive U and V mean wind from
        # the south-west and positive east/north components.
        east, north = _wind_components(wind_value((10.0, 5.0)))
        assert east > 0.0
        assert north > 0.0
    finally:
        source.close()
