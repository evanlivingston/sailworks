"""The settled raster chart waits for the ENC scene's own publication.

A settled chart request asks the scene layer for the same camera first.  The
scene publishes that view a second or so later; composing the raster chart in
the same tick duplicates it and costs seconds.  These tests cover the pure
deferral decision and the bounded fallback around the widget methods that use
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sailworks.qt.gribviewport import (
    CHART_SCENE_DEFER_MS,
    _viewport_widget_class,
    apply_scene_publication,
    defer_chart_for_scene,
    plan_scene_deferral,
    resolve_scene_deferral,
    scene_chart_deferral_live,
    scene_chart_deferral_pending,
    scene_draws_chart,
)


@dataclass(frozen=True)
class FakeCamera:
    center_lon: float = -122.4
    center_lat: float = 37.8
    zoom: float = 14.0
    width: int = 1000
    height: int = 720


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
    assert (action, record) == ("compose", None)
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


def test_camera_change_replaces_the_deferral():
    other = FakeCamera(center_lon=-121.0)
    action, record = plan_scene_deferral(
        pending=_deferral(camera=CAMERA),
        camera=other,
        generation=50,
        published=(41, object(), {}),
        now=NOW + 0.2,
        timeout_ms=CHART_SCENE_DEFER_MS,
    )
    assert action == "wait"
    assert record["camera"] == other
    assert record["deadline"] == pytest.approx(NOW + 0.2 + 1.5)


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


# ---- widget-side wait and release ---------------------------------------


def test_widget_defers_and_arms_bounded_timer(viewport_class):
    timer = FakeTimer()
    state = SimpleNamespace(
        _enc_scene_model=SimpleNamespace(_published=(41, None, {})),
        _camera=CAMERA,
        _chart_timer=timer,
        _chart_scene_deferral=None,
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
    )
    assert defer_chart_for_scene(state, 43, NOW + 0.1) is True
    assert state._chart_scene_deferral["camera"] == other
    assert state._chart_scene_deferral["generation"] == 43
    assert not scene_chart_deferral_live(state._chart_scene_deferral, CAMERA, NOW + 0.2)


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
    )
    viewport_class._request_chart(state)
    assert asked == [1]
