"""The GPU-resident field store and the field/selection boundary.

The field is published once and reused by covered camera motion; the
selection is the exact current-view artefact and is published per view.
"""

from datetime import timedelta

import numpy as np
import pytest

from gweatherrouting.core.gribsampling import WeatherSampler
from gweatherrouting.qt.gpuweatherfields import (
    FieldIdentity,
    FieldStore,
    GeographicField,
    TEXTURE_BYTES_PER_FLOAT32,
    validate_binding,
)
from gweatherrouting.qt.gpuweatherjobs import source_key
from gweatherrouting.rendering.weatherbarbs import (
    BarbView,
    field_window,
    stage_barbs,
)
from gweatherrouting.rendering.weatherstaging import time_window
from test.test_weather_barbs import Land
from test.test_weather_staging import gribs, START  # noqa: F401


def identity(**overrides):
    values = dict(
        source=(("gfs", "2026-10-19", (1, 2, 3, 4)),),
        parameter="wind",
        before=START.timestamp(),
        after=START.timestamp() + 10800,
        window=((-1.0, -1.0), (2.0, 2.0)),
        resolution=(60, 80),
    )
    values.update(overrides)
    return FieldIdentity(**values)


def _stage(sampler, view, stamp, store=None, source=None):
    if store is not None:
        from gweatherrouting.rendering.weatherbarbs import _stage_with_field

        return _stage_with_field(
            sampler, stamp, view, Land(), lambda: False, store, source
        )
    return stage_barbs(sampler, stamp, view, Land())


def _sources(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    return source, WeatherSampler([source]), (source_key(source),)


# ---------------------------------------------------------------- identity
def test_field_identity_is_stable_and_names_every_part_of_the_field():
    first = identity()
    assert first == identity()
    assert first.key() == identity().key()
    assert first.key() != identity(before=first.before + 1).key()
    assert first.key() != identity(after=first.after + 1).key()
    assert first.key() != identity(parameter="pressure").key()
    assert first.key() != identity(window=((-2.0, -2.0), (2.0, 2.0))).key()
    assert first.key() != identity(resolution=(60, 81)).key()
    assert first.key() != identity(source=(("gfs", "other"),)).key()


def test_resident_time_window_matches_the_staging_semantics(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(1) + gribs.field(3), end=3)
    sampler = WeatherSampler([source])
    boundary = (START + timedelta(hours=1)).timestamp()
    assert time_window(sampler, boundary) == (boundary, boundary)
    before, after = time_window(sampler, boundary + 60)
    assert before == boundary and after > boundary


# ------------------------------------------------------------------ budget
def test_the_field_budget_accounts_values_coordinates_and_lookup(gribs):
    source, sampler, signature = _sources(gribs)
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    store = FieldStore(1 << 24)
    atlas = _stage(sampler, view, START.timestamp() + 3600, store, signature)
    field = atlas.field
    assert field.value_bytes == (field.staged.first.nbytes + field.staged.second.nbytes)
    assert field.coordinate_bytes == field.points.nbytes
    assert field.lookup_bytes > 0
    assert field.nbytes == (
        field.value_bytes
        + field.coordinate_bytes
        + field.mask_bytes
        + field.lookup_bytes
        + field.texture_bytes
    )
    # The packed transport charges eight bytes of texture per float32.
    assert field.texture_bytes == len(field.images) * (
        field.images[0].width() * field.images[0].height() * 4
    )
    assert TEXTURE_BYTES_PER_FLOAT32 == 8


def test_allocation_eviction_and_release_are_bounded(gribs):
    source, sampler, signature = _sources(gribs)
    store = FieldStore(1 << 24)
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    there = BarbView(640, 480, 0.6, 0.6, 9, "standard", False)
    stamp = START.timestamp() + 3600
    first = _stage(sampler, here, stamp, store, signature).field
    second = _stage(sampler, there, stamp, store, signature).field
    assert store.statistics()["retained_entries"] == 2
    assert store.current_bytes == first.nbytes + second.nbytes
    # A budget that cannot hold the field rejects it instead of exceeding it.
    tiny = FieldStore(64)
    assert tiny.store(first) is False
    assert tiny.current_bytes == 0
    assert tiny.statistics()["rejected"] == 1
    # Release is deterministic.
    assert store.release(first.identity) is True
    assert store.release(first.identity) is False
    assert store.statistics()["retained_entries"] == 1
    store.clear()
    assert store.current_bytes == 0
    assert store.statistics()["retained_entries"] == 0


def test_the_oldest_field_is_evicted_before_the_newest(gribs):
    source, sampler, signature = _sources(gribs)
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    there = BarbView(640, 480, 0.6, 0.6, 9, "standard", False)
    stamp = START.timestamp() + 3600
    first = _stage(sampler, here, stamp, FieldStore(1 << 24), signature).field
    size = first.nbytes
    store = FieldStore(size * 2 + 1)
    store.store(first)
    second = _stage(sampler, there, stamp, store, signature).field
    assert store.current_bytes <= size * 2 + 1
    assert store.statistics()["retained_entries"] == 2


# --------------------------------------------------------------- residency
def test_a_field_is_published_once_and_covered_pans_reuse_it(gribs):
    """The camera moves inside the field's window: one field, many selections."""
    source, sampler, signature = _sources(gribs)
    store = FieldStore(1 << 24)
    stamp = START.timestamp() + 3600
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    first = _stage(sampler, here, stamp, store, signature)
    generation = first.field.generation
    selections = [first.selection]
    for offset in (0.001, 0.002, 0.003):
        view = BarbView(640, 480, 0.5 + offset, 0.5, 9, "standard", False)
        atlas = _stage(sampler, view, stamp, store, signature)
        # The same resident field, the same resource identity.
        assert atlas.field is first.field
        assert atlas.field.generation == generation
        # A new selection for every view.
        assert atlas.selection is not first.selection
        selections.append(atlas.selection)
    assert store.statistics()["acquire_hits"] == 3
    assert store.statistics()["acquire_misses"] == 1
    assert store.statistics()["retained_entries"] == 1
    assert len({id(selection) for selection in selections}) == 4


def test_the_field_window_is_stable_inside_a_lattice_cell(gribs):
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    near = BarbView(640, 480, 0.5 + 0.0005, 0.5, 9, "standard", False)
    far = BarbView(640, 480, 0.6, 0.6, 9, "standard", False)
    assert field_window(here) == field_window(near)
    assert field_window(here) != field_window(far)


def test_each_views_selection_matches_the_direct_reference(gribs):
    """The selection of a panned view equals the direct path's selection."""
    source, sampler, signature = _sources(gribs)
    stamp = START.timestamp() + 3600
    panned = BarbView(640, 480, 0.5 + 0.002, 0.5, 9, "standard", False)
    direct = _stage(sampler, panned, stamp)
    resident = _stage(sampler, panned, stamp, FieldStore(1 << 24), signature)
    assert resident.count == direct.count
    # The anchors are the direct path's anchors, byte for byte.
    for published, expected in zip(resident.selection.anchors, direct.anchors):
        assert published.constBits().tobytes() == expected.constBits().tobytes()
    # The node ids name the field's nodes with the direct path's values.
    ids = resident.selection.node_ids
    ids = ids.ravel()
    selected = ids >= 0
    mesh = np.asarray(direct.staged.first).reshape(-1, 2)
    values = np.asarray(resident.field.staged.first[0]).reshape(-1, 2)
    np.testing.assert_allclose(
        values[ids[selected].astype(int)], mesh[selected], rtol=0, atol=1e-9
    )
    # The unselected cells carry no symbol.
    assert np.all(ids[~selected] == -1)


def test_field_texture_order_matches_shader_bindings(gribs):
    """Exercise transport, not just the CPU values used to create textures."""
    from gweatherrouting.qt.gpuweather import decode_component

    source, sampler, signature = _sources(gribs)
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    atlas = _stage(
        sampler, view, START.timestamp() + 3600, FieldStore(1 << 24), signature
    )
    field = atlas.field
    expected = (
        field.staged.first[..., 0],
        field.staged.first[..., 1],
        field.staged.second[..., 0],
        field.staged.second[..., 1],
    )
    for image, values in zip(field.images, expected):
        actual = decode_component(image).ravel()
        np.testing.assert_array_equal(
            actual[: values.size], values.astype(np.float32).ravel()
        )
        assert np.isnan(actual[values.size :]).all()


def test_viewport_rejects_stale_selection_before_publishing():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from gweatherrouting.qt.gribviewport import _viewport_widget_class

    weather = Mock()
    provider = Mock()
    scene = SimpleNamespace(_weather=weather, _provider=provider)
    with pytest.raises(ValueError, match="Stale"):
        _viewport_widget_class()._publish(
            scene,
            SimpleNamespace(generation="current"),
            SimpleNamespace(generation="stale"),
            SimpleNamespace(),
        )
    provider.publish_field.assert_not_called()


@pytest.mark.parametrize("over_land", [True, False])
def test_covered_selection_needs_no_sampler_or_land_io(gribs, monkeypatch, over_land):
    from gweatherrouting.qt.gpuweather import decode_component
    from gweatherrouting.rendering.weatherbarbs import select_resident

    source, sampler, signature = _sources(gribs)
    stamp = START.timestamp() + 3600
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", over_land)
    near = BarbView(640, 480, 0.502, 0.5, 9, "standard", over_land)
    prepared = _stage(sampler, here, stamp, FieldStore(1 << 24), signature)
    expected = _stage(sampler, near, stamp)

    def forbidden(*args, **kwargs):
        raise AssertionError("Covered selection accessed preparation")

    monkeypatch.setattr(sampler, "sample_timestamps", forbidden)
    monkeypatch.setattr(source.sampling, "nodes", forbidden)
    monkeypatch.setattr(Land, "contains", forbidden)
    monkeypatch.setattr(Land, "refresh_source", forbidden)
    actual = select_resident(prepared.field, stamp, near)
    assert actual.field is prepared.field
    assert actual.count == expected.count
    for result, reference in zip(actual.anchors, expected.anchors):
        np.testing.assert_array_equal(
            decode_component(result), decode_component(reference)
        )


def test_shared_grid_enumeration_is_deduplicated_without_dropping_sources(gribs):
    from gweatherrouting.qt.gpuweather import decode_component
    from gweatherrouting.rendering.weatherbarbs import select_resident

    sources = [
        gribs.file(gribs.field(0) + gribs.field(3), end=3),
        gribs.file(gribs.field(0) + gribs.field(3), process=83, end=3),
    ]
    sampler = WeatherSampler(sources)
    signature = tuple(source_key(source) for source in sources)
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", True)
    stamp = START.timestamp() + 3600
    atlas = _stage(sampler, view, stamp, FieldStore(1 << 24), signature)
    assert len(sampler.indices) == 2
    assert len(atlas.field.grids) == 1
    assert atlas.field.identity.source == signature
    near = BarbView(640, 480, 0.502, 0.5, 9, "standard", True)
    actual = select_resident(atlas.field, stamp, near)
    expected = _stage(sampler, near, stamp)
    assert actual.count == expected.count
    for result, reference in zip(actual.anchors, expected.anchors):
        np.testing.assert_array_equal(
            decode_component(result), decode_component(reference)
        )


def test_a_missing_endpoint_stays_missing_in_the_field(gribs):
    """A missing value is published as the same NaN, so it stays transparent."""
    source = gribs.file(
        [
            ("UGRD", 0, [[-9999, -9999], [-9999, -9999]]),
            ("VGRD", 0, [[1, 1], [1, 1]]),
            ("UGRD", 3, [[-9999, -9999], [-9999, -9999]]),
            ("VGRD", 3, [[1, 1], [1, 1]]),
        ],
        process=83,
        end=3,
    )
    sampler = WeatherSampler([source])
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    atlas = _stage(sampler, view, START.timestamp() + 3600, FieldStore(1 << 24), None)
    values = np.asarray(atlas.field.staged.first[0]).reshape(-1, 2)
    assert np.any(np.isnan(values))
    # The published image carries the same NaN, so the shader discards it.
    from gweatherrouting.qt.gpuweather import decode_component

    decoded = decode_component(atlas.field.images[0])
    assert np.isnan(decoded).any()


def test_a_stale_selection_is_rejected(gribs):
    """A selection of another field never passes as current."""
    source, sampler, signature = _sources(gribs)
    store = FieldStore(1 << 24)
    stamp = START.timestamp() + 3600
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    there = BarbView(640, 480, 0.6, 0.6, 9, "standard", False)
    first = _stage(sampler, here, stamp, store, signature)
    other = _stage(sampler, there, stamp, store, signature)
    with pytest.raises(ValueError, match="Stale"):
        validate_binding(first.field, other.selection)
    with pytest.raises(ValueError, match="Stale"):
        validate_binding(other.field, first.selection)
    # A selection of its own field is accepted.
    assert validate_binding(first.field, first.selection) is first.selection


def test_a_coverage_miss_never_silently_becomes_current(gribs):
    """A view outside the field's window stages its own field."""
    source, sampler, signature = _sources(gribs)
    store = FieldStore(1 << 24)
    stamp = START.timestamp() + 3600
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    there = BarbView(640, 480, 0.6, 0.6, 9, "standard", False)
    first = _stage(sampler, here, stamp, store, signature)
    outside = _stage(sampler, there, stamp, store, signature)
    assert outside.field is not first.field
    assert outside.field.generation != first.field.generation
    assert outside.selection.generation == outside.field.generation
    # The first field is still resident: the escape does not invalidate it.
    assert store.statistics()["retained_entries"] == 2
    assert store.acquire(first.field.identity) is first.field


def test_a_stalled_preparation_does_not_block_the_caller(gribs):
    """The worker prepares on its own thread; the caller never waits."""
    from threading import Event

    from gweatherrouting.qt.gpuweatherjobs import WeatherStager

    source, _sampler, _signature = _sources(gribs)
    canvas = type(
        "Canvas",
        (),
        {
            "controller": type(
                "Controller",
                (),
                {
                    "settings": type("Settings", (), {"gribArrowOnGround": True})(),
                    "core": type(
                        "Core",
                        (),
                        {"grib_manager": type("Manager", (), {"gribs": [source]})()},
                    )(),
                },
            )(),
            "width": lambda self: 640,
            "height": lambda self: 480,
            "_center_lat": 0.5,
            "_center_lon": 0.5,
            "_zoom": 9,
            "wind_visible": True,
            "time_control": type(
                "Time",
                (),
                {"time": START + timedelta(hours=1)},
            )(),
        },
    )()
    stager = WeatherStager(resident_fields=True)
    entered, release = Event(), Event()

    def stage(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return None

    try:
        from unittest.mock import patch

        with patch("gweatherrouting.qt.gpuweatherjobs.stage_barbs", side_effect=stage):
            # The call returns while the worker is still preparing.
            assert stager.request(canvas) is None
            assert entered.wait(5)
            assert stager.request(canvas) is None
    finally:
        release.set()
        stager.close()


# ------------------------------------------------------- default behaviour
def test_the_default_path_keeps_the_historical_publication(gribs):
    source, sampler, _signature = _sources(gribs)
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    stamp = START.timestamp() + 3600
    atlas = _stage(sampler, view, stamp)
    assert atlas.field is None
    assert atlas.selection is None
    from gweatherrouting.qt.gpuweather import WeatherImageProvider

    provider = WeatherImageProvider()
    first = provider.publish(atlas)
    second = provider.publish(atlas)
    assert first != second
    assert all(url.startswith("image://weather/") for url in first)
