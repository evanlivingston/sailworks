from unittest.mock import patch

import numpy as np
import pytest

from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.qt.gpuweather import decode_component
from gweatherrouting.rendering.weatherbarbs import BarbView, stage_barbs
from gweatherrouting.rendering.windpainter import WindPainter, barb_density_parameters
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
