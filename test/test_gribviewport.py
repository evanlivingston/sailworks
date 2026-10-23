"""The independent GRIB viewport: boundary, coverage, reuse and close.

These checks are model-level and headless: they do not need a GPU, a window
or the application's frame pipeline.
"""

from dataclasses import replace
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


@pytest.mark.parametrize("over_land", [True, False])
def test_all_points_selects_every_native_node_at_current_time(gribs, over_land):
    import numpy as np
    from gweatherrouting.core.gribsampling import WeatherSampler
    from gweatherrouting.qt.gpuweather import decode_component
    from gweatherrouting.rendering.weatherbarbs import _view_bounds

    fields = gribs.field(0, size=101) + gribs.field(3, size=101)
    fields = [
        (element, hour, values / 100 if element in ("UGRD", "VGRD") else values)
        for element, hour, values in fields
    ]
    source = gribs.file(
        fields,
        transform=(-0.005, 0.01, 0, -0.005, 0, 0.01),
        end=3,
    )
    model = GribViewportModel((source.path,), land_mask=Land())
    sampler = WeatherSampler([source])
    camera = _camera(density="all", over_land=over_land)
    try:
        for hour in (1, 2, 3):
            current = replace(camera, time=START + timedelta(hours=hour))
            field, selection = _wait_ready(model, camera=current)
            points = source.sampling.nodes(_view_bounds(current.view()))
            uv = sampler.sample_timestamps(
                "wind", current.stamp(), points[:, 0], points[:, 1]
            )
            expected = points[np.isfinite(uv).all(axis=1)]
            if not over_land:
                expected = expected[expected[:, 0] <= 0.5]
            ids = selection.node_ids.ravel()
            ids = ids[ids >= 0].astype(int)
            actual = field.points[ids]
            assert selection.count == len(expected)
            assert len(np.unique(ids)) == len(expected)
            if over_land:
                assert selection.count > 8192
            order = lambda a: a[np.lexsort((a[:, 0], a[:, 1]))]
            np.testing.assert_allclose(
                order(actual), order(expected), rtol=0, atol=1e-9
            )
            values = field.staged.first[0, ids] + (
                field.staged.second[0, ids] - field.staged.first[0, ids]
            ) * field.staged.weight(current.stamp())
            reference = sampler.sample_timestamps(
                "wind", current.stamp(), actual[:, 0], actual[:, 1]
            )
            # GPU staging uses float32 (relative rounding up to ~7e-8).
            # At zero, equivalent interpolation orders also leave float64
            # roundoff (~1e-18 m/s); neither difference is a missing node.
            np.testing.assert_allclose(values, reference, rtol=1e-7, atol=1e-14)
            np.testing.assert_array_equal(
                decode_component(selection.node_image).ravel(),
                selection.node_ids.ravel(),
            )
        # Density is portrayal only: returning from thinned mode loses no nodes.
        assert model.update(replace(current, density="standard"))[0] == "ready"
        state, same, restored = model.update(current)
        assert state == "ready" and same is field
        np.testing.assert_array_equal(restored.node_ids, selection.node_ids)
    finally:
        model.close()


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


def test_a_new_time_does_not_cancel_the_active_preparation(gribs, monkeypatch):
    from threading import Event

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
        model.update(_camera(time=START + timedelta(hours=2)))
        # A time-window change queues the new demand; it does not abort the
        # active preparation, so a 30 Hz sweep cannot starve the one worker.
        assert not model._active[1].is_set()
        assert model._pending is not None
    finally:
        release.set()
        model.close()
        if model._thread is not None:
            model._thread.join(5)


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
        # Explicit cancellation, not a time change, is what forbids publication.
        with model._condition:
            model._active[1].set()
        release.set()
        deadline = time.monotonic() + 5
        while model.counters["stale_rejected"] == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert model.counters["stale_rejected"] > 0
        observations = model.observations()
        assert (
            gribviewport._generation_key(old_field)
            in observations["rejected_generations"]
        )
        assert old_field.generation not in observations["published_generations"]
        assert model._field is None
    finally:
        release.set()
        model.close()
        if model._thread is not None:
            model._thread.join(5)


def test_a_prepared_back_slot_is_handed_forward_while_the_front_is_presented(
    gribs, monkeypatch
):
    from threading import Event
    import time

    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    entered, release = Event(), Event()
    real_prepare = model._prepare

    def blocked(camera, cancel):
        entered.set()
        release.wait(5)
        return real_prepare(camera, cancel)

    monkeypatch.setattr(model, "_prepare", blocked)
    try:
        front, _selection = _wait_ready(model)
        # A demand outside the front window starts the worker on the back slot
        # while the front is still presentable.
        moved = _camera(center_lon=1.2)
        assert model.update(moved)[0] == "loading"
        assert entered.wait(5)
        state, presented, _selection = model.update(_camera())
        assert state == "ready" and presented is front
        with model._condition:
            assert model.observations()["slots_filled"] == 1
        release.set()
        moved_field, _selection = _wait_ready(model, camera=moved, timeout=10)
        assert moved_field is not front
        observations = model.observations()
        assert observations["slot_handoffs"] >= 1
        assert observations["slots_filled"] >= 1
        assert front.generation in observations["published_generations"]
    finally:
        release.set()
        model.close()
        if model._thread is not None:
            model._thread.join(5)


def test_a_cancelled_request_never_publishes(gribs, monkeypatch):
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
        # Explicit cancellation, not a time change, forbids publication.
        with model._condition:
            model._active[1].set()
        release.set()
        deadline = time.monotonic() + 5
        while model.counters["stale_rejected"] == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        observations = model.observations()
        assert observations["superseded_rejected"] >= 1
        assert old_field.generation not in observations["published_generations"]
        assert model._field is None
    finally:
        release.set()
        model.close()
        if model._thread is not None:
            model._thread.join(5)


def test_a_superseded_completed_field_is_retained_not_published(gribs, monkeypatch):
    from threading import Event
    import time

    paths, _source = _paths(gribs)
    reference = GribViewportModel(paths, land_mask=Land())
    try:
        old_field, _selection = _wait_ready(reference)
    finally:
        reference.close()
    model = GribViewportModel(paths, land_mask=Land())
    real_prepare = model._prepare
    entered, release = Event(), Event()

    def delayed(camera, cancel):
        entered.set()
        release.wait(5)
        return old_field

    monkeypatch.setattr(model, "_prepare", delayed)
    try:
        model.update(_camera())
        assert entered.wait(5)
        # A newer demand replaces the active request; the older field is valid
        # but must not be published as current.  It is retained for later.
        far = _camera(center_lon=3.5)
        assert model.update(far)[0] == "loading"
        release.set()
        deadline = time.monotonic() + 5
        while (
            model.counters["superseded_retained"] == 0 and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert model.counters["superseded_retained"] >= 1
        observations = model.observations()
        assert old_field.generation not in observations["published_generations"]
        assert (
            gribviewport._generation_key(old_field)
            not in observations["rejected_generations"]
        )
        assert model._field is None
        # Stop the replaced demand; the retained field still serves its own
        # window when the camera returns.
        model._prepare = real_prepare
        with model._condition:
            for request in (model._pending, model._active):
                if request is not None:
                    request[1].set()
            model._pending = None
        state, field, _selection = model.update(_camera())
        assert state == "ready" and field is old_field
    finally:
        release.set()
        model.close()
        if model._thread is not None:
            model._thread.join(5)


def test_a_generation_mismatch_is_rejected_not_presented(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        _field, _selection = _wait_ready(model)
        with model._condition:
            resource = next(item for item in model._slots if item is not None)
            stale = replace(resource, generation=("stale",), slot=0)
            model._store.clear()
            model._field = None
            model._slots = [stale, None]
            model._front_index = 0
            before = model.counters["generation_rejected"]
        state, presented, selection = model.update(_camera())
        assert state == "loading"
        assert presented is None and selection is None
        observations = model.observations()
        assert model.counters["generation_rejected"] > before
        assert observations["generation_rejected"] >= 1
        assert stale.generation not in observations["published_generations"]
    finally:
        model.close()


def test_the_speculative_prefetcher_is_retired():
    assert not hasattr(GribViewportModel, "_prefetch_request")
    model = object.__new__(GribViewportModel)
    assert not hasattr(model, "_prefetch_attempts")
    assert not hasattr(model, "_speculative")


def test_recentre_camera_advances_one_lattice_in_the_direction_of_motion():
    from gweatherrouting.rendering.weatherbarbs import FIELD_LATTICE_PX

    camera = _camera()
    moved = replace(camera, center_lon=camera.center_lon + 0.01)
    target = GribViewportModel._recentre_camera(
        object.__new__(GribViewportModel), moved, camera
    )
    view = moved.view()
    assert view._lon_to_x(target.center_lon) - view._lon_to_x(
        moved.center_lon
    ) == pytest.approx(FIELD_LATTICE_PX)
    assert target.center_lat == moved.center_lat
    # A stationary camera is not re-centred.
    assert (
        GribViewportModel._recentre_camera(
            object.__new__(GribViewportModel), camera, camera
        )
        is None
    )


def test_recentre_if_needed_queues_one_window_only_when_the_guard_fails(monkeypatch):
    from threading import Condition, Event

    model = object.__new__(GribViewportModel)
    model._condition = Condition()
    model._active = None
    model._pending = None
    model._resident_window = False
    model.counters = {"proactive_recentres": 0, "time_lookaheads": 0}
    model._events = ()
    queued = []
    monkeypatch.setattr(model, "_request", lambda camera: queued.append(camera))
    monkeypatch.setattr(
        model, "_request_key", lambda camera: ("key", camera.center_lon)
    )

    class FakeIdentity:
        window = ((-100.0, -100.0), (100.0, 100.0))
        before = after = 0.0

    class FakeField:
        identity = FakeIdentity()

    camera = _camera()
    previous = replace(camera, center_lon=camera.center_lon - 0.01)
    monkeypatch.setattr(gribviewport, "_covers", lambda field, cam: True)
    model._recentre_if_needed(camera, previous, FakeField())
    assert queued == []
    monkeypatch.setattr(gribviewport, "_covers", lambda field, cam: False)
    model._recentre_if_needed(camera, previous, FakeField())
    assert len(queued) == 1 and model.counters["proactive_recentres"] == 1
    # A preparation already in flight is not replaced by another re-centre.
    model._pending = (camera, Event(), "in-flight")
    model._recentre_if_needed(camera, previous, FakeField())
    assert len(queued) == 1
    # A stationary camera queues nothing.
    model._pending = None
    model._recentre_if_needed(camera, camera, FakeField())
    assert len(queued) == 1


def test_a_stationary_ready_field_prepares_one_time_lookahead(gribs, monkeypatch):
    import time as clock

    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    camera = _camera(time=START)
    real_prepare = model._prepare
    seen = []

    def observed(camera_, cancel):
        seen.append(camera_)
        return real_prepare(camera_, cancel)

    monkeypatch.setattr(model, "_prepare", observed)
    try:
        field, _selection = _wait_ready(model, camera=camera)
        expected = model._next_time_camera(camera, field)
        assert expected is not None
        deadline = clock.monotonic() + 10
        while len(seen) < 2 and clock.monotonic() < deadline:
            clock.sleep(0.01)
        assert len(seen) >= 2
        lookahead = seen[-1]
        # One deterministic N+1 step on the same spatial window, inside the
        # interval that follows the presented field's after boundary.
        assert lookahead.stamp() == pytest.approx(expected.stamp())
        assert (lookahead.center_lon, lookahead.center_lat, lookahead.zoom) == (
            camera.center_lon,
            camera.center_lat,
            camera.zoom,
        )
        assert model._time_key(lookahead.stamp()) != model._time_key(camera.stamp())
        assert model.counters["time_lookaheads"] == 1
    finally:
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


def test_exact_future_event_is_prepared_on_demand_without_blending_across_it(gribs):
    import numpy as np
    from gweatherrouting.qt.gpuweather import decode_component
    from gweatherrouting.core.gribsampling import WeatherSampler
    from gweatherrouting.rendering.weatherbarbs import stage_barbs

    paths, source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    future = _camera(time=START + timedelta(hours=3))
    try:
        _wait_ready(model)
        # #66 replaces speculative lookahead with demand-driven slots: the
        # future event is prepared when it is requested, not guessed in idle
        # time, and still keeps exact-instant (no blend) semantics.
        field, selection = _wait_ready(model, camera=future)
        assert field.identity.before == field.identity.after == future.stamp()
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


# ------------------------------------------------------ resident window
def _settle_window(model, members=1, timeout=20.0):
    import time as clock

    deadline = clock.monotonic() + timeout
    while clock.monotonic() < deadline:
        if model.counters["time_window_fields"] >= members:
            return True
        clock.sleep(0.01)
    return False


def test_the_resident_window_is_opt_in(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        assert model._resident_window is False
        # The default path keeps the single-step lookahead of the previous
        # issue and prepares no window member.
        _wait_ready(model, camera=_camera(time=START))
        assert model.counters["time_window_fields"] == 0
    finally:
        model.close()


def test_the_resident_window_holds_one_member_per_forecast_interval(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land(), resident_time_window=True)
    try:
        _wait_ready(model, camera=_camera(time=START))
        anchor = START.timestamp()
        members = model._time_members(anchor)
        # The anchor is served by a singleton, and every event instant by a
        # singleton of its own, with the bracket between two events beside it.
        assert members[0] == (anchor, anchor)
        assert all(before <= after for before, after in members)
        assert any(before < after for before, after in members)
        # Every instant of the horizon is served by one member of the window.
        for hour in range(4):
            stamp = anchor + hour * 3600
            assert model._time_key(stamp) in members
        # The horizon is bounded: it never reaches past the declared span.
        assert members[-1][1] <= anchor + 9 * 3600
    finally:
        model.close()


def test_a_forecast_time_change_inside_the_window_needs_no_preparation(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land(), resident_time_window=True)
    camera = _camera(time=START)
    try:
        field, _selection = _wait_ready(model, camera=camera)
        assert _settle_window(model, 1)
        preparations = model.counters["field_preparations"]
        later = replace(camera, time=START + timedelta(hours=1))
        state, other, selection = model.update(later)
        assert state == "ready"
        assert other is not field
        assert selection is not None
        # The resident window served the change: no preparation, no decode.
        assert model.counters["field_preparations"] == preparations
        assert model.counters["time_window_hits"] >= 1
    finally:
        model.close()


def test_the_window_fill_starts_only_where_the_field_was_prepared(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land(), resident_time_window=True)
    camera = _camera(time=START)
    try:
        field, _selection = _wait_ready(model, camera=camera)
        moved = replace(camera, center_lon=camera.center_lon + 0.5)
        with model._condition:
            model._pending = None
            model._active = None
            blocked = model.counters["time_window_blocked"]
            # A camera that left the prepared field does not claim the worker.
            assert model._queue_window_fill(moved, field) is False
            assert model.counters["time_window_blocked"] > blocked
            assert model._pending is None
            # The camera where the field was prepared is not blocked.
            blocked = model.counters["time_window_blocked"]
            model._window_started = False
            model._queue_window_fill(camera, field)
            assert model.counters["time_window_blocked"] == blocked
    finally:
        model.close()
        if model._thread is not None:
            model._thread.join(5)


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


# ------------------------------------------------------------ the fill layer
def _fill_scene():
    """A stand-in for the render context's scene, without a window."""
    from types import SimpleNamespace

    class Layer:
        def __init__(self):
            self.properties = {}
            self.visible = True

        def setProperty(self, name, value):
            self.properties[name] = value
            return True

    return SimpleNamespace(
        _native_weather_fields=False,
        _fill=Layer(),
        _barbs=Layer(),
        _provider=SimpleNamespace(
            publish_field=lambda images, key: tuple(f"u{index}" for index in range(4)),
            field_publications=0,
            selection_publications=0,
        ),
        _published=None,
        _published_fill=None,
        counters=dict(
            fill_publications=0,
            fill_uploads=0,
            fill_selection_publications=0,
            barbs_field_publications=0,
            barbs_field_uploads=0,
            barbs_selection_publications=0,
            barbs_selection_uploads=0,
        ),
    )


def _field():
    from types import SimpleNamespace

    from gweatherrouting.rendering.weathergrid import GridLattice

    lattice = GridLattice(
        origin_lon=0.0,
        origin_merc=0.0,
        spacing=0.25,
        cols=4,
        rows=4,
        window=((0.0, 0.0), (1.0, 1.0)),
    )
    return SimpleNamespace(
        generation="field-generation",
        images=("u0", "v0", "u1", "v1"),
        staged=SimpleNamespace(weight=lambda stamp: 0.25),
        grid=SimpleNamespace(
            images=("u0", "v0", "u1", "v1"),
            lattice=lattice,
        ),
    )


def test_the_fill_is_off_by_default():
    """The viewport's entry point keeps the numerical fill behind a flag."""
    import ast

    source = inspect.getsource(gribviewport)
    tree = ast.parse(source)
    flags = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "add_argument"
        ):
            for keyword in node.keywords:
                if (
                    keyword.arg == "action"
                    and getattr(keyword.value, "value", "") == "store_true"
                ):
                    flags.append(node.args[0].value)
    assert "--gpu-weather-fill" in flags
    assert "--resident-time-window" in flags
    # The flag reaches the render context, and the render context's default
    # is off: the default viewport path never loads the fill scene.
    widget = gribviewport._viewport_widget_class()
    defaults = {
        name: argument.default
        for name, argument in inspect.signature(widget.__init__).parameters.items()
        if argument.default is not inspect.Parameter.empty
    }
    assert defaults["gpu_weather_fill"] is False


def test_the_default_path_does_not_load_the_fill_scene():
    """The barbs' own component is the default root; the scene is opt-in."""
    import ast

    tree = ast.parse(inspect.getsource(gribviewport))
    scenes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.endswith(".qml"):
                scenes.append(node.value)
    assert "WeatherBarbs.qml" in scenes
    assert "WeatherScene.qml" in scenes
    # The application's own component is unchanged by the fill: the fill is a
    # separate file, never a part of the barbs.
    barbs = (
        Path(gribviewport.__file__).parent / "shaders" / "WeatherBarbs.qml"
    ).read_text()
    assert "WeatherFill" not in barbs
    assert "weather.frag" not in barbs


def test_the_fill_runs_the_corrected_weather_shader():
    """The fill uses the corrected weather shader, unchanged."""
    shaders = Path(gribviewport.__file__).parent / "shaders"
    fill = (shaders / "WeatherFill.qml").read_text()
    assert "fragmentShader: root.fragmentShader" in fill
    assert 'property string fragmentShader: "weather.frag.qsb"' in fill
    shader = (shaders / "weather.frag").read_text()
    # The corrected missing-data policy: a missing sample contributes
    # exactly zero alpha and is never replaced by a neighbour's value.
    assert "fragColor = vec4(0);" in shader
    assert "if (any(isnan(value)) || any(isinf(value)))" in shader
    # U and V are interpolated first; the wind speed is derived afterwards.
    assert "float speed = clamp(length(uv) / 32.0" in shader
    assert shader.index("uv += value * weight;") < shader.index("length(uv)")


def test_the_fill_is_a_separate_layer_behind_the_barbs():
    """The scene keeps the two layers apart, and the fill behind the barbs."""
    shaders = Path(gribviewport.__file__).parent / "shaders"
    scene = (shaders / "WeatherScene.qml").read_text()
    assert scene.index("WeatherFill") < scene.index("WeatherBarbs")
    assert 'objectName: "weatherFill"' in scene
    assert 'objectName: "weatherBarbs"' in scene
    assert "visible: root.fillEnabled" in scene


def test_a_selection_change_never_restages_the_fill():
    """The fill's publication path never touches the selection."""
    import ast
    import textwrap

    source = textwrap.dedent(inspect.getsource(gribviewport._viewport_widget_class()))
    tree = ast.parse(source)
    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    fill = functions["_publish_fill"]
    names = {child.id for child in ast.walk(fill) if isinstance(child, ast.Name)}
    assert "selection" not in names
    assert "selection" in {
        child.id
        for child in ast.walk(functions["_publish_barbs"])
        if isinstance(child, ast.Name)
    }
    published = functions["_publish"]
    called = {
        child.func.attr
        for child in ast.walk(published)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
    }
    assert "_publish_fill" in called
    assert "_publish_barbs" in called


def test_the_fill_and_the_barbs_count_their_publications_apart():
    """A fill publication moves the fill's counters and not the barbs'."""
    scene = _fill_scene()
    viewport = gribviewport._viewport_widget_class()
    viewport._publish_fill(scene, _field(), ("u0", "v0", "u1", "v1"), _camera())
    assert scene.counters["fill_publications"] == 1
    assert scene.counters["fill_uploads"] == 1
    assert scene.counters["barbs_field_publications"] == 0
    assert scene.counters["barbs_selection_publications"] == 0
    assert scene._barbs.properties == {}
    assert set(scene._fill.properties) == {
        "u0Source",
        "v0Source",
        "u1Source",
        "v1Source",
        "blend",
        "gridX",
        "gridY",
        "gridWidth",
        "gridHeight",
    }
    # A second publication of the same field is not a restaging.
    viewport._publish_fill(scene, _field(), ("u0", "v0", "u1", "v1"), _camera())
    assert scene.counters["fill_publications"] == 1


def test_a_fill_only_publication_leaves_the_barbs_untouched():
    """The fill's own publication path never restages the barbs."""
    scene = _fill_scene()
    viewport = gribviewport._viewport_widget_class()
    urls = viewport.publish_fill(scene, ("u0", "v0", "u1", "v1"), "probe-key")
    assert urls == ("u0", "u1", "u2", "u3")
    assert scene.counters["fill_publications"] == 1
    assert scene.counters["fill_uploads"] == 1
    assert scene.counters["barbs_field_publications"] == 0
    assert scene.counters["barbs_selection_publications"] == 0
    assert scene._barbs.properties == {}


def test_the_application_never_imports_the_legacy_map():
    """Application hosts may use the viewport, never the retired TrackMap."""
    import ast

    package = Path(gribviewport.__file__).parent
    for path in sorted(package.glob("*.py")):
        if path.name == "mapcanvas.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(
                    "mapcanvas" in alias.name.split(".") for alias in node.names
                ), path.name
            elif isinstance(node, ast.ImportFrom):
                assert "mapcanvas" not in (node.module or "").split("."), path.name
                assert not any(
                    alias.name in ("TrackMap", "mapcanvas") for alias in node.names
                ), path.name
            elif isinstance(node, ast.Name):
                assert node.id != "TrackMap", path.name
            elif isinstance(node, ast.Attribute):
                assert node.attr not in ("TrackMap", "mapcanvas"), path.name


def test_the_chart_layer_is_off_by_default():
    """The viewport's entry point keeps the chart layer behind a flag."""
    import ast

    tree = ast.parse(inspect.getsource(gribviewport))
    flags = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "add_argument"
        ):
            for keyword in node.keywords:
                if (
                    keyword.arg == "action"
                    and getattr(keyword.value, "value", "") == "store_true"
                ):
                    flags.append(node.args[0].value)
    assert "--chart-layer" in flags
    widget = gribviewport._viewport_widget_class()
    defaults = {
        name: argument.default
        for name, argument in inspect.signature(widget.__init__).parameters.items()
        if argument.default is not inspect.Parameter.empty
    }
    assert defaults["chart_layer"] is False
    assert defaults["gpu_weather_fill"] is False


def test_the_default_path_does_not_load_the_chart_scene():
    """The chart layer is a separate scene; the barbs' own is unchanged."""
    import ast

    tree = ast.parse(inspect.getsource(gribviewport))
    scenes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.endswith(".qml"):
                scenes.append(node.value)
    assert "ViewportChartScene.qml" in scenes
    shaders = Path(gribviewport.__file__).parent / "shaders"
    barbs = (shaders / "WeatherBarbs.qml").read_text()
    assert "ChartLayer" not in barbs
    scene = (shaders / "ViewportChartScene.qml").read_text()
    # The chart scene carries the application's own weather components
    # unchanged and adds the chart layer behind them.
    assert "WeatherBarbs" in scene
    assert "WeatherFill" in scene


def test_the_chart_composer_is_the_applications_own():
    """The chart layer reuses the application's chart composition."""
    from gweatherrouting.qt.basecharts import BaseChartRenderer
    from gweatherrouting.qt.gribviewportchart import _ChartComposer
    from gweatherrouting.rendering.windpainter import WindPainter

    composer = _ChartComposer("cm93", ())
    assert isinstance(composer._renderer, BaseChartRenderer)
    assert isinstance(composer.painter(), WindPainter)


def _chart_layer(tmp_path, **kwargs):
    from gweatherrouting.qt.gribviewportchart import ViewportChartLayer

    return ViewportChartLayer(paths=(), data_dir=tmp_path, **kwargs)


def _chart_metadata(camera):
    from gweatherrouting.core.mapviewport import MapViewport
    from gweatherrouting.qt.gribviewportchart import ChartCamera

    return {
        "viewport": MapViewport(ChartCamera(camera), 256, lambda: None),
        "time": camera.stamp(),
    }


def test_a_superseded_chart_generation_never_publishes(tmp_path):
    """A chart generation that is replaced while composing never publishes."""
    import time as clock
    from threading import Event

    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append)
    entered = Event()
    release = Event()

    def compose(camera, cancel):
        entered.set()
        release.wait(5)
        return ("image", _chart_metadata(camera))

    layer._compose = compose
    camera = _camera()
    first = layer.request(camera)
    assert entered.wait(5)
    second = layer.request(camera, replace=True)
    release.set()
    deadline = clock.monotonic() + 5
    while not published and clock.monotonic() < deadline:
        clock.sleep(0.01)
    observations = layer.observations()
    assert first in observations["rejected_generations"]
    assert first not in observations["published_generations"]
    assert observations["published_generations"] == (second,)
    assert published and published[0][0] == second
    layer.close()


def test_a_retained_chart_composition_is_reused(tmp_path):
    """The retained composition is reused while it still covers the camera."""
    import time as clock

    published = []
    layer = _chart_layer(tmp_path, on_publish=published.append)

    def compose(camera, cancel):
        return ("image", _chart_metadata(camera))

    layer._compose = compose
    camera = _camera()
    layer.request(camera)
    deadline = clock.monotonic() + 5
    while not published and clock.monotonic() < deadline:
        clock.sleep(0.01)
    assert published
    generation = layer.observations()["generation"]
    layer.request(camera)
    observations = layer.observations()
    assert observations["generation"] == generation
    assert observations["counters"]["reuses"] == 1
    layer.close()


def test_the_chart_layer_never_claims_the_weather_worker(tmp_path):
    """The chart layer's composition yields to the weather pipeline."""
    layer = _chart_layer(tmp_path)
    busy = [False]
    layer._worker_idle = lambda: not busy[0]
    busy[0] = True
    assert layer._await_idle(None) is False
    busy[0] = False
    assert layer._await_idle(None) is True
