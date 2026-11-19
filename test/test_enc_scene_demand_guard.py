"""One unchanged scene demand mints at most one generation.

Three producers ask for a scene: the settle path, the yield retry and the
cover-stale probe.  Without a per-demand guard each ask minted a generation
that superseded the previous one, so a demand that could not be covered
published, failed coverage and re-asked forever.  These tests count requests;
the presentation rules themselves are covered in
``test_enc_scene_viewport_retention.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event
from types import MethodType, SimpleNamespace

import shapely

from sailworks.qt import enc_trace
from sailworks.qt import gribviewport as gv
from sailworks.qt.encscenelayer import EncSceneLayer
from sailworks.qt.gribviewport import ViewportCamera, _viewport_widget_class
from sailworks.rendering import encview


class _Layer:
    """The layer's request/reuse/resume surface, counted."""

    def __init__(self, state="loading"):
        self.state = state
        self.published = None
        self.generation = 0
        self.mints = []
        self.requests = 0
        self.resumes = 0
        self.prefetches = 0
        self.prefetch_cancels = 0
        self.yielded = None
        self.cells_key = ("cells", "a")

    def prefetch(self, camera=None):
        self.prefetches += 1
        return True

    def cancel_prefetch(self):
        self.prefetch_cancels += 1
        return True

    def request(self, camera, replace=False):
        self.requests += 1
        if self.yielded is not None and not replace:
            generation = self.yielded
            self.yielded = None
            self.state = "loading"
            return generation
        self.generation += 1
        self.mints.append(self.generation)
        self.state = "loading"
        return self.generation

    def resume(self):
        self.resumes += 1
        if self.yielded is None:
            return None
        generation = self.yielded
        self.yielded = None
        self.state = "loading"
        return generation


class _Item:
    def __init__(self):
        self.width = 0
        self.height = 0

    def setWidth(self, value):
        self.width = value

    def setHeight(self, value):
        self.height = value

    def setX(self, value):
        pass

    def setY(self, value):
        pass


class _Timer:
    def __init__(self):
        self.started = []

    def start(self, delay):
        self.started.append(delay)


def _camera(**changes):
    values = dict(
        center_lat=48.00842,
        center_lon=-124.74315,
        zoom=12.0,
        width=800,
        height=600,
    )
    values.update(changes)
    return ViewportCamera(**values)


def _widget(layer=None, camera=None):
    widget_class = _viewport_widget_class()
    widget = SimpleNamespace(
        _disposed=False,
        _enc_scene_model=_Layer() if layer is None else layer,
        _camera=_camera() if camera is None else camera,
        _camera_dpr=1.0,
        _enc_scene_item=_Item(),
        _enc_fallback=None,
        _enc_scene_pending_generation=None,
        _enc_scene_retry_camera=None,
        _enc_scene_retry_attempt=0,
        _enc_scene_retry_timer=_Timer(),
        _enc_scene_demand_key=None,
        _enc_scene_demand_state=None,
        _enc_scene_demand_generation=None,
        _enc_scene_request_cause=None,
        _map_pan_reference=object(),
        _enc_scene_frame_camera=None,
        _enc_scene_camera=None,
        _enc_scene_generation=None,
        _enc_scene_cover_generation=None,
        _enc_scene_partial_generation=None,
        _mark_enc_scene_updating=lambda: None,
    )
    for name in (
        "_request_enc_scene",
        "_retry_enc_scene",
        "_schedule_enc_scene_retry",
        "_place_enc_scene",
    ):
        setattr(widget, name, MethodType(getattr(widget_class, name), widget))
    return widget


def _place_stubs(monkeypatch, *, stale):
    monkeypatch.setattr(gv, "same_frame", lambda first, second: True)
    monkeypatch.setattr(gv, "enc_scene_placement", lambda *args: (0.0, 0.0))
    monkeypatch.setattr(gv, "enc_compose_margin", lambda frame: 0)
    monkeypatch.setattr(gv, "enc_scene_cover_stale", lambda *args: stale)


def test_an_in_flight_demand_mints_one_generation_across_asks():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1

    # Settle ticks keep asking while the generation is in flight.
    for _ in range(4):
        widget._enc_scene_request_cause = "settle"
        assert widget._request_enc_scene() == 1

    # A yield drops the worker's copy; the retry resumes that generation.
    layer.state = "idle"
    layer.yielded = 1
    widget._enc_scene_retry_camera = widget._camera
    widget._enc_scene_pending_generation = 1
    widget._retry_enc_scene()
    widget._retry_enc_scene()

    assert layer.mints == [1]
    assert layer.resumes >= 1


def test_a_failure_clears_a_covered_record_and_asks_again():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_settled(widget, "covered", 1)
    assert widget._enc_scene_demand_state == "covered"

    # A withdrawal naming the demand's own generation, or a layer-level
    # ``unavailable``, means the content it described is gone.
    gv.enc_scene_demand_failed(widget, 1)
    assert widget._enc_scene_demand_state == "failed"
    assert widget._request_enc_scene() == 2
    assert layer.mints == [1, 2]


def test_cover_stale_requests_are_budgeted_per_demand(monkeypatch):
    layer = _Layer()
    widget = _widget(layer)
    widget._enc_scene_frame_camera = widget._camera
    widget._enc_scene_camera = widget._camera
    widget._enc_scene_generation = 1
    _place_stubs(monkeypatch, stale=True)

    # Each new presentation re-opens the per-generation gate; the per-demand
    # budget is the bound, and the slack probe never marks the demand
    # terminal.
    for _ in range(gv.ENC_SCENE_COVER_RETRY_MAX):
        widget._enc_scene_cover_generation = None
        widget._place_enc_scene()
    assert len(layer.mints) == gv.ENC_SCENE_COVER_RETRY_MAX
    assert widget._enc_scene_demand_state == "in_flight"

    for _ in range(3):
        widget._enc_scene_cover_generation = None
        widget._place_enc_scene()
    assert len(layer.mints) == gv.ENC_SCENE_COVER_RETRY_MAX

    # A changed demand key (here, a camera move) starts the budget over.
    widget._camera = _camera(zoom=12.5)
    widget._enc_scene_frame_camera = widget._camera
    widget._enc_scene_cover_generation = None
    widget._place_enc_scene()
    assert len(layer.mints) == gv.ENC_SCENE_COVER_RETRY_MAX + 1


def test_a_covering_frame_records_covered(monkeypatch):
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    widget._enc_scene_frame_camera = widget._camera
    widget._enc_scene_camera = widget._camera
    widget._enc_scene_generation = 1
    _place_stubs(monkeypatch, stale=False)

    widget._place_enc_scene()
    assert widget._enc_scene_demand_state == "covered"

    assert widget._request_enc_scene() == 1
    assert layer.mints == [1]


def test_a_camera_change_requests_immediately():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_settled(widget, "covered", 1)

    widget._camera = _camera(zoom=12.5)
    assert widget._request_enc_scene() == 2
    assert layer.mints == [1, 2]


def test_a_cell_set_change_at_the_same_camera_requests_again():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_settled(widget, "covered", 1)

    # The layer's cell identity is part of the demand.
    layer.cells_key = ("cells", "b")
    assert widget._request_enc_scene() == 2
    assert layer.mints == [1, 2]


def test_a_new_forecast_instant_at_the_same_camera_requests_again():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_settled(widget, "covered", 1)

    # A different instant selects different cells at the same camera.
    widget._camera = _camera(time=datetime(2026, 1, 1, 3, tzinfo=timezone.utc))
    assert widget._request_enc_scene() == 2
    assert layer.mints == [1, 2]


def test_a_failed_demand_still_retries_and_keeps_its_backoff():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_failed(widget)
    assert widget._enc_scene_demand_state == "failed"

    widget._enc_scene_retry_camera = widget._camera
    widget._enc_scene_pending_generation = 1
    layer.state = "idle"
    widget._retry_enc_scene()
    assert layer.mints == [1, 2]

    widget._schedule_enc_scene_retry()
    widget._schedule_enc_scene_retry()
    assert widget._enc_scene_retry_timer.started == [
        gv.ENC_SCENE_RETRY_MS,
        gv.ENC_SCENE_RETRY_MS * 2,
    ]


def test_skipped_attempts_are_traced_with_the_same_demand(monkeypatch):
    events = []
    monkeypatch.setattr(enc_trace, "enabled", lambda: True)
    monkeypatch.setattr(
        enc_trace, "event", lambda kind, **fields: events.append((kind, fields))
    )

    layer = _Layer()
    widget = _widget(layer)
    widget._request_enc_scene()
    widget._enc_scene_request_cause = "settle"
    widget._request_enc_scene()

    assert [kind for kind, _ in events] == ["scene_request", "scene_request_skipped"]
    requested = events[0][1]
    skipped = events[1][1]
    assert requested["cause"] == "settle"
    assert requested["demand"] == skipped["demand"]
    assert skipped["reason"] == "in_flight"
    assert skipped["generation"] == requested["generation"] == 1


def test_a_content_generation_bump_requests_once_at_an_unchanged_camera():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_settled(widget, "covered", 1)

    # Without a content change the covered demand is not asked again.
    for _ in range(3):
        assert widget._request_enc_scene() == 1
    assert layer.mints == [1]

    # A downloaded cell / class toggle is a new content generation.
    layer.cells_key = ("cells", "a", 1)
    assert widget._request_enc_scene() == 2
    assert layer.mints == [1, 2]

    # The new generation is covered in turn; no second ask without a bump.
    assert widget._request_enc_scene() == 2


def test_a_real_layer_bumps_content_generation_at_its_change_points(tmp_path):
    data = tmp_path / "enc"
    data.mkdir()
    (data / "settings.json").write_text("{}")
    layer = EncSceneLayer(data, None)
    assert layer.content_generation == 0
    assert layer.cells_key == (str(data), 0)

    # The user's manifest changed: installed cells, enabled state or the
    # class flag may have moved.
    (data / "settings.json").write_text('{"chart_entries": []}')
    assert layer.content_generation == 1
    assert layer.cells_key == (str(data), 1)

    # A palette replacement is a content change too.
    assert layer.set_palette({"water": "#123456"}) is True
    assert layer.content_generation == 2

    # The worker's catalog refresh observes cell-level changes the settings
    # stamp cannot see; the first view is the baseline, a change bumps.
    layer._note_content(
        SimpleNamespace(generation=(("a", True, 1),), class_enabled=True)
    )
    assert layer.content_generation == 2
    layer._note_content(
        SimpleNamespace(generation=(("a", True, 2),), class_enabled=True)
    )
    assert layer.content_generation == 3


def test_a_covered_demand_is_remembered_and_not_asked_again():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_settled(widget, "covered", 1)
    layer.state = "ready"
    requests, resumes = layer.requests, layer.resumes

    # The demand guard's own bookkeeping drifting (another producer's record)
    # must not re-open a demand whose camera and cell content are unchanged:
    # the layer can only hand back the generation it already holds, so the ask
    # is skipped without even probing ``resume``.
    widget._enc_scene_demand_state = "in_flight"
    for _ in range(5):
        widget._enc_scene_request_cause = "settle"
        assert widget._request_enc_scene() == 1
    assert layer.requests == requests and layer.resumes == resumes
    assert layer.mints == [1]

    # Cell content changing is a genuinely different demand.
    layer.cells_key = ("cells", "b")
    widget._enc_scene_request_cause = "settle"
    assert widget._request_enc_scene() == 2
    assert layer.mints == [1, 2]

    # And so is a camera move, after the new generation is covered.
    gv.enc_scene_demand_settled(widget, "covered", 2)
    widget._camera = _camera(zoom=12.5)
    assert widget._request_enc_scene() == 3
    assert layer.mints == [1, 2, 3]


def test_a_covered_skip_is_traced_once_per_generation(monkeypatch):
    """Five identical settle ticks: no layer work, one informative record."""
    events = []
    monkeypatch.setattr(enc_trace, "enabled", lambda: True)
    monkeypatch.setattr(
        enc_trace, "event", lambda kind, **fields: events.append((kind, fields))
    )

    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_settled(widget, "covered", 1)
    layer.state = "ready"
    events.clear()

    for _ in range(5):
        widget._enc_scene_request_cause = "settle"
        assert widget._request_enc_scene() == 1
    assert layer.requests == 1 and layer.resumes == 0 and layer.mints == [1]

    skipped = [fields for kind, fields in events if kind == "scene_request_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "covered"
    assert skipped[0]["cause"] == "settle"
    assert skipped[0]["generation"] == 1


def test_a_cover_ask_is_not_swallowed_by_a_covered_demand():
    layer = _Layer()
    widget = _widget(layer)
    assert widget._request_enc_scene() == 1
    gv.enc_scene_demand_settled(widget, "covered", 1)

    # A cover replacement is a deliberate widening, even for the same camera.
    widget._enc_scene_request_cause = "cover"
    assert widget._request_enc_scene() == 2
    assert layer.mints == [1, 2]


# ------------------------------------------------------------------ prefetch
def _covered_widget(layer, camera=None):
    """A widget whose demand is covered at the camera it settled on."""
    widget = _widget(layer, camera)
    widget._enc_prefetch_timer = _Timer()
    widget._enc_prefetch_armed_demand = None
    key = gv.enc_scene_demand_key(widget, widget._camera, layer)
    widget._enc_scene_demand_state = "covered"
    widget._enc_scene_demand_key = key
    widget._enc_scene_covered_demand = (key, widget._enc_scene_demand_generation)
    setattr(
        widget,
        "_enc_prefetch_neighbours",
        MethodType(_viewport_widget_class()._enc_prefetch_neighbours, widget),
    )
    return widget


def test_the_prefetch_is_armed_only_by_a_covered_settle():
    layer = _Layer(state="ready")
    widget = _covered_widget(layer)
    widget._enc_scene_demand_state = "in_flight"

    # Still loading: the request that fills the view must not be competed with.
    assert gv.enc_scene_prefetch_settled(widget) is False
    assert widget._enc_prefetch_timer.started == []

    widget._enc_scene_demand_state = "covered"
    assert gv.enc_scene_prefetch_settled(widget) is True
    assert widget._enc_prefetch_timer.started == [gv.ENC_PREFETCH_DELAY_MS]
    # The same settled demand arms once, not on every settle tick.
    assert gv.enc_scene_prefetch_settled(widget) is False
    assert widget._enc_prefetch_timer.started == [gv.ENC_PREFETCH_DELAY_MS]


def test_a_covered_terminal_outcome_arms_the_prefetch():
    layer = _Layer()
    widget = _widget(layer)
    widget._enc_prefetch_timer = _Timer()
    widget._enc_prefetch_armed_demand = None
    assert widget._request_enc_scene() == 1

    gv.enc_scene_demand_settled(widget, "covered", 1)
    assert widget._enc_prefetch_timer.started == [gv.ENC_PREFETCH_DELAY_MS]


def test_the_prefetch_never_runs_while_the_view_is_uncovered():
    layer = _Layer(state="ready")
    widget = _covered_widget(layer)
    widget._enc_scene_demand_state = "in_flight"

    widget._enc_prefetch_neighbours()
    assert layer.prefetches == 0

    # A newer camera invalidates the covered record even once it is covered
    # again: the delay cannot outlive the quiet window that armed it.
    widget._enc_scene_demand_state = "covered"
    widget._enc_scene_covered_demand = (("another", "demand"), 1)
    widget._enc_prefetch_neighbours()
    assert layer.prefetches == 0

    widget._enc_scene_covered_demand = (
        widget._enc_scene_demand_key,
        widget._enc_scene_demand_generation,
    )
    widget._enc_prefetch_neighbours()
    assert layer.prefetches == 1


def test_a_new_demand_cancels_a_queued_prefetch(tmp_path):
    layer = EncSceneLayer(tmp_path, None)
    # No worker is started: the test only exercises the queue's hand-off.
    layer._thread = SimpleNamespace()
    camera = _camera()
    cancel = Event()
    layer._prefetch = (cancel, camera)
    generation = layer.request(camera)
    assert generation is not None
    assert cancel.is_set()
    assert layer._prefetch is None
    layer.close()


def test_a_prefetch_never_publishes_or_advances_a_generation(tmp_path, monkeypatch):
    published = []
    layer = EncSceneLayer(tmp_path, None, on_publish=published.append)
    camera = _camera()
    layer._scene_cache = object()
    layer._mesh_cache = object()
    layer._scene_store = object()
    layer._prefetch_basis = {
        "camera": camera,
        "plan": (),
        "viewport": None,
        "projection": None,
        "display_scale": None,
        "travel": (1.0, 0.0),
    }
    monkeypatch.setattr(
        encview,
        "_view_region",
        lambda plan, viewport: shapely.box(0.0, 0.0, 1.0, 1.0),
    )
    warmed = []

    def fake_prefetch(plan, cache, **kwargs):
        warmed.append(kwargs["granule"])
        return {
            "built": 0,
            "resident": 1,
            "skipped": 0,
            "cancelled": False,
            "reason": None,
        }

    monkeypatch.setattr(encview, "prefetch_granule", fake_prefetch)
    layer._generation = 7
    layer._published = ("sentinel",)

    layer._prefetch_run(Event(), camera)

    # One granule only, the neighbour in the direction of travel, and only the
    # mesh cache moved.
    assert encview.PREFETCH_GRANULES == 1
    assert len(warmed) == 1
    assert layer._generation == 7
    assert layer._published == ("sentinel",)
    assert published == []


def test_a_prefetch_without_travel_warms_nothing(tmp_path, monkeypatch):
    published = []
    layer = EncSceneLayer(tmp_path, None, on_publish=published.append)
    camera = _camera()
    layer._scene_cache = object()
    layer._mesh_cache = object()
    layer._scene_store = object()
    layer._prefetch_basis = {
        "camera": camera,
        "plan": (),
        "viewport": None,
        "projection": None,
        "display_scale": None,
        "travel": None,
    }
    monkeypatch.setattr(
        encview,
        "_view_region",
        lambda plan, viewport: shapely.box(0.0, 0.0, 1.0, 1.0),
    )
    warmed = []
    monkeypatch.setattr(
        encview,
        "prefetch_granule",
        lambda plan, cache, **kwargs: warmed.append(kwargs["granule"]),
    )
    layer._generation = 7
    layer._published = ("sentinel",)

    layer._prefetch_run(Event(), camera)

    # No direction of travel: there is no neighbour worth guessing at.
    assert warmed == []
    assert layer._generation == 7
    assert layer._published == ("sentinel",)
    assert published == []


def _ready_widget(layer, cell_states):
    """A widget whose demand settled at ``ready`` with the given cell states."""
    widget = _widget(layer)
    widget._enc_prefetch_timer = _Timer()
    widget._enc_prefetch_armed_demand = None
    widget._enc_scene_demand_key = gv.enc_scene_demand_key(
        widget, widget._camera, layer
    )
    # ``covers_view`` false keeps the demand at ``in_flight``: the settle path
    # never records it as covered, which is exactly the live case this fixes.
    widget._enc_scene_demand_state = "in_flight"
    widget._enc_scene_cell_states = tuple(cell_states)
    setattr(
        widget,
        "_enc_prefetch_neighbours",
        MethodType(_viewport_widget_class()._enc_prefetch_neighbours, widget),
    )
    return widget


def test_a_ready_composition_with_cell_gaps_arms_the_prefetch():
    layer = _Layer(state="ready")
    widget = _ready_widget(
        layer,
        (
            {"path": "a", "state": "ready"},
            {"path": "b", "state": "ready"},
            {"path": "c", "state": "ready"},
            {"path": "d", "state": "ready"},
        ),
    )

    # Four cells drawn and the layer ready, but the bands leave genuine gaps:
    # the composition does not cover the view, and it still arms.
    assert gv.enc_scene_prefetch_settled(widget) is True
    assert widget._enc_prefetch_timer.started == [gv.ENC_PREFETCH_DELAY_MS]
    # Once per demand, not on every settle tick.
    assert gv.enc_scene_prefetch_settled(widget) is False
    assert widget._enc_prefetch_timer.started == [gv.ENC_PREFETCH_DELAY_MS]


def test_a_loading_or_empty_composition_does_not_arm():
    loading = _ready_widget(_Layer(state="loading"), ({"state": "ready"},))
    assert gv.enc_scene_prefetch_settled(loading) is False
    assert loading._enc_prefetch_timer.started == []

    empty = _ready_widget(_Layer(state="ready"), ())
    assert gv.enc_scene_prefetch_settled(empty) is False
    assert empty._enc_prefetch_timer.started == []

    refused = _ready_widget(_Layer(state="ready"), ({"state": "unavailable"},))
    assert gv.enc_scene_prefetch_settled(refused) is False
    assert refused._enc_prefetch_timer.started == []

    resolving = _ready_widget(
        _Layer(state="ready"), ({"state": "ready"}, {"state": "loading"})
    )
    assert gv.enc_scene_prefetch_settled(resolving) is False
    assert resolving._enc_prefetch_timer.started == []


def test_a_new_demand_cancels_an_armed_prefetch():
    layer = _Layer(state="ready")
    widget = _ready_widget(layer, ({"path": "a", "state": "ready"},))
    assert gv.enc_scene_prefetch_settled(widget) is True
    assert widget._enc_prefetch_armed_demand is not None

    # A real request before the quiet delay elapses wins over the idle warm.
    widget._camera = _camera(center_lon=-124.0)
    assert widget._request_enc_scene() == 1
    assert widget._enc_prefetch_armed_demand is None
    assert layer.state == "loading"
    assert gv.enc_scene_prefetch_settled(widget) is False

    # Even if the old timer still fires, the run re-check refuses it.
    widget._enc_prefetch_neighbours()
    assert layer.prefetches == 0
