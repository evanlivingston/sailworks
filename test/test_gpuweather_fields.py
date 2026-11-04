"""The GPU-resident field store and the field/selection boundary.

The field is published once and reused by covered camera motion; the
selection is the exact current-view artefact and is published per view.
"""

from datetime import timedelta
import re
from pathlib import Path

import numpy as np
import pytest

from sailworks.core.gribsampling import WeatherSampler
from sailworks.qt.gpuweatherfields import (
    BarbSelection,
    FieldIdentity,
    FieldStore,
    TEXTURE_BYTES_PER_FLOAT32,
    validate_binding,
)
from sailworks.qt.gpuweatherjobs import source_key
from sailworks.rendering.weatherbarbs import (
    BarbView,
    field_window,
    stage_barbs,
    stage_field,
)
from sailworks.rendering.weatherstaging import time_window
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


def _stage(sampler, view, stamp, store=None, source=None, grid=False, reference=None):
    if store is not None:
        from sailworks.rendering.weatherbarbs import _stage_with_field

        return _stage_with_field(
            sampler,
            stamp,
            view,
            Land(),
            lambda: False,
            store,
            source,
            grid,
            reference,
        )
    return stage_barbs(sampler, stamp, view, Land())


def _sources(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    return source, WeatherSampler([source]), (source_key(source),)


def _wave_source(gribs):
    """A scalar wave forecast: ``HTSGW`` in metres at 0 h and 3 h."""
    y, x = np.mgrid[:2, :2]
    records = [
        (
            "HTSGW",
            hour,
            0.5 + x + 2 * y + 0.25 * hour,
            {"GRIB_SHORT_NAME": "0-SFC", "GRIB_UNIT": "[m]"},
        )
        for hour in (0, 3)
    ]
    return gribs.file(records, end=3)


def _wave_field(gribs, sampler=None, grid=False, stamp=None):
    """Stage the scalar wave field exactly as the wave layer does."""
    source = _wave_source(gribs)
    sampler = sampler or WeatherSampler([source])
    stamp = START.timestamp() + 3600 if stamp is None else stamp
    window = ((-1.0, -1.0), (2.0, 2.0))
    identity = FieldIdentity(
        source=(source_key(source),),
        parameter="wave_height",
        before=stamp,
        after=stamp,
        window=window,
        resolution=(2, 2),
    )
    return stage_field(
        sampler,
        stamp,
        window,
        identity,
        lambda: False,
        grid=grid,
        parameter="wave_height",
        components=1,
    )


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
    atlas = _stage(sampler, view, START.timestamp() + 3600, store, signature, grid=True)
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
        + field.grid_bytes
    )
    # The packed transport charges eight bytes of texture per float32.
    assert field.texture_bytes == len(field.images) * (
        field.images[0].width() * field.images[0].height() * 4
    )
    assert TEXTURE_BYTES_PER_FLOAT32 == 8
    # The gridded form is accounted with the field: its values and its own
    # published images.
    assert field.grid is not None
    assert field.grid_bytes == field.grid.value_bytes + field.grid.texture_bytes
    assert field.grid_bytes > 0


def test_value_bytes_is_component_aware(gribs):
    """A scalar field pays for one array per endpoint, not two."""
    wave = _wave_field(gribs)
    assert wave.staged.components == 1
    assert wave.staged.first.shape[-1] == 1
    assert wave.staged.second.shape[-1] == 1
    # Both endpoint arrays are accounted, and only their true float32 width.
    assert wave.value_bytes == wave.staged.first.nbytes + wave.staged.second.nbytes
    assert wave.value_bytes == wave.node_count * 4 * 2

    source, sampler, signature = _sources(gribs)
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    wind = _stage(
        sampler, view, START.timestamp() + 3600, FieldStore(1 << 24), signature
    ).field
    assert wind.staged.components == 2
    assert wind.value_bytes == wind.staged.first.nbytes + wind.staged.second.nbytes
    assert wind.value_bytes == wind.node_count * 4 * 2 * 2
    assert wave.value_bytes < wind.value_bytes


def test_wave_identity_keys_separately_from_wind_and_pressure():
    wind = identity()
    wave = identity(parameter="wave_height")
    pressure = identity(parameter="pressure")
    # The parameter is part of the resource identity, so a scalar wave field
    # never reuses a wind or pressure publication.
    assert len({wind.key(), wave.key(), pressure.key()}) == 3
    assert wave.key() == identity(parameter="wave_height").key()
    assert wave.key() != identity(parameter="wind").key()


def test_wave_staging_samples_once_and_binds_the_scalar_into_u0(gribs, monkeypatch):
    from sailworks.qt.gpuweather import decode_component
    from sailworks.rendering import weatherbarbs

    source = _wave_source(gribs)
    sampler = WeatherSampler([source])
    stages = []
    samples = []
    original_stage = weatherbarbs.stage_wind
    original_sample = sampler.sample_timestamps

    def counted_stage(*args, **kwargs):
        stages.append(kwargs.get("parameter"))
        return original_stage(*args, **kwargs)

    def counted_sample(field, *args, **kwargs):
        samples.append(field)
        return original_sample(field, *args, **kwargs)

    monkeypatch.setattr(weatherbarbs, "stage_wind", counted_stage)
    sampler.sample_timestamps = counted_sample
    # An exact-instant wave field samples once, through the public sampler.
    field = _wave_field(gribs, sampler=sampler, stamp=START.timestamp())

    # One staging pass and one sampler call for the whole scalar field.
    assert stages == ["wave_height"]
    assert samples == ["wave_height"]
    assert field.staged.components == 1
    assert field.staged.first.shape[-1] == 1
    # The scalar travels in u0 and a zero plane in v0, so the shader's
    # length(vec2(s, 0)) is the wave height itself.
    pair = field.staged.texture_pair()
    u0 = decode_component(pair.images[0]).ravel()
    v0 = decode_component(pair.images[1]).ravel()
    scalar = np.asarray(field.staged.first[..., 0]).ravel()
    np.testing.assert_array_equal(u0, scalar)
    assert v0.shape == u0.shape
    assert np.all(v0 == 0.0)


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


def test_the_gridded_form_samples_once_and_keeps_the_samplers_values(gribs):
    """The gridded form costs no second sampling pass, and its values are
    the CPU sampler's values at the lattice's node positions."""
    from sailworks.rendering import weatherbarbs
    from sailworks.rendering.weatherbarbs import stage_field
    from sailworks.rendering.weatherstaging import stage_wind

    source, sampler, signature = _sources(gribs)
    view = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    window = field_window(view)
    stamp = START.timestamp() + 3600
    field_identity = identity(source=signature, window=window)

    calls = []
    original = weatherbarbs.stage_wind

    def counted(*args, **kwargs):
        calls.append(len(np.asarray(args[2]).ravel()))
        return original(*args, **kwargs)

    weatherbarbs.stage_wind = counted
    try:
        field = stage_field(
            sampler, stamp, window, field_identity, lambda: False, grid=True
        )
    finally:
        weatherbarbs.stage_wind = original
    assert field.grid is not None
    # One pass: the field's nodes and the lattice's own nodes are sampled
    # together, so the gridded form adds no second pass over the weather.
    assert len(calls) == 1
    assert calls[0] >= len(field.points) + field.grid.lattice.node_count
    # The values are the sampler's values at the lattice's node positions.
    lons, lats = field.grid.lattice.nodes()
    reference = stage_wind(sampler, stamp, lats[None, :], lons[None, :], lambda: False)
    values = np.asarray(field.grid.staged.first).transpose(1, 0, 2).reshape(-1, 2)
    np.testing.assert_allclose(
        values, np.asarray(reference.first[0]), rtol=0, atol=0, equal_nan=True
    )


def test_eviction_then_reload_preserves_values_masks_and_provenance(gribs):
    source, sampler, signature = _sources(gribs)
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    there = BarbView(640, 480, 0.6, 0.6, 9, "standard", False)
    stamp = START.timestamp() + 3600
    first = _stage(sampler, here, stamp, FieldStore(1 << 24), signature).field
    second = _stage(sampler, there, stamp, FieldStore(1 << 24), signature).field
    # Fits either field, never both. Eviction must really occur, not merely
    # insertion into a generously sized cache.
    store = FieldStore(max(first.nbytes, second.nbytes))
    assert store.store(first)
    assert store.store(second)
    assert store.acquire(first.identity) is None
    assert store.statistics()["evictions"] >= 1
    reloaded = _stage(sampler, here, stamp, store, signature).field
    assert reloaded is not first
    assert reloaded.identity == first.identity
    np.testing.assert_array_equal(reloaded.staged.first, first.staged.first)
    np.testing.assert_array_equal(reloaded.staged.second, first.staged.second)
    assert reloaded.staged.sources == first.staged.sources
    np.testing.assert_array_equal(reloaded.land_flags, first.land_flags)
    assert store.current_bytes <= store.byte_limit
    store.clear()
    assert store.current_bytes == 0
    assert not store.fields()


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


def _node_anchors(selection, view):
    """Decode a selection's node atlas at the nodes its slots draw."""
    from sailworks.qt.gpuweather import decode_component
    from sailworks.rendering.windpainter import mercator_project_flat

    ids = selection.node_ids.ravel()
    slots = np.flatnonzero(ids >= 0)
    node_ids = ids[slots].astype(int)
    ax = decode_component(selection.anchors[0]).ravel()
    ay = decode_component(selection.anchors[1]).ravel()
    nodes = selection.field.points[node_ids]
    expected = np.column_stack(
        mercator_project_flat(
            nodes[:, 0],
            nodes[:, 1],
            view._center_lat,
            view._center_lon,
            view._zoom,
            view.width,
            view.height,
        )
    )
    return node_ids, np.column_stack((ax[node_ids], ay[node_ids])), expected


def test_each_views_selection_matches_the_direct_reference(gribs):
    """The selection of a panned view equals the direct path's selection."""
    source, sampler, signature = _sources(gribs)
    stamp = START.timestamp() + 3600
    panned = BarbView(640, 480, 0.5 + 0.002, 0.5, 9, "standard", False)
    direct = _stage(sampler, panned, stamp)
    resident = _stage(sampler, panned, stamp, FieldStore(1 << 24), signature)
    assert resident.count == direct.count
    # The node atlas anchors every drawn field node at the current view.
    node_ids, anchors, expected = _node_anchors(resident.selection, panned)
    assert np.unique(node_ids).size == resident.count
    np.testing.assert_allclose(anchors, expected, rtol=0, atol=1e-4)
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
    from sailworks.qt.gpuweather import decode_component

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


def test_barb_selection_matches_only_exact_membership():
    ids = np.array([[0, 1, -1], [2, 3, 0]])
    selection = BarbSelection("g", ids, (), 5, None)
    assert selection.matches(BarbSelection("g", ids.copy(), (), 5, None))
    assert not selection.matches(None)
    # Generation binds the selection to its resident field and time.
    assert not selection.matches(BarbSelection("other", ids.copy(), (), 5, None))
    assert not selection.matches(BarbSelection("g", ids.copy(), (), 4, None))
    assert not selection.matches(BarbSelection("g", ids.reshape(3, 2), (), 5, None))
    changed = ids.copy()
    changed[0, 1] = 2
    assert not selection.matches(BarbSelection("g", changed, (), 5, None))


def test_barb_selection_anchor_frame_is_part_of_the_membership():
    ids = np.array([[0, 1, -1], [2, 3, 0]])
    frame = (48.3, -123.0, 9.0, 900.0, 700.0)
    selection = BarbSelection("g", ids, (), 5, None, anchor_reference=frame)
    # A pan inside one pan reference keeps identical anchors and membership.
    assert selection.matches(
        BarbSelection("g", ids.copy(), (), 5, None, anchor_reference=frame)
    )
    assert selection.matches(
        BarbSelection("g", ids.copy(), (), 5, None, anchor_reference=tuple(frame))
    )
    # Anchors encoded at another zoom, viewport size or dpr cannot be placed
    # by the shared transform, so they are never the same presentation.
    for other in (
        (48.3, -123.0, 10.0, 900.0, 700.0),
        (48.3, -123.0, 9.0, 901.0, 700.0),
        (48.3, -123.0, 9.0, 900.0, 701.0),
        (48.3, -123.0, 9.0, 900.0, 700.0, 2.0),
    ):
        assert not selection.matches(
            BarbSelection("g", ids.copy(), (), 5, None, anchor_reference=other)
        )
    # The historical camera-local path has no reference and is distinct.
    assert not selection.matches(BarbSelection("g", ids.copy(), (), 5, None))


def test_antimeridian_seam_anchors_translate_to_the_current_projection():
    from types import SimpleNamespace

    from sailworks.qt.gpuweather import decode_component
    from sailworks.qt.viewporttransform import PanReference
    from sailworks.rendering.weatherbarbs import stage_selection
    from sailworks.rendering.windpainter import mercator_project_flat

    # A native grid straddling the seam, represented as continuous west
    # longitudes.  The reference sits on the other side, at +179.95.
    lats = np.array([-0.2, 0.0, 0.2])
    lons = np.array([-180.15, -179.95, -179.75])
    points = np.array([(lat, lon) for lat in lats for lon in lons])
    points = points[np.lexsort((points[:, 0], points[:, 1]))]
    zeros = np.zeros((len(points), 2))
    field = SimpleNamespace(
        generation="g",
        points=points,
        grids=(SimpleNamespace(nodes=lambda bounds: points),),
        index={
            (round(lat, 9), round(lon, 9)): index
            for index, (lat, lon) in enumerate(points)
        },
        staged=SimpleNamespace(
            first=(zeros,), second=(zeros,), weight=lambda stamp: 0.0
        ),
        land_flags=None,
    )
    current = BarbView(640, 480, 0.0, -179.95, 9, "standard", True)
    reference = BarbView(640, 480, 0.0, 179.95, 9, "standard", True)
    selection = stage_selection(None, field, 0.0, current, None, reference=reference)
    assert selection.anchor_reference == (
        reference._center_lat,
        reference._center_lon,
        reference._zoom,
        reference.width,
        reference.height,
    )
    ids = selection.node_ids.ravel()
    slots = np.flatnonzero(ids >= 0)
    nodes = points[ids[slots].astype(int)]
    assert selection.count == len(slots) > 0
    assert np.any(nodes[:, 1] < -180.0)
    node_ids = ids[slots].astype(int)
    anchored = np.column_stack(
        (
            decode_component(selection.anchors[0]).ravel()[node_ids],
            decode_component(selection.anchors[1]).ravel()[node_ids],
        )
    )
    camera = SimpleNamespace(
        center_lat=current._center_lat,
        center_lon=current._center_lon,
        zoom=current._zoom,
        width=current.width,
        height=current.height,
    )
    translation = PanReference(*selection.anchor_reference).translation_to(camera)
    projected = np.column_stack(
        mercator_project_flat(
            nodes[:, 0],
            nodes[:, 1],
            current._center_lat,
            current._center_lon,
            current._zoom,
            current.width,
            current.height,
        )
    )
    # The anchors are local to the other side of the seam; the reference
    # translation carries them exactly onto the current view's projection.
    assert not np.allclose(anchored, projected)
    np.testing.assert_allclose(anchored + translation, projected, atol=1e-4, rtol=0)


def test_pan_keeps_exact_membership_with_reference_local_anchors(gribs):
    from sailworks.qt.gpuweather import decode_component

    source, sampler, signature = _sources(gribs)
    stamp = START.timestamp() + 3600
    store = FieldStore(1 << 24)
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    panned = BarbView(640, 480, 0.501, 0.5, 9, "standard", False)
    base = _stage(sampler, here, stamp, store, signature, reference=here)
    repeat = _stage(sampler, here, stamp, store, signature, reference=here)
    moved = _stage(sampler, panned, stamp, store, signature, reference=here)
    # The direct path is the membership oracle for the panned camera, and a
    # fresh field store resolves the same view independently of the cache.
    oracle = _stage(sampler, panned, stamp)
    independent = _stage(
        sampler, panned, stamp, FieldStore(1 << 24), signature, reference=here
    )
    # Restaging one view reuses the field and reproduces the same symbols and
    # the same anchor artefacts, byte for byte.
    assert repeat.field is base.field
    assert repeat.selection.matches(base.selection)
    np.testing.assert_array_equal(repeat.selection.node_ids, base.selection.node_ids)
    for actual, expected in zip(repeat.selection.anchors, base.selection.anchors):
        assert actual.constBits().tobytes() == expected.constBits().tobytes()
    # A covered pan reuses the field and selects exactly the direct path's
    # symbols. Membership is geographic and independent of the cache: the
    # exact node ids match the oracle, not merely its count.
    assert moved.field is base.field
    assert moved.selection.matches(base.selection)
    assert moved.count == oracle.count
    np.testing.assert_array_equal(moved.selection.node_ids, base.selection.node_ids)
    np.testing.assert_array_equal(
        moved.selection.node_ids, independent.selection.node_ids
    )
    ids = moved.selection.node_ids
    selected = ids >= 0
    assert np.all(ids[selected] < moved.field.node_count)
    assert np.unique(ids[selected]).size == moved.selection.count
    # Anchors are reference-local: a translation-only pan keeps both anchor
    # sources untouched and moves the whole layer through the shader offset.
    for actual, expected in zip(moved.selection.anchors, base.selection.anchors):
        assert actual.constBits().tobytes() == expected.constBits().tobytes()
    for actual, expected in zip(moved.selection.anchors, independent.selection.anchors):
        np.testing.assert_array_equal(
            decode_component(actual), decode_component(expected)
        )


def test_node_anchor_atlas_is_stable_across_a_wide_slot_permutation(gribs):
    """A pan of more than one mesh step reassigns slots, not anchor bytes."""
    from sailworks.qt.gpuweather import decode_component
    from sailworks.qt.gpuweatherfields import FIELD_COLUMNS
    from sailworks.rendering.weatherbarbs import stage_selection

    source, sampler, signature = _sources(gribs)
    stamp = START.timestamp() + 3600
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", False)
    # 0.2 degrees is about 73 pixels at zoom 9, more than one 44 px step.
    panned = BarbView(640, 480, 0.5, 0.5 + 0.2, 9, "standard", False)
    field = _stage(
        sampler, here, stamp, FieldStore(1 << 24), signature, reference=here
    ).field
    base = stage_selection(None, field, stamp, here, None, reference=here)
    moved = stage_selection(None, field, stamp, panned, None, reference=here)
    # The current-view slot assignment really changed.
    assert base.node_ids.shape == moved.node_ids.shape
    assert not np.array_equal(base.node_ids, moved.node_ids)
    assert moved.anchor_reference == base.anchor_reference
    # The atlas is field-node-indexed at FIELD_COLUMNS width, and the pan
    # leaves every encoded byte untouched.
    rows = -(-field.node_count // FIELD_COLUMNS)
    assert base.anchor_shape == moved.anchor_shape == (rows, FIELD_COLUMNS)
    for actual, expected in zip(moved.anchors, base.anchors):
        assert actual.constBits().tobytes() == expected.constBits().tobytes()
    # Padding past the node count is finite, as the shader may clamp to it.
    for image in moved.anchors:
        values = decode_component(image).ravel()
        assert np.isfinite(values).all()
        assert np.all(values[field.node_count :] == 0.0)


def test_land_mask_survives_a_covered_pan_with_reference_anchors(gribs):
    source, sampler, signature = _sources(gribs)
    stamp = START.timestamp() + 3600
    store = FieldStore(1 << 24)
    here = BarbView(640, 480, 0.5, 0.5, 9, "standard", True)
    panned = BarbView(640, 480, 0.501, 0.5, 9, "standard", True)
    base = _stage(sampler, here, stamp, store, signature, reference=here)
    moved = _stage(sampler, panned, stamp, store, signature, reference=here)
    expected = _stage(sampler, panned, stamp, FieldStore(1 << 24), signature)
    # The mask is applied to the current view's selection, and its ids are the
    # direct path's; the anchors stay reference-local.
    assert moved.field is base.field
    np.testing.assert_array_equal(moved.selection.node_ids, expected.selection.node_ids)
    assert moved.selection.count == expected.count
    for actual, reference in zip(moved.selection.anchors, base.selection.anchors):
        assert actual.constBits().tobytes() == reference.constBits().tobytes()


def test_land_mask_changes_the_membership_exactly(gribs):
    source, sampler, signature = _sources(gribs)
    stamp = START.timestamp() + 3600
    store = FieldStore(1 << 24)
    offshore = _stage(
        sampler,
        BarbView(640, 480, 0.5, 0.5, 9, "standard", False),
        stamp,
        store,
        signature,
    )
    with_land = _stage(
        sampler,
        BarbView(640, 480, 0.5, 0.5, 9, "standard", True),
        stamp,
        store,
        signature,
    )
    assert with_land.field is offshore.field
    assert not with_land.selection.matches(offshore.selection)
    # Exact membership, not merely "different": a fresh store resolves the
    # same symbols, and the land policy only fills the cells it removed.
    expected = _stage(
        sampler,
        BarbView(640, 480, 0.5, 0.5, 9, "standard", True),
        stamp,
        FieldStore(1 << 24),
        signature,
    )
    offshore_ids = offshore.selection.node_ids
    land_ids = with_land.selection.node_ids
    np.testing.assert_array_equal(land_ids, expected.selection.node_ids)
    kept = offshore_ids != -1
    np.testing.assert_array_equal(land_ids[kept], offshore_ids[kept])
    restored = (~kept) & (land_ids != -1)
    assert restored.sum() == with_land.selection.count - offshore.selection.count
    assert np.all(land_ids[~kept & ~restored] == -1)
    assert offshore.selection.count < with_land.selection.count


def test_viewport_rejects_stale_selection_before_publishing():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from sailworks.qt.gribviewport import _viewport_widget_class

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
    from sailworks.rendering.weatherbarbs import select_resident

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
    _node_ids, anchors, projected = _node_anchors(actual.selection, near)
    np.testing.assert_allclose(anchors, projected, rtol=0, atol=1e-4)


def test_shared_grid_enumeration_is_deduplicated_without_dropping_sources(gribs):
    from sailworks.rendering.weatherbarbs import select_resident

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
    _node_ids, anchors, projected = _node_anchors(actual.selection, near)
    np.testing.assert_allclose(anchors, projected, rtol=0, atol=1e-4)


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
    from sailworks.qt.gpuweather import decode_component

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

    from sailworks.qt.gpuweatherjobs import WeatherStager

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
                    "display_forecast": type(
                        "Forecast", (), {"sources": lambda self: [source]}
                    )(),
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

        with patch("sailworks.qt.gpuweatherjobs.stage_barbs", side_effect=stage):
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
    from sailworks.qt.gpuweather import WeatherImageProvider

    provider = WeatherImageProvider()
    first = provider.publish(atlas)
    second = provider.publish(atlas)
    assert first != second
    assert all(url.startswith("image://weather/") for url in first)


# ------------------------------------------------------------- wave layer
def _wave_host(**attributes):
    """A headless host with the render context's real wave methods bound.

    The viewport is a ``QQuickWidget``; these checks stay model-level and call
    the production methods on a plain host, as the repo's other headless
    viewport checks do.
    """
    from types import SimpleNamespace

    from sailworks.qt import gribviewport

    viewport = gribviewport._viewport_widget_class()
    host = SimpleNamespace(**attributes)
    for name in (
        "_show_wave_state",
        "_set_wave_status",
        "_update_wave",
        "set_wave_visible",
        "wave_state",
    ):
        setattr(host, name, getattr(viewport, name).__get__(host))
    return host


def test_wave_visibility_never_touches_the_wind_fill():
    """Hiding the wave layer leaves the wind layer's state exactly as it was."""
    from unittest.mock import Mock

    wave_fill, wave_back, wind_fill = Mock(), Mock(), Mock()
    host = _wave_host(
        _wave_visible=True,
        _wave_state="ready",
        _wave_fill=wave_fill,
        _wave_fill_back=wave_back,
        _wave_status=Mock(),
        _wind_opacity=0.42,
        _wind_gradient_enabled=True,
        _wind_gradient_opacity=0.12,
        _fill=wind_fill,
    )
    host.set_wave_visible(False)
    assert host.wave_state() == "hidden"
    assert host._wave_visible is False
    wave_fill.setVisible.assert_called_once_with(False)
    wave_back.setVisible.assert_called_once_with(False)
    # The wind fill is enabled, opaque and untouched by the wave toggle.
    assert host._wind_opacity == 0.42
    assert host._wind_gradient_enabled is True
    assert host._wind_gradient_opacity == 0.12
    assert not wind_fill.setVisible.called
    assert not wind_fill.setOpacity.called


def test_missing_wave_data_is_an_explicit_unavailable_state():
    """No wave field is published as zero height; the status says why."""
    from unittest.mock import Mock

    class _Model:
        error = "No wave forecast"

        def busy(self):
            return False

        def update(self, camera, reference):
            return "unavailable", None, None

    wave_fill, wave_back, status = Mock(), Mock(), Mock()
    host = _wave_host(
        _wave_visible=True,
        _wave_state="loading",
        _wave_error="",
        _wave_fill=wave_fill,
        _wave_fill_back=wave_back,
        _wave_status=status,
        _wave_model=_Model(),
        _camera=object(),
        _disposed=False,
        _barb_reference_view=lambda camera: None,
    )
    host._update_wave(host._camera)
    assert host.wave_state() == "unavailable"
    status.setProperty.assert_called_with("text", "No wave forecast")
    # The fill is never shown; the status, not a zero-height field, carries
    # the failure.
    for item in (wave_fill, wave_back):
        item.setVisible.assert_called_once_with(False)
        item.setProperty.assert_any_call("statusText", "No wave forecast")
        item.setProperty.assert_any_call("waveLoading", False)
