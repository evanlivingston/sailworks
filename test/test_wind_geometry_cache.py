# -*- coding: utf-8 -*-
"""The live barb reprojection cache reuses world pixels without changing output."""

import numpy as np
import pytest
from sailworks.rendering import windpainter
from sailworks.rendering.windpainter import WindPainter

from unittest import mock

VIEW = dict(center_lat=0.0, center_lon=0.0, zoom=2, width=400, height=400, step=10)


def make_flat(count=12, seed=0):
    """A tiny synthetic field inside the synthetic viewport."""
    base = np.linspace(0.0, 1.0, count)
    return (
        base * 350.0 + seed,
        base * 18.0 + 2.0,
        np.linspace(-40.0, 40.0, count),
        np.linspace(-60.0, 60.0, count),
    )


FLAT = make_flat()
FLAT_OTHER = make_flat(seed=7)


def plain(value):
    """Turn numpy output into plain containers so dict equality is unambiguous."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def run(flat=FLAT, **overrides):
    kwargs = dict(VIEW)
    kwargs.update(overrides)
    return WindPainter.live_geometry_for_view(flat, **kwargs)


def projections_for(first, second):
    with mock.patch.object(
        windpainter,
        "mercator_project_flat",
        wraps=windpainter.mercator_project_flat,
    ) as project:
        run(**first)
        run(**second)
    return project.call_count


@pytest.fixture(autouse=True)
def clear_cache():
    WindPainter.clear_live_geometry_cache()
    yield
    WindPainter.clear_live_geometry_cache()


def test_repeat_view_reuses_projection():
    with mock.patch.object(
        windpainter,
        "mercator_project_flat",
        wraps=windpainter.mercator_project_flat,
    ) as project:
        first, first_count = run()
        second, second_count = run()

    assert project.call_count == 1
    assert first_count > 0
    assert second_count == first_count
    assert plain(first) == plain(second)


def test_hit_matches_a_fresh_reprojection():
    first, first_count = run()
    WindPainter.clear_live_geometry_cache()
    rebuilt, rebuilt_count = run()
    assert (plain(rebuilt), rebuilt_count) == (plain(first), first_count)


def test_cached_arrays_are_read_only():
    run()
    _flat, _mask, _projected, _on_ground, xxs, yys, idx, _nbytes = next(
        iter(WindPainter._live_geometry_cache.values())
    )
    assert not xxs.flags.writeable
    assert not yys.flags.writeable
    assert not idx.flags.writeable


def projected_pair(offset=0.0):
    """World pixels for the synthetic field, so the viewport sees them."""
    px, py = windpainter.mercator_project_nodes(FLAT[2], FLAT[3], VIEW["zoom"])
    return (np.asarray(px) + offset, np.asarray(py))


@pytest.mark.parametrize(
    "first,second",
    [
        ({}, {"center_lon": 3.0}),
        ({}, {"center_lat": 2.0}),
        ({}, {"zoom": 3}),
        ({}, {"width": 420}),
        ({}, {"height": 420}),
        ({}, {"step": 20}),
        ({}, {"max_count": 3}),
        ({}, {"hide_on_ground": True}),
        ({}, {"palette_key": ("default", 0.8)}),
        ({"palette_key": ("default", 0.8)}, {"palette_key": ("night", 0.8)}),
    ],
)
def test_view_changes_force_reprojection(first, second):
    assert projections_for(first, second) == 2


def test_new_field_forces_reprojection():
    with mock.patch.object(
        windpainter,
        "mercator_project_flat",
        wraps=windpainter.mercator_project_flat,
    ) as project:
        run()
        run(flat=FLAT_OTHER)
    assert project.call_count == 2


def test_land_mask_change_forces_reprojection():
    first_mask = np.zeros(len(FLAT[0]), dtype=bool)
    second_mask = np.zeros(len(FLAT[0]), dtype=bool)
    with mock.patch.object(
        windpainter,
        "mercator_project_flat",
        wraps=windpainter.mercator_project_flat,
    ) as project:
        run(land_mask=first_mask)
        run(land_mask=second_mask)
    assert project.call_count == 2


def test_projected_inputs_are_identity_checked():
    first_projected = projected_pair()
    other_projected = projected_pair(offset=5.0)
    with mock.patch.object(
        windpainter,
        "mercator_project_flat",
        wraps=windpainter.mercator_project_flat,
    ) as project:
        run(projected=first_projected)
        run(projected=first_projected)
        assert project.call_count == 1
        run(projected=other_projected)
    assert project.call_count == 2


def test_on_ground_callable_is_identity_checked():
    first_on_ground = lambda lat, lon: False  # noqa: E731
    other_on_ground = lambda lat, lon: False  # noqa: E731
    with mock.patch.object(
        windpainter,
        "mercator_project_flat",
        wraps=windpainter.mercator_project_flat,
    ) as project:
        run(on_ground=first_on_ground)
        run(on_ground=first_on_ground)
        assert project.call_count == 1
        run(on_ground=other_on_ground)
    assert project.call_count == 2


def test_cache_is_bounded_by_entries():
    for offset in range(WindPainter._LIVE_GEOMETRY_CACHE_ENTRIES + 3):
        run(center_lon=float(offset))
        assert (
            len(WindPainter._live_geometry_cache)
            <= WindPainter._LIVE_GEOMETRY_CACHE_ENTRIES
        )
    assert (
        len(WindPainter._live_geometry_cache)
        == WindPainter._LIVE_GEOMETRY_CACHE_ENTRIES
    )


def test_oversized_entry_is_not_cached(monkeypatch):
    monkeypatch.setattr(WindPainter, "_LIVE_GEOMETRY_CACHE_BYTES", 8)
    run()
    assert len(WindPainter._live_geometry_cache) == 0
    assert WindPainter._live_geometry_cache_bytes == 0


def test_flat_arrays_count_toward_the_byte_budget(monkeypatch):
    run()
    _flat, _mask, _projected, _on_ground, xxs, yys, idx, nbytes = next(
        iter(WindPainter._live_geometry_cache.values())
    )
    flat_bytes = sum(array.nbytes for array in FLAT)
    assert flat_bytes > 0
    assert nbytes >= xxs.nbytes + yys.nbytes + idx.nbytes + flat_bytes
    # A budget that fits the reprojected arrays but not the pinned field.
    monkeypatch.setattr(WindPainter, "_LIVE_GEOMETRY_CACHE_BYTES", nbytes - 1)
    WindPainter.clear_live_geometry_cache()
    run()
    assert len(WindPainter._live_geometry_cache) == 0
    assert WindPainter._live_geometry_cache_bytes == 0
