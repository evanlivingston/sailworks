"""The settled raster chart waits for the ENC scene's own publication.

A settled chart request asks the scene layer for the same camera first.  The
scene publishes that view a second or so later; composing the raster chart in
the same tick duplicates it and costs seconds.  These tests cover the pure
deferral decision and the bounded fallback around the widget methods that use
it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sailworks.qt import enc_trace
from sailworks.qt.gribviewport import (
    CHART_SCENE_DEFER_MS,
    _viewport_widget_class,
    apply_scene_publication,
    defer_chart_for_scene,
    enc_scene_covers_view,
    ensure_chart_content,
    plan_scene_deferral,
    resolve_scene_deferral,
    scene_chart_deferral_live,
    scene_chart_deferral_pending,
    scene_draws_chart,
    scene_presents_chart,
    scene_suppression_expired,
    trace_raster_visibility,
)


@dataclass(frozen=True)
class FakeCamera:
    center_lon: float = -122.4
    center_lat: float = 37.8
    zoom: float = 14.0
    width: int = 1000
    height: int = 720
    time: float = 0.0


NOW = 1000.0
CAMERA = FakeCamera()


def _deferral(camera=CAMERA, generation=42, deadline=NOW + 1.5):
    return {"camera": camera, "generation": generation, "deadline": deadline}


@pytest.fixture(scope="module")
def viewport_class():
    return _viewport_widget_class()


class FakeTimer:
    def __init__(self):
        self.started = []
        self.stopped = False

    def start(self, ms):
        self.started.append(ms)

    def stop(self):
        self.stopped = True


class _SceneItem:
    """The GUI item's coverage surface, as the real EncMeshItem reports it."""

    def __init__(self, vertices=100, covers=True, visible=True):
        self.vertices = vertices
        self.covers = covers
        self.visible = visible

    def isVisible(self):
        return self.visible

    def shown_vertex_count(self):
        return self.vertices

    def scene_covers_view(self):
        return self.covers

    def setVisible(self, value):
        self.visible = bool(value)


# ---- pure decision -------------------------------------------------------


def test_scene_in_flight_for_same_camera_defers():
    action, pending = plan_scene_deferral(
        pending=None,
        camera=CAMERA,
        generation=42,
        published=(41, object(), {}),
        now=NOW,
        timeout_ms=CHART_SCENE_DEFER_MS,
    )
    assert action == "wait"
    assert pending["camera"] == CAMERA
    assert pending["generation"] == 42
    assert pending["deadline"] == pytest.approx(NOW + CHART_SCENE_DEFER_MS / 1000.0)


def test_reused_generation_is_not_in_flight():
    # The layer served an already-published generation: no publication follows.
    assert not scene_chart_deferral_pending(41, (41, object(), {}))
    action, pending = plan_scene_deferral(
        pending=None,
        camera=CAMERA,
        generation=41,
        published=(41, object(), {}),
        now=NOW,
        timeout_ms=CHART_SCENE_DEFER_MS,
    )
    assert (action, pending) == ("compose", None)


def test_no_scene_request_composes():
    action, pending = plan_scene_deferral(
        pending=None,
        camera=CAMERA,
        generation=None,
        published=None,
        now=NOW,
        timeout_ms=CHART_SCENE_DEFER_MS,
    )
    assert (action, pending) == ("compose", None)


def test_timeout_composes_raster():
    pending = _deferral(deadline=NOW - 0.001)
    action, record = plan_scene_deferral(
        pending=pending,
        camera=CAMERA,
        generation=43,
        published=(41, object(), {}),
        now=NOW,
        timeout_ms=CHART_SCENE_DEFER_MS,
    )
    assert action == "compose"
    # The expired episode is retained so a later demand cannot re-arm it.
    assert record["expired"] is True
    assert record["deadline"] == pytest.approx(NOW - 0.001)
    assert not scene_chart_deferral_live(pending, CAMERA, NOW)


def test_same_camera_keeps_original_deadline_across_reentries():
    pending = _deferral(deadline=NOW + 1.0)
    action, record = plan_scene_deferral(
        pending=pending,
        camera=CAMERA,
        generation=99,
        published=(41, object(), {}),
        now=NOW + 0.5,
        timeout_ms=CHART_SCENE_DEFER_MS,
    )
    assert action == "wait"
    # A new generation does not buy a fresh timeout.
    assert record["deadline"] == pytest.approx(NOW + 1.0)
    assert record["generation"] == 99


def test_camera_change_keeps_the_loading_episode_deadline():
    # A zoom or pan is a new demand, but it is still the same continuous
    # loading episode.  Giving every camera its own 1.5 s was the re-arm that
    # kept the raster hidden for 5.8 s in the live trace.
    other = FakeCamera(center_lon=-121.0)
    action, record = plan_scene_deferral(
        pending=_deferral(camera=CAMERA, deadline=NOW + 1.5),
        camera=other,
        generation=50,
        published=(41, object(), {}),
        now=NOW + 0.2,
        timeout_ms=CHART_SCENE_DEFER_MS,
    )
    assert action == "wait"
    assert record["camera"] == other
    assert record["generation"] == 50
    assert record["deadline"] == pytest.approx(NOW + 1.5)


def test_an_expired_episode_cannot_be_rearmed_by_a_new_demand():
    expired = dict(_deferral(), expired=True)
    action, record = plan_scene_deferral(
        pending=expired,
        camera=FakeCamera(center_lon=-121.0),
        generation=51,
        published=(41, object(), {}),
        now=NOW + 0.1,
        timeout_ms=CHART_SCENE_DEFER_MS,
    )
    assert action == "compose"
    assert record["expired"] is True
    assert record["deadline"] == pytest.approx(NOW + 1.5)


def test_repeated_partials_do_not_extend_the_deadline():
    pending = _deferral(deadline=NOW + 1.5)
    # Every partial re-enters the plan with a newer generation; none of them
    # may move the deadline.
    for step, generation in enumerate((43, 44, 45, 46)):
        action, pending = plan_scene_deferral(
            pending=pending,
            camera=CAMERA,
            generation=generation,
            published=(41, object(), {}),
            now=NOW + 0.1 * (step + 1),
            timeout_ms=CHART_SCENE_DEFER_MS,
        )
        assert action == "wait"
        assert pending["deadline"] == pytest.approx(NOW + 1.5)
    assert pending["generation"] == 46


# ---- publication resolution ---------------------------------------------


def test_complete_scene_for_this_camera_skips_raster():
    assert (
        resolve_scene_deferral(
            pending=_deferral(),
            camera=CAMERA,
            generation=42,
            metadata={"camera": CAMERA},
        )
        == "skip"
    )


def test_partial_scene_keeps_waiting():
    assert (
        resolve_scene_deferral(
            pending=_deferral(),
            camera=CAMERA,
            generation=42,
            metadata={"camera": CAMERA, "partial": True},
        )
        == "keep"
    )


def test_withdrawal_for_this_generation_releases_raster():
    assert (
        resolve_scene_deferral(
            pending=_deferral(), camera=CAMERA, generation=42, metadata=None
        )
        == "release"
    )


def test_withdrawal_for_superseded_generation_keeps_waiting():
    assert (
        resolve_scene_deferral(
            pending=_deferral(), camera=CAMERA, generation=7, metadata=None
        )
        == "keep"
    )


def test_publication_for_another_camera_releases_raster():
    other = FakeCamera(center_lon=-121.0)
    assert (
        resolve_scene_deferral(
            pending=_deferral(camera=CAMERA),
            camera=other,
            generation=42,
            metadata={"camera": other},
        )
        == "release"
    )


def test_complete_scene_for_a_stale_view_releases_raster():
    stale = FakeCamera(center_lon=-120.0)
    assert (
        resolve_scene_deferral(
            pending=_deferral(),
            camera=CAMERA,
            generation=42,
            metadata={"camera": stale},
        )
        == "release"
    )


def test_no_pending_deferral_ignores_publications():
    assert (
        resolve_scene_deferral(
            pending=None, camera=CAMERA, generation=42, metadata={"camera": CAMERA}
        )
        == "keep"
    )


def test_scene_draws_chart_requires_exact_complete_camera():
    assert scene_draws_chart({"camera": CAMERA}, CAMERA)
    assert not scene_draws_chart({"camera": CAMERA, "partial": True}, CAMERA)
    assert not scene_draws_chart({"camera": FakeCamera(zoom=13.0)}, CAMERA)
    assert not scene_draws_chart(None, CAMERA)


# ---- coverage gate: raster may be held only while ENC draws this view ----


def test_enc_coverage_needs_visibility_vertices_and_overlap():
    assert enc_scene_covers_view(_SceneItem())
    assert not enc_scene_covers_view(_SceneItem(visible=False))
    assert not enc_scene_covers_view(_SceneItem(vertices=0))
    assert not enc_scene_covers_view(_SceneItem(covers=False))
    assert not enc_scene_covers_view(None)


def test_a_complete_scene_claim_cannot_hold_the_raster_past_its_budget():
    """A partially presented scene reports coverage forever; the hold is bounded.

    The startup trace showed a 33.6 s suppression loop at ``500 ms`` intervals
    with ``cause: scene_complete`` while the ENC frame drew 86 559 of its
    338 730 vertices.  Coverage reported by a frame's bounding geometry must
    not outlive the budget.
    """
    from sailworks.qt.gribviewportchart import CHART_SCENE_SUPPRESS_MAX_MS

    state = SimpleNamespace()
    # The first decision starts the episode and withholds for this camera.
    assert (
        scene_suppression_expired(state, CAMERA, NOW, CHART_SCENE_SUPPRESS_MAX_MS)
        is False
    )
    # Its whole budget may be spent, no more.
    assert (
        scene_suppression_expired(
            state,
            CAMERA,
            NOW + CHART_SCENE_SUPPRESS_MAX_MS / 1000.0 - 0.001,
            CHART_SCENE_SUPPRESS_MAX_MS,
        )
        is False
    )
    assert (
        scene_suppression_expired(
            state,
            CAMERA,
            NOW + CHART_SCENE_SUPPRESS_MAX_MS / 1000.0,
            CHART_SCENE_SUPPRESS_MAX_MS,
        )
        is True
    )
    # A different camera is a new episode with a fresh budget.
    other = FakeCamera(center_lon=-121.0)
    assert (
        scene_suppression_expired(state, other, NOW + 10.0, CHART_SCENE_SUPPRESS_MAX_MS)
        is False
    )


def test_a_timeline_tick_does_not_rearm_the_suppression_budget():
    """A new forecast instant in the same view keeps the original episode.

    The startup trace re-armed the budget on every demand: lat, lon, zoom and
    size were identical, but ``ViewportCamera`` also carries the forecast time,
    which the timeline rewrites each tick.  Keying on the whole camera gave a
    fresh 1 s budget for every tick, so the loop never expired.
    """
    from sailworks.qt.gribviewportchart import CHART_SCENE_SUPPRESS_MAX_MS

    state = SimpleNamespace()
    assert (
        scene_suppression_expired(state, CAMERA, NOW, CHART_SCENE_SUPPRESS_MAX_MS)
        is False
    )
    later_time = FakeCamera(time=1.0)
    assert later_time != CAMERA
    # Same geometry, new time: the same episode, which is now over budget.
    assert (
        scene_suppression_expired(
            state,
            later_time,
            NOW + CHART_SCENE_SUPPRESS_MAX_MS / 1000.0,
            CHART_SCENE_SUPPRESS_MAX_MS,
        )
        is True
    )
    # A moved view is still a new episode with a fresh budget.
    moved = FakeCamera(center_lon=-121.0, time=2.0)
    assert (
        scene_suppression_expired(
            state,
            moved,
            NOW + 10.0,
            CHART_SCENE_SUPPRESS_MAX_MS,
        )
        is False
    )


class _FakeChartModel:
    def __init__(self):
        self.requests = []

    def request(self, camera, cause=None, replace=False):
        self.requests.append((camera, cause))
        return None


def test_a_presented_scene_stops_the_raster_entirely(viewport_class):
    """A scene that covers the view is the chart: the raster does not compose.

    A painted raster used to justify composing again, which re-anchored the
    picture on every publication and snapped the view mid-drag.  The scene
    draws in map coordinates, so with it covering the view the raster neither
    composes nor defers; only a short re-check is armed so that raster returns
    as soon as coverage ends.
    """
    import time

    timer = FakeTimer()
    chart_model = _FakeChartModel()
    state = SimpleNamespace(
        _camera=CAMERA,
        _chart_model=chart_model,
        _chart_timer=timer,
        _chart_camera_changed=time.perf_counter() - 10.0,
        _chart_scene_deferral=None,
        _chart_visible_generation=7,
        _chart_pending=None,
        _chart_redelivery_generation=None,
        _chart_budget_refusal=None,
        _chart_request_times=None,
        _chart_force_replace=False,
        _weather_ready_at=123.0,
        _model=SimpleNamespace(busy=lambda: False),
        _enc_scene_model=SimpleNamespace(_published=(41, None, {"camera": CAMERA})),
        _enc_scene_item=_SceneItem(),
        _request_enc_scene=lambda: 42,
        _flush_held_enc_scene_adoption=lambda: None,
        _scene_draws_chart=lambda: True,
        _chart_state_now=lambda: {},
        counters={"chart_requests": 0},
    )
    state._chart_scene_suppressed_at = time.perf_counter()
    state._chart_scene_suppressed_camera = (-122.4, 37.8, 14.0, 1000, 720)

    viewport_class._request_chart(state)

    # Nothing composed; a single re-check keeps the return prompt.
    assert chart_model.requests == []
    assert len(timer.started) == 1
    assert state._chart_scene_suppressed_at is None
    assert state._chart_scene_suppressed_camera is None
    assert state.counters["chart_requests"] == 0


def test_the_first_raster_takes_a_short_startup_head_start():
    """Before the first raster the long cold-start window starves the only chart."""
    from sailworks.qt.gribviewportchart import (
        CHART_RETAINED_HEAD_START_MS,
        CHART_STARTUP_HEAD_START_MS,
        CHART_WEATHER_READY_WAIT_MS,
    )

    assert CHART_STARTUP_HEAD_START_MS <= CHART_RETAINED_HEAD_START_MS
    assert CHART_STARTUP_HEAD_START_MS < CHART_WEATHER_READY_WAIT_MS


def test_a_complete_scene_is_held_only_until_the_first_raster(viewport_class):
    """The complete frame's GUI-thread build may not precede the first raster."""
    from sailworks.qt.gribviewportchart import CHART_STARTUP_SCENE_HOLD_MS

    hold = viewport_class._hold_scene_for_first_raster
    state = SimpleNamespace(_chart_visible_generation=None)
    payload = (7, "scene", {})
    assert hold(state, 7, payload) is True
    assert state._enc_scene_held_publication is payload
    # Once the hold's budget is spent the scene is adopted: a raster that
    # never arrives cannot withhold the ENC.
    state._enc_scene_hold_since -= CHART_STARTUP_SCENE_HOLD_MS / 1000.0
    assert hold(state, 7, payload) is False
    assert state._enc_scene_held_publication is None
    # After any raster has been shown the hold never applies again.
    state._chart_visible_generation = 3
    assert hold(state, 7, payload) is False


def test_complete_scene_still_holds_the_raster_only_while_it_covers():
    metadata = {"camera": CAMERA}
    assert scene_presents_chart(metadata, CAMERA, _SceneItem())
    # A complete generation for this camera that draws nothing is not chart
    # content: the raster must be shown.
    assert not scene_presents_chart(metadata, CAMERA, _SceneItem(covers=False))
    assert not scene_presents_chart(metadata, CAMERA, _SceneItem(vertices=0))
    assert not scene_presents_chart(
        {"camera": CAMERA, "partial": True}, CAMERA, _SceneItem()
    )
    assert not scene_presents_chart(
        {"camera": FakeCamera(zoom=13.0)}, CAMERA, _SceneItem()
    )


# ---- widget-side wait and release ---------------------------------------


def test_widget_defers_and_arms_bounded_timer(viewport_class):
    timer = FakeTimer()
    state = SimpleNamespace(
        _enc_scene_model=SimpleNamespace(_published=(41, None, {})),
        _camera=CAMERA,
        _chart_timer=timer,
        _chart_scene_deferral=None,
        _enc_scene_item=_SceneItem(),
    )
    assert defer_chart_for_scene(state, 42, NOW) is True
    record = state._chart_scene_deferral
    assert record["camera"] == CAMERA
    assert record["generation"] == 42
    assert record["deadline"] == pytest.approx(NOW + CHART_SCENE_DEFER_MS / 1000.0)
    assert timer.started == [CHART_SCENE_DEFER_MS]


def test_widget_composes_when_the_wait_expires(viewport_class):
    timer = FakeTimer()
    state = SimpleNamespace(
        _enc_scene_model=SimpleNamespace(_published=(41, None, {})),
        _camera=CAMERA,
        _chart_timer=timer,
        _chart_scene_deferral=_deferral(deadline=NOW - 1.0),
        _enc_scene_item=_SceneItem(),
    )
    assert defer_chart_for_scene(state, 42, NOW) is False
    # The episode is retained as expired: the raster composes now, and the
    # next demand in the same loading episode will not re-arm the wait.
    assert state._chart_scene_deferral["expired"] is True
    assert timer.started == []


def test_widget_composes_immediately_without_enc_coverage(viewport_class):
    # The presented ENC frame draws nothing, so even a fresh scene request
    # may not hold the raster back.
    timer = FakeTimer()
    state = SimpleNamespace(
        _enc_scene_model=SimpleNamespace(_published=(41, None, {})),
        _camera=CAMERA,
        _chart_timer=timer,
        _chart_scene_deferral=None,
        _enc_scene_item=_SceneItem(vertices=0, covers=False),
    )
    assert defer_chart_for_scene(state, 42, NOW) is False
    assert state._chart_scene_deferral is None
    assert timer.started == []


def test_widget_composes_without_a_scene_layer(viewport_class):
    timer = FakeTimer()
    state = SimpleNamespace(
        _enc_scene_model=None,
        _camera=CAMERA,
        _chart_timer=timer,
        _chart_scene_deferral=_deferral(),
    )
    assert defer_chart_for_scene(state, 42, NOW) is False
    assert state._chart_scene_deferral is None
    assert timer.started == []


def test_a_withdrawn_scene_requests_the_raster_at_once(viewport_class):
    """Withdrawal leaves nothing on screen: the raster composes immediately."""
    timer = FakeTimer()
    item = _SceneItem()
    state = SimpleNamespace(
        _camera=CAMERA,
        _chart_model=_FakeChartModel(),
        _chart_timer=timer,
        _chart_visible_generation=None,
        _chart_scene_deferral=_deferral(),
        _chart_scene_suppressed_at=NOW,
        _chart_scene_suppressed_camera=(1, 2, 3, 4, 5),
        _enc_scene_item=item,
        _enc_scene_ready_generation=42,
        _enc_scene_partial_generation=42,
        _enc_scene_provisional=True,
        _enc_scene_cells_order=(),
        _enc_scene_cell_states=(),
        _enc_fallback=None,
        _disposed=False,
        _cancel_enc_scene_partial_hold=lambda: None,
    )

    viewport_class._withdraw_enc_scene(state)

    assert item.isVisible() is False
    assert state._enc_scene_ready_generation is None
    # Not the 500 ms coverage re-check and not the settle window: the next
    # event-loop turn asks the chart layer.
    assert timer.started == [1]
    # Neither a deferral nor a suppression budget may hold the raster back.
    assert state._chart_scene_deferral is None
    assert state._chart_scene_suppressed_at is None
    assert state._chart_scene_suppressed_camera is None
    assert state._chart_force_coverage is True


def test_a_scene_with_no_geometry_composes_the_raster(viewport_class):
    """A generation that adopted nothing is not chart content."""
    timer = FakeTimer()
    state = SimpleNamespace(
        _camera=CAMERA,
        _chart_model=_FakeChartModel(),
        _chart_timer=timer,
        _chart_visible_generation=7,
        _enc_scene_item=_SceneItem(vertices=0, covers=False),
        _disposed=False,
    )
    assert ensure_chart_content(state, "scene_not_presenting", generation=42) is True
    assert timer.started == [1]
    assert state._chart_force_coverage is True


def test_a_hidden_scene_composes_the_raster(viewport_class):
    timer = FakeTimer()
    state = SimpleNamespace(
        _camera=CAMERA,
        _chart_model=_FakeChartModel(),
        _chart_timer=timer,
        _chart_visible_generation=3,
        _enc_scene_item=_SceneItem(visible=False),
        _disposed=False,
    )
    assert ensure_chart_content(state, "scene_hidden") is True
    assert timer.started == [1]


def test_a_covering_scene_still_suppresses_the_raster(viewport_class):
    """No regression: a healthy covering mesh is not duplicated by a raster."""
    timer = FakeTimer()
    state = SimpleNamespace(
        _camera=CAMERA,
        _chart_model=_FakeChartModel(),
        _chart_timer=timer,
        _chart_visible_generation=3,
        _enc_scene_item=_SceneItem(),
        _disposed=False,
    )
    assert ensure_chart_content(state, "scene_adopted") is False
    assert timer.started == []
    assert getattr(state, "_chart_force_coverage", False) is False


def test_a_disposed_viewport_does_not_compose(viewport_class):
    timer = FakeTimer()
    state = SimpleNamespace(
        _camera=CAMERA,
        _chart_model=_FakeChartModel(),
        _chart_timer=timer,
        _enc_scene_item=_SceneItem(vertices=0),
        _disposed=True,
    )
    assert ensure_chart_content(state, "scene_withdrawn") is False
    assert timer.started == []


def test_blank_guard_trace_names_the_reason_and_scene_state(
    monkeypatch, tmp_path, viewport_class
):
    target = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    enc_trace.reset()
    try:
        timer = FakeTimer()
        state = SimpleNamespace(
            _camera=CAMERA,
            _chart_model=_FakeChartModel(),
            _chart_timer=timer,
            _chart_visible_generation=None,
            _enc_scene_item=_SceneItem(vertices=0, covers=False, visible=False),
            _disposed=False,
        )
        ensure_chart_content(state, "scene_withdrawn", generation=42)
        records = [
            json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
        ]
        guard = [r for r in records if r["event"] == "chart_blank_guard"]
        assert len(guard) == 1
        record = guard[0]
        assert record["reason"] == "scene_withdrawn"
        assert record["generation"] == 42
        assert record["scene_verts"] == 0
        assert record["scene_visible"] is False
        assert record["scene_covers"] is False
        assert record["raster_requested"] is True
    finally:
        enc_trace.reset()


def test_widget_skips_raster_when_the_scene_publishes(viewport_class):
    timer = FakeTimer()
    restarts = []
    state = SimpleNamespace(
        _camera=CAMERA,
        _chart_timer=timer,
        _chart_scene_deferral=_deferral(),
        _restart_chart_settle=lambda: restarts.append(True),
    )
    assert apply_scene_publication(state, 42, {"camera": CAMERA}) == "skip"
    assert state._chart_scene_deferral is None
    assert timer.stopped is True
    assert restarts == []


def test_widget_releases_raster_on_withdrawal(viewport_class):
    timer = FakeTimer()
    restarts = []
    state = SimpleNamespace(
        _camera=CAMERA,
        _chart_timer=timer,
        _chart_scene_deferral=_deferral(),
        _restart_chart_settle=lambda: restarts.append(True),
    )
    assert apply_scene_publication(state, 42, None) == "release"
    assert state._chart_scene_deferral is None
    assert timer.stopped is True
    assert restarts == [True]


def test_widget_second_camera_change_cancels_the_first_deferral(viewport_class):
    timer = FakeTimer()
    other = FakeCamera(center_lon=-121.0)
    state = SimpleNamespace(
        _enc_scene_model=SimpleNamespace(_published=(41, None, {})),
        _camera=other,
        _chart_timer=timer,
        _chart_scene_deferral=_deferral(camera=CAMERA),
        _enc_scene_item=_SceneItem(),
    )
    assert defer_chart_for_scene(state, 43, NOW + 0.1) is True
    assert state._chart_scene_deferral["camera"] == other
    assert state._chart_scene_deferral["generation"] == 43
    assert not scene_chart_deferral_live(state._chart_scene_deferral, CAMERA, NOW + 0.2)


# ---- raster visibility trace ---------------------------------------------


def test_raster_visibility_trace_records_transitions_only(monkeypatch, tmp_path):
    target = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    enc_trace.reset()
    try:
        state = SimpleNamespace(
            _chart_visible_generation=None,
            _enc_scene_item=_SceneItem(covers=False),
            _enc_scene_ready_generation=5,
            _enc_scene_partial_generation=None,
            _enc_scene_provisional=False,
        )
        assert trace_raster_visibility(state, "scene_published") is not None
        # The same presence is not logged twice.
        assert trace_raster_visibility(state, "chart_tick") is None

        state._chart_visible_generation = 1
        assert trace_raster_visibility(state, "chart_adopted") is not None

        records = [
            json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
        ]
        assert [record["event"] for record in records] == ["raster_visibility"] * 2
        assert records[0]["visible"] is False
        assert records[0]["reason"] == "scene_published"
        assert records[1]["visible"] is True
        assert records[1]["generation"] == 1
        assert records[1]["enc_generation"] == 5
        assert records[1]["enc_covers_view"] is False
        assert records[1]["reason"] == "chart_adopted"
    finally:
        enc_trace.reset()


def test_raster_visibility_trace_is_off_by_default(monkeypatch):
    monkeypatch.delenv(enc_trace.ENV_PATH, raising=False)
    enc_trace.reset()
    state = SimpleNamespace(
        _chart_visible_generation=1,
        _enc_scene_item=_SceneItem(),
        _enc_scene_ready_generation=5,
        _enc_scene_partial_generation=None,
    )
    assert trace_raster_visibility(state, "chart_adopted") is None


class _ChartLayerItem:
    """The ChartLayer QML item's own on-screen state, as the widget reads it."""

    def __init__(self, *, source="", ready=True, visible=True, opacity=1.0):
        self.slot0 = source
        self.slot1 = ""
        self.ready = ready
        self.visible = visible
        self.opacity_value = opacity
        self.front_slot = 0

    def isVisible(self):
        return self.visible

    def opacity(self):
        return self.opacity_value

    def setVisible(self, visible):
        self.visible = bool(visible)

    def property(self, name):
        return {
            "ready": self.ready,
            "visible": self.visible,
            "frontSlot": self.front_slot,
            "slot0Source": self.slot0,
            "slot1Source": self.slot1,
        }.get(name)


def _raster_state(chart, generation):
    return SimpleNamespace(
        _chart_visible_generation=generation,
        _chart=chart,
        _enc_scene_item=_SceneItem(covers=False),
        _enc_scene_ready_generation=None,
        _enc_scene_partial_generation=None,
        _enc_scene_provisional=False,
    )


def test_raster_visibility_records_a_real_hide(monkeypatch, tmp_path):
    target = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    enc_trace.reset()
    try:
        chart = _ChartLayerItem(source="image://chart/7")
        state = _raster_state(chart, 7)
        assert trace_raster_visibility(state, "chart_adopted") is not None

        # A withdrawal clears the front slot: the layer paints nothing even
        # though a generation was adopted earlier.
        chart.slot0 = ""
        assert trace_raster_visibility(state, "chart_withdrawn") is not None

        records = [
            json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
        ]
        assert records[0]["visible"] is True
        assert records[0]["painted_generation"] == 7
        assert records[0]["replaced"] is False
        assert records[1]["visible"] is False
        assert records[1]["painted_generation"] is None
        assert records[1]["reason"] == "chart_withdrawn"
        assert records[1]["hidden_total_ms"] >= 0.0
        assert state._enc_trace_raster_hidden_since is not None
    finally:
        enc_trace.reset()


def test_raster_visibility_marks_a_visible_replacement(monkeypatch, tmp_path):
    target = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    enc_trace.reset()
    try:
        chart = _ChartLayerItem(source="image://chart/7")
        state = _raster_state(chart, 7)
        assert trace_raster_visibility(state, "chart_adopted") is not None

        # A new image is promoted while the layer stays on screen: a blink by
        # replacement, distinct from a hide.
        chart.slot0 = "image://chart/8"
        state._chart_visible_generation = 8
        state._enc_trace_painted = {
            "generation": 8,
            "mode": "reused",
            "composed_pixels": 0,
            "reused_pixels": 6400,
        }
        assert trace_raster_visibility(state, "chart_adopted") is not None

        records = [
            json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
        ]
        assert records[1]["visible"] is True
        assert records[1]["replaced"] is True
        assert records[1]["painted_generation"] == 8
        assert records[1]["painted_mode"] == "reused"
        assert records[1]["painted_reused_pixels"] == 6400
        assert records[1]["hidden_total_ms"] == 0.0
    finally:
        enc_trace.reset()


class _Url:
    """A QUrl stand-in: its text is only reachable through toString()."""

    def __init__(self, text):
        self.text = text

    def toString(self):
        return self.text

    def __str__(self):
        return "PySide6.QtCore.QUrl('%s')" % self.text


def test_raster_visibility_reads_a_qurl_front_source(monkeypatch, tmp_path):
    target = tmp_path / "trace.jsonl"
    monkeypatch.setenv(enc_trace.ENV_PATH, str(target))
    enc_trace.reset()
    try:
        chart = _ChartLayerItem(source=_Url("image://chart/11"))
        state = _raster_state(chart, 11)
        assert trace_raster_visibility(state, "chart_adopted") is not None
        records = [
            json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
        ]
        assert records[0]["visible"] is True
        assert records[0]["painted_generation"] == 11
    finally:
        enc_trace.reset()


def test_raster_visibility_trace_is_a_no_op_with_a_live_layer(monkeypatch):
    monkeypatch.delenv(enc_trace.ENV_PATH, raising=False)
    enc_trace.reset()
    chart = _ChartLayerItem(source="image://chart/1")
    state = _raster_state(chart, 1)
    assert trace_raster_visibility(state, "chart_adopted") is None
    assert not hasattr(state, "_enc_trace_raster_state")


# ---- prompt retry after a scene yield ------------------------------------


def test_a_yield_schedules_a_prompt_retry(viewport_class):
    from sailworks.qt import gribviewport as gv

    timer = FakeTimer()
    state = SimpleNamespace(
        _disposed=False,
        _enc_scene_retry_timer=timer,
        _enc_scene_retry_attempt=0,
    )
    viewport_class._schedule_enc_scene_retry(state)
    viewport_class._schedule_enc_scene_retry(state)
    assert timer.started == [gv.ENC_SCENE_RETRY_MS, gv.ENC_SCENE_RETRY_MS * 2]
    # Far below the five-second chart-source poll that used to be the only
    # asker after a yield.
    assert max(timer.started) < 5000


def test_retries_are_bounded_per_camera(viewport_class):
    from sailworks.qt import gribviewport as gv

    timer = FakeTimer()
    state = SimpleNamespace(
        _disposed=False,
        _enc_scene_retry_timer=timer,
        _enc_scene_retry_attempt=0,
    )
    for _ in range(gv.ENC_SCENE_RETRY_ATTEMPTS + 5):
        viewport_class._schedule_enc_scene_retry(state)
    assert len(timer.started) == gv.ENC_SCENE_RETRY_ATTEMPTS
    assert max(timer.started) == gv.ENC_SCENE_RETRY_MAX_MS


def test_retry_asks_again_only_for_an_open_demand(viewport_class):
    asked = []

    def ask():
        asked.append(1)

    state = SimpleNamespace(
        _disposed=False,
        _camera=CAMERA,
        _enc_scene_model=SimpleNamespace(state="idle"),
        _enc_scene_pending_generation=42,
        _enc_scene_retry_attempt=2,
        _enc_scene_retry_camera=CAMERA,
        _request_enc_scene=ask,
    )
    viewport_class._retry_enc_scene(state)
    assert asked == [1]

    # Already composing: a retry would supersede the job in flight.
    state._enc_scene_model.state = "loading"
    viewport_class._retry_enc_scene(state)
    # Nothing outstanding.
    state._enc_scene_model.state = "idle"
    state._enc_scene_pending_generation = None
    viewport_class._retry_enc_scene(state)
    assert asked == [1]


def test_a_superseded_camera_cancels_its_retries(viewport_class):
    asked = []
    timer = FakeTimer()
    state = SimpleNamespace(
        _disposed=False,
        _camera=FakeCamera(zoom=13.0),
        _enc_scene_model=SimpleNamespace(state="idle"),
        _enc_scene_pending_generation=43,
        _enc_scene_retry_attempt=2,
        _enc_scene_retry_camera=CAMERA,
        _enc_scene_retry_timer=timer,
        _request_enc_scene=lambda: asked.append(1),
    )
    viewport_class._retry_enc_scene(state)
    assert asked == []
    # The newer demand owns the schedule and starts its backoff over.
    assert state._enc_scene_retry_attempt == 0


def test_the_scene_is_asked_when_the_chart_never_composes(viewport_class):
    """Scene mode must not gate the scene on the raster chart's own request."""
    asked = []
    state = SimpleNamespace(
        _disposed=False,
        _chart_model=None,
        _camera=CAMERA,
        _chart_scene_deferral=None,
        _restart_chart_settle=lambda: None,
        _request_enc_scene=lambda: asked.append(1) or 7,
        _flush_held_enc_scene_adoption=lambda: None,
    )
    viewport_class._request_chart(state)
    assert asked == [1]
