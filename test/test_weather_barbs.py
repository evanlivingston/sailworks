from unittest.mock import patch

import numpy as np
import pytest

from sailworks.common import wave_color
from sailworks.core.gribsampling import WeatherSampler
from sailworks.qt.gpuweather import decode_component
from sailworks.qt.viewportpublication import LayerRole
from sailworks.rendering.weatherbarbs import (
    BarbView,
    stage_barbs,
    stage_wave_selection,
)
from sailworks.rendering.windpainter import (
    BARB_DENSITIES,
    WAVE_ARROW_SHAFT_MAX,
    WAVE_ARROW_SHAFT_MIN,
    WindPainter,
    barb_bucket_colors,
    barb_density_parameters,
    build_wave_geometry,
    wave_arrow_length,
    wave_bucket_colors,
)
from test.test_weather_staging import gribs, START  # noqa: F401


class Land:
    def refresh_source(self, scale):
        pass

    def contains(self, lat, lon):
        return lat > 0.5


@pytest.mark.parametrize("density", ["sparse", "standard", "dense", "very_dense"])
@pytest.mark.parametrize("over_land", [False, True])
def test_native_selection_matches_cpu_cells_and_land_policy(gribs, density, over_land):
    source = gribs.file(
        gribs.field(0, size=11) + gribs.field(3, size=11),
        transform=(-0.05, 0.1, 0, -0.05, 0, 0.1),
        end=3,
    )
    sampler = WeatherSampler([source])
    view = BarbView(640, 480, 0.5, 0.5, 9, density, over_land)
    stamp = START.timestamp() + 3600
    atlas = stage_barbs(sampler, stamp, view, Land())
    selected = np.isfinite(atlas.staged.first).all(axis=-1)
    ax, ay = map(decode_component, atlas.anchors)
    actual = np.column_stack((ax[selected], ay[selected]))
    positions = np.unique(source.sampling.nodes(((-1, -1), (2, 2))), axis=0)
    positions = positions[np.lexsort((positions[:, 0], positions[:, 1]))]
    uv = sampler.sample_timestamps("wind", stamp, positions[:, 0], positions[:, 1])
    flat = (
        np.degrees(np.arctan2(-uv[:, 0], -uv[:, 1])) % 360,
        np.hypot(uv[:, 0], uv[:, 1]),
        positions[:, 0],
        positions[:, 1],
    )
    step, cap = barb_density_parameters(density)
    with patch.object(WindPainter, "build_barb_geometry", return_value={}) as build:
        _, count = WindPainter.live_geometry_for_view(
            flat,
            0.5,
            0.5,
            9,
            640,
            480,
            step=step,
            max_count=cap,
            land_mask=None if over_land else positions[:, 0] > 0.5,
        )
    xx, yy, _dirs, _speeds = build.call_args.args
    expected = np.column_stack((xx, yy))
    # Slots are row-major; CPU selection is native longitude-major.
    actual = actual[np.lexsort((actual[:, 0], actual[:, 1]))]
    expected = expected[np.lexsort((expected[:, 0], expected[:, 1]))]
    np.testing.assert_allclose(actual, expected, atol=3e-5, rtol=0)
    assert atlas.count == count
    assert atlas.nbytes < 32 * 1024 * 1024


def test_cancel_and_unthinned_mode_do_not_publish(gribs):
    sampler = WeatherSampler([gribs.file(gribs.field(0))])
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", True)
    assert stage_barbs(sampler, START.timestamp(), view, Land(), lambda: True) is None
    with pytest.raises(ValueError, match="Unthinned"):
        stage_barbs(
            sampler,
            START.timestamp(),
            BarbView(640, 480, 0.5, 0.5, 9, "all", True),
            Land(),
        )


def _only_lines(geometry):
    """The lines of a one-glyph geometry, whatever its height bucket is."""
    assert len(geometry) == 1
    lines, tris, dots = next(iter(geometry.values()))
    assert not tris and not dots
    return lines


def _shaft_length(lines):
    x1, y1, x2, y2 = lines[0]
    return float(np.hypot(x2 - x1, y2 - y1))


def test_wave_arrow_length_follows_period_and_clamps_both_ends():
    assert wave_arrow_length(4) == pytest.approx(WAVE_ARROW_SHAFT_MIN)
    assert wave_arrow_length(12) == pytest.approx(
        (WAVE_ARROW_SHAFT_MIN + WAVE_ARROW_SHAFT_MAX) / 2
    )
    assert wave_arrow_length(20) == pytest.approx(WAVE_ARROW_SHAFT_MAX)
    # Clamped at both ends, and a missing/zero period keeps the minimum.
    assert wave_arrow_length(2) == pytest.approx(WAVE_ARROW_SHAFT_MIN)
    assert wave_arrow_length(60) == pytest.approx(WAVE_ARROW_SHAFT_MAX)
    assert wave_arrow_length(0) == pytest.approx(WAVE_ARROW_SHAFT_MIN)
    assert wave_arrow_length(None) == pytest.approx(WAVE_ARROW_SHAFT_MIN)


def test_wave_geometry_length_increases_with_period():
    lengths = [
        _shaft_length(_only_lines(build_wave_geometry([0], [0], [0], [1.5], [period])))
        for period in (4, 8, 12, 16, 20)
    ]
    assert lengths == sorted(lengths)
    assert lengths[0] == pytest.approx(WAVE_ARROW_SHAFT_MIN)
    assert lengths[-1] == pytest.approx(WAVE_ARROW_SHAFT_MAX)
    # Past the top of the range the arrow stays clamped, not unbounded.
    assert _shaft_length(
        _only_lines(build_wave_geometry([0], [0], [0], [1.5], [40]))
    ) == pytest.approx(WAVE_ARROW_SHAFT_MAX)


def test_wave_geometry_zero_period_draws_minimum_length():
    lines = _only_lines(build_wave_geometry([10], [10], [45], [1.5], [0]))
    assert len(lines) == 3  # shaft plus two head strokes
    assert _shaft_length(lines) == pytest.approx(WAVE_ARROW_SHAFT_MIN)


def test_wave_geometry_colour_bucket_follows_height():
    geometry = build_wave_geometry(
        [0, 50, 100], [0, 0, 0], [0, 0, 0], [0.2, 1.2, 3.7], [8, 8, 8]
    )
    assert sorted(geometry) == [0, 2, 7]
    # The buckets are the legend's own, not a second ramp.
    colors = wave_bucket_colors()
    for bucket in geometry:
        assert colors[bucket] == wave_color(bucket * 0.5)


def test_wave_geometry_uses_the_coming_from_convention():
    # 90 deg: waves come from the east, so the shaft points east (right).
    lines = _only_lines(build_wave_geometry([0], [0], [90], [1.0], [4]))
    x1, y1, tip_x, tip_y = lines[0]
    assert (x1, y1) == (0.0, 0.0)
    assert tip_x == pytest.approx(WAVE_ARROW_SHAFT_MIN)
    assert tip_y == pytest.approx(0.0)
    # 0 deg: waves come from the north, so the shaft points up (screen -y).
    lines = _only_lines(build_wave_geometry([0], [0], [0], [1.0], [4]))
    _x1, _y1, tip_x, tip_y = lines[0]
    assert tip_x == pytest.approx(0.0)
    assert tip_y == pytest.approx(-WAVE_ARROW_SHAFT_MIN)
    # A single head: the two head strokes meet at the tip, and no feathers
    # (which would read as Beaufort force on a wave arrow) are drawn.
    assert len(lines) == 3
    assert lines[1][:2] == lines[2][:2] == (tip_x, tip_y)
    assert lines[1][2:] != lines[2][2:]


def test_wave_selection_needs_every_band():
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", True)
    stamp = START.timestamp()
    bands = {name: object() for name in ("wave_height", "wave_period")}
    # A source missing the direction band draws no arrows at all: a 0 degree
    # direction would be a fabricated bearing.
    assert stage_wave_selection(None, bands, stamp, view, Land()) is None
    assert stage_wave_selection(None, {}, stamp, view, Land()) is None
    assert stage_wave_selection(None, None, stamp, view, Land()) is None


def test_wave_arrows_and_wind_barbs_are_independent_layers():
    # Distinct publication roles: disabling waves withdraws WAVE_FILL and
    # WAVE_BARBS and never touches BARBS.
    assert LayerRole.WAVE_BARBS != LayerRole.BARBS
    assert LayerRole.WAVE_BARBS != LayerRole.WAVE_FILL
    # The wind palette is unchanged by the wave ramp.
    assert barb_bucket_colors() != wave_bucket_colors()
    from sailworks.core.settings import SettingsManager

    settings = SettingsManager()
    # Its own setting, in the same density vocabulary as the wind barbs.
    assert settings.gribWaveDensity in dict.fromkeys(
        key for key, _label, _spacing in BARB_DENSITIES
    )
    wind_before = settings.gribBarbDensity
    settings.gribWaveDensity = "dense"
    assert settings.gribBarbDensity == wind_before
