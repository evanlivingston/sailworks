"""The independent GRIB viewport: boundary, coverage, reuse and close.

These checks are model-level and headless: they do not need a GPU, a window
or the application's frame pipeline.
"""

from datetime import timedelta
import inspect
from pathlib import Path

import pytest

from gweatherrouting.qt import gribviewport
from gweatherrouting.qt.gribviewport import GribViewportModel, ViewportCamera
from test.test_weather_barbs import Land
from test.test_weather_staging import gribs, START  # noqa: F401

FORBIDDEN = (
    "Core",
    "SessionController",
    "TrackMap",
    "MapFrame",
    "MapScene",
    "WeatherStager",
)


def _paths(gribs):
    source = gribs.file(gribs.field(0) + gribs.field(3), end=3)
    return (source.path,), source


def _camera(**overrides):
    values = dict(
        center_lat=0.5,
        center_lon=0.5,
        zoom=9,
        width=640,
        height=480,
        time=START + timedelta(hours=1),
    )
    values.update(overrides)
    return ViewportCamera(**values)


def _wait_ready(model, timeout=20.0, camera=None):
    import time as clock

    deadline = clock.monotonic() + timeout
    while clock.monotonic() < deadline:
        state, field, selection = model.update(camera or _camera())
        if state == "ready":
            return field, selection
        if model.state == "unavailable":
            raise AssertionError(model.error)
        clock.sleep(0.02)
    raise AssertionError("the viewport never prepared a field")


# ------------------------------------------------------------- boundary
def test_the_viewport_does_not_use_the_application_frame_pipeline():
    """No application frame class is referenced, and no tools/ module is imported."""
    import ast

    tree = ast.parse(inspect.getsource(gribviewport))
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    for name in FORBIDDEN:
        assert name not in referenced, name
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert not any(name.startswith("tools") for name in imported), imported
    for name in imported:
        assert not name.startswith("gweatherrouting.core.gribmanager"), name


def test_the_viewport_owns_its_camera_and_time_state():
    camera = _camera()
    assert camera.view()._center_lat == camera.center_lat
    assert camera.stamp() == camera.time.timestamp()
    other = _camera(center_lon=0.7)
    assert other.view()._center_lon == 0.7


# -------------------------------------------------------------- coverage
def test_a_covered_camera_move_reuses_the_resident_field(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        field, selection = _wait_ready(model)
        assert field is not None and selection.count > 0
        misses = model.counters["misses"]
        # A covered move inside the field's window: the same field, a new
        # selection, and no further preparation.
        moved = _camera(center_lon=0.5005)
        state, same, next_selection = model.update(moved)
        assert state == "ready"
        assert same is field
        assert next_selection is not selection
        # Background prefetch may prepare another time, but this demand does
        # not miss or wait for it.
        assert model.counters["misses"] == misses
        assert model.counters["covered_reuses"] >= 1
    finally:
        model.close()


def test_a_coverage_miss_is_never_shown_as_current(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        _wait_ready(model)
        # Far outside the resident window: the state is loading, never the
        # previous field's content.
        state, field, selection = model.update(_camera(center_lon=3.5))
        assert state == "loading"
        assert field is None and selection is None
        assert model.counters["misses"] >= 1
    finally:
        model.close()


def test_a_time_outside_the_resident_field_is_not_current(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        field, _selection = _wait_ready(model)
        later = _camera(time=START + timedelta(hours=4))
        state, published, _selection = model.update(later)
        assert state == "loading"
        assert published is None
    finally:
        model.close()


# ------------------------------------------------------------- bindings
def test_a_selection_of_another_field_is_rejected(gribs):
    from gweatherrouting.qt.gpuweatherfields import validate_binding

    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        field, selection = _wait_ready(model)
        assert validate_binding(field, selection) is selection
        stale = selection.__class__(
            generation="other-generation",
            node_ids=selection.node_ids,
            anchors=selection.anchors,
            count=selection.count,
            field=field,
            node_image=selection.node_image,
        )
        with pytest.raises(ValueError, match="Stale"):
            validate_binding(field, stale)
    finally:
        model.close()


# ------------------------------------------------------- cancel and close
def test_close_releases_a_pending_preparation(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    model.update(_camera())
    model.close()
    assert model.state in ("loading", "unavailable")
    assert model.counters["cancelled"] >= 1
    # A closed model never presents anything.
    state, field, selection = model.update(_camera())
    assert state == "unavailable"
    assert field is None and selection is None


def test_a_new_time_replaces_the_pending_preparation(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        model.update(_camera())
        model.update(_camera(time=START + timedelta(hours=2)))
        assert model.counters["cancelled"] >= 1
    finally:
        model.close()


def test_the_viewport_module_lives_beside_the_qt_host():
    assert Path(gribviewport.__file__).parent.name == "qt"


def test_saved_selection_preserves_order_and_style_without_writing(tmp_path):
    import json

    (tmp_path / "grib").mkdir()
    for name in ("second.grib2", "first.grib2"):
        (tmp_path / "grib" / name).touch()
    manager = tmp_path / "grib-manager.json"
    manager.write_text(json.dumps({"opened": ["second.grib2", "first.grib2"]}))
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"gribBarbDensity": "very_dense", "gribArrowOnGround": False})
    )
    before = {p: p.read_bytes() for p in (manager, settings)}
    paths, style = gribviewport.saved_selection(tmp_path)
    assert [Path(p).name for p in paths] == ["second.grib2", "first.grib2"]
    assert style == {"gribBarbDensity": "very_dense", "gribArrowOnGround": False}
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize("opened", [[], ["missing.grib2"], ["../outside.grib2"]])
def test_saved_selection_never_silently_prunes_unavailable_sources(tmp_path, opened):
    import json

    (tmp_path / "grib-manager.json").write_text(json.dumps({"opened": opened}))
    with pytest.raises(ValueError):
        gribviewport.saved_selection(tmp_path)


def test_existing_app_cache_still_returns_an_exact_view_atlas(gribs):
    from gweatherrouting.qt.gpuweatherfields import FieldStore
    from gweatherrouting.rendering.weatherbarbs import stage_barbs
    from gweatherrouting.core.gribsampling import WeatherSampler

    paths, source = _paths(gribs)
    sampler = WeatherSampler([source])
    store = FieldStore(1 << 24)
    camera = _camera()
    first = stage_barbs(
        sampler, camera.stamp(), camera.view(), Land(), store=store, source=paths
    )
    second = stage_barbs(
        sampler, camera.stamp() + 60, camera.view(), Land(), store=store, source=paths
    )
    assert second is first
    assert first.field is None and first.selection is None
    assert first.pair.shape == first.staged.first.shape[:2]
    assert len(first.images) == 6


def test_coverage_requires_the_entire_view_not_just_the_camera_center(gribs):
    from dataclasses import replace

    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        field, _selection = _wait_ready(model)
        assert gribviewport._covers(field, _camera())
        assert not gribviewport._covers(field, replace(_camera(), width=4000))
        assert not gribviewport._covers(field, replace(_camera(), zoom=8))
    finally:
        model.close()


def test_close_never_waits_for_active_preparation(gribs, monkeypatch):
    from threading import Event
    import time

    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    entered, release = Event(), Event()

    def blocked(camera, cancel):
        entered.set()
        release.wait(5)
        return None

    monkeypatch.setattr(model, "_prepare", blocked)
    try:
        model.update(_camera())
        assert entered.wait(5)
        began = time.monotonic()
        model.close()
        assert time.monotonic() - began < 0.5
        assert model._active[1].is_set()
        assert model.state == "unavailable"
    finally:
        release.set()
        model.close()
        if model._thread is not None:
            model._thread.join(5)
            assert not model._thread.is_alive()


def test_cancelled_preparation_never_replaces_current_demand(gribs, monkeypatch):
    from threading import Event
    import time

    paths, _source = _paths(gribs)
    reference = GribViewportModel(paths, land_mask=Land())
    try:
        old_field, _selection = _wait_ready(reference)
    finally:
        reference.close()
    model = GribViewportModel(paths, land_mask=Land())
    entered, release = Event(), Event()

    def blocked(camera, cancel):
        entered.set()
        release.wait(5)
        return old_field

    monkeypatch.setattr(model, "_prepare", blocked)
    try:
        model.update(_camera())
        assert entered.wait(5)
        model.update(_camera(time=START + timedelta(hours=4)))
        assert model._active[1].is_set()
        release.set()
        deadline = time.monotonic() + 5
        while model.counters["stale_rejected"] == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert model.counters["stale_rejected"] > 0
        assert model._field is None
    finally:
        release.set()
        model.close()
        if model._thread is not None:
            model._thread.join(5)


def test_zoom_reuses_values_only_with_the_same_shoreline_policy(gribs):
    from dataclasses import replace
    import numpy as np
    from gweatherrouting.qt.gpuweather import decode_component
    from gweatherrouting.core.gribsampling import WeatherSampler
    from gweatherrouting.rendering.weatherbarbs import stage_barbs

    paths, source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    camera = _camera(over_land=False)
    try:
        field, _selection = _wait_ready(model, camera=camera)
        zoomed = replace(camera, zoom=camera.zoom + 0.2)
        state, same, selection = model.update(zoomed)
        assert state == "ready" and same is field
        direct = stage_barbs(
            WeatherSampler([source]), zoomed.stamp(), zoomed.view(), Land()
        )
        assert selection.count == direct.count
        for actual, expected in zip(selection.anchors, direct.anchors):
            np.testing.assert_array_equal(
                decode_component(actual), decode_component(expected)
            )
        # This zoom is geographically covered, but it crosses the shoreline
        # policy boundary (intermediate -> high). Reusing its mask is forbidden.
        assert not gribviewport._covers(field, replace(camera, zoom=camera.zoom + 1))
    finally:
        model.close()


def test_exact_future_event_is_prefetched_without_blending_across_it(gribs):
    import time
    import numpy as np
    from gweatherrouting.qt.gpuweather import decode_component
    from gweatherrouting.core.gribsampling import WeatherSampler
    from gweatherrouting.rendering.weatherbarbs import stage_barbs

    paths, source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    future = _camera(time=START + timedelta(hours=3))
    try:
        _wait_ready(model)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with model._condition:
                warmed = any(
                    gribviewport._covers(field, future)
                    for field in model._store.fields()
                )
            if warmed:
                break
            time.sleep(0.005)
        assert warmed, "Future exact event was not prepared in the background"
        misses = model.counters["misses"]
        state, field, selection = model.update(future)
        assert state == "ready"
        assert field.identity.before == field.identity.after == future.stamp()
        assert model.counters["misses"] == misses
        direct = stage_barbs(
            WeatherSampler([source]), future.stamp(), future.view(), Land()
        )
        assert selection.count == direct.count
        for actual, expected in zip(selection.anchors, direct.anchors):
            np.testing.assert_array_equal(
                decode_component(actual), decode_component(expected)
            )
        ids = selection.node_ids.ravel()
        used = ids >= 0
        np.testing.assert_array_equal(
            field.staged.first[0, ids[used].astype(int)],
            direct.staged.first.reshape(-1, 2)[used],
        )
    finally:
        model.close()


def test_speculative_store_cannot_evict_the_visible_field():
    from types import SimpleNamespace
    from gweatherrouting.qt.gpuweatherfields import FieldStore

    store = FieldStore(100)
    current = SimpleNamespace(identity="visible", nbytes=70)
    future = SimpleNamespace(identity="speculation", nbytes=70)
    assert store.store(current)
    assert not store.store(future, protect=False)
    assert store.acquire("visible") is current
    assert store.current_bytes == 70


@pytest.mark.parametrize(
    "scale,quality",
    [
        (5001, "c"),
        (5000, "l"),
        (501, "l"),
        (500, "i"),
        (201, "i"),
        (200, "h"),
        (15, "h"),
        (14.99, "f"),
    ],
)
def test_zoom_mask_reuse_keeps_the_existing_scale_boundaries(scale, quality):
    from gweatherrouting.rendering.windpainter import shoreline_quality

    assert shoreline_quality(scale) == quality
