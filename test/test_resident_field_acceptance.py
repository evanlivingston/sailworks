"""#64 acceptance: resident fields serve covered demand with no preparation.

A covered camera move or a forecast-time change inside the resident window
must cost zero field preparations and zero misses.  A demand the resident
fields do not cover must show loading, and a ready answer must always cover
the camera.  Headless and model-level; no GPU or window is needed.
"""

from dataclasses import replace
from datetime import timedelta

from gweatherrouting.qt.gribviewport import GribViewportModel, _covers
from test.test_gribviewport import Land, _camera, _paths, _settle_window, _wait_ready
from test.test_weather_staging import gribs, START  # noqa: F401

PAN_STEPS = 20
PAN_STEP_DEGREES = 0.0005


def test_source_retirement_clears_barb_and_fill_qml_bindings():
    from types import SimpleNamespace

    from gweatherrouting.qt.gribviewport import _viewport_widget_class

    class Item:
        def __init__(self):
            self.values = {}

        def setProperty(self, name, value):
            self.values[name] = value

    barbs, fill = Item(), Item()
    _viewport_widget_class()._clear_weather_sources(
        SimpleNamespace(_barbs=barbs, _fill=fill, _native_weather_fields=False)
    )
    assert set(barbs.values) == {
        "u0Source",
        "v0Source",
        "u1Source",
        "v1Source",
        "nodeIdSource",
        "anchorXSource",
        "anchorYSource",
    }
    assert set(fill.values) == {"u0Source", "v0Source", "u1Source", "v1Source"}
    assert all(url.isEmpty() for url in (*barbs.values.values(), *fill.values.values()))


def _resident_fields(model):
    fields = [slot.field for slot in model._slots if slot is not None]
    if model._field is not None:
        fields.append(model._field)
    fields.extend(model._store.fields())
    return fields


def _covered_now(model, camera):
    return any(_covers(field, camera) for field in _resident_fields(model))


def _counters(model):
    return {
        "field_preparations": model.counters["field_preparations"],
        "misses": model.counters["misses"],
    }


def test_covered_pan_inside_the_resident_window_costs_nothing(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        _wait_ready(model)
        covered_inputs = 0
        for step in range(1, PAN_STEPS + 1):
            camera = _camera(center_lon=0.5 + step * PAN_STEP_DEGREES)
            covered = _covered_now(model, camera)
            before = _counters(model)
            state, field, selection = model.update(camera)
            if covered:
                covered_inputs += 1
                assert state == "ready"
                assert _counters(model) == before
                assert field is not None and _covers(field, camera)
                assert selection is not None
            elif state == "ready":
                assert _covers(field, camera)
        assert covered_inputs == PAN_STEPS
    finally:
        model.close()


def test_covered_time_change_inside_the_resident_window_costs_nothing(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land(), resident_time_window=True)
    base = _camera(time=START)
    try:
        _wait_ready(model, camera=base)
        assert _settle_window(model, 1)
        before = _counters(model)
        previous = base
        covered_inputs = 0
        bracket_changes = 0
        hits_before = model.counters["time_window_hits"]
        for hours in (0.5, 1, 1.5, 2):
            camera = replace(base, time=START + timedelta(hours=hours))
            covered = _covered_now(model, camera)
            state, field, selection = model.update(camera)
            if covered:
                covered_inputs += 1
                assert state == "ready"
                assert selection is not None
                assert _counters(model) == before
                if model._time_key(camera.stamp()) != model._time_key(previous.stamp()):
                    bracket_changes += 1
            elif state == "ready":
                assert _covers(field, camera)
            previous = camera
        assert covered_inputs >= 1
        # A hit is counted per forecast-bracket change, not per input.
        assert model.counters["time_window_hits"] - hits_before == bracket_changes
        assert bracket_changes >= 1
    finally:
        model.close()


def test_a_demand_outside_the_resident_field_is_loading_never_stale(gribs):
    paths, _source = _paths(gribs)
    model = GribViewportModel(paths, land_mask=Land())
    try:
        _wait_ready(model)
        far = _camera(center_lat=0.5, center_lon=50.0)
        assert not _covered_now(model, far)
        before = model.counters["misses"]
        state, field, selection = model.update(far)
        assert state in ("loading", "unavailable")
        assert field is None and selection is None
        assert model.counters["misses"] == before + 1
    finally:
        model.close()
