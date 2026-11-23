"""Headless contract for the staged layer deadlines (#261).

Each staged layer owns one restartable single-shot timer.  A rapid burst of
pans re-arms that timer instead of queueing an unparented
``QTimer.singleShot`` per request, so at most one deadline is pending per
layer, the armed token is always the latest one, and a superseded token can
never be failed by a late deadline.  Wave fill and wave barbs can be pending
together, so they keep independent deadlines.
"""

import inspect
import re

import pytest
from PySide6.QtCore import QObject, QTimer

from sailworks.qt import gribviewport
from test.test_image_fill_transaction import Scene, stage

TIMER_SPECS = (
    (
        "_image_fill_timeout_timer",
        "_image_fill_timeout_token",
        "_image_fill_timeout_tick",
    ),
    ("_wave_timeout_timer", "_wave_timeout_token", "_wave_timeout_tick"),
    (
        "_wave_barbs_timeout_timer",
        "_wave_barbs_timeout_token",
        "_wave_barbs_timeout_tick",
    ),
)


@pytest.fixture(scope="module", autouse=True)
def _qt_app():
    """Timer activation needs an application; reuse any earlier one.

    This shares a process with widget tests, which need a widget-capable
    ``QApplication``; a bare ``QCoreApplication`` could not be upgraded, so
    install the same one those modules create.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TimerScene(Scene):
    """A Scene whose staged deadlines are real, parented Qt timers.

    ``Scene`` binds the real viewport methods without constructing a
    ``QQuickWidget``, so the timers live on a plain ``QObject`` owner here.
    """

    def __init__(self):
        super().__init__()
        self._timer_owner = QObject()
        for timer_attr, token_attr, tick in TIMER_SPECS:
            timer = QTimer(self._timer_owner)
            timer.setSingleShot(True)
            timer.setInterval(10000)
            timer.timeout.connect(getattr(self, tick))
            setattr(self, timer_attr, timer)
            setattr(self, token_attr, None)


def test_rapid_staging_leaves_one_image_fill_deadline():
    scene = TimerScene()
    tokens = [stage(scene, generation) for generation in range(1, 51)]
    assert all(tokens), "every rapid stage must hand back a token"
    timer = scene._image_fill_timeout_timer
    assert timer.isActive()
    assert timer.isSingleShot()
    assert timer.remainingTime() > 0
    assert scene._image_fill_timeout_token is tokens[-1]
    assert scene._image_fill_token is tokens[-1]
    # One deadline, not fifty: the layer owns a single timer and starting it
    # again replaces the previous deadline.
    timers = scene._timer_owner.findChildren(QTimer)
    assert len(timers) == 3
    assert [item for item in timers if item.isActive()] == [timer]


def test_stale_token_delivery_does_not_fail_the_newer_request():
    scene = TimerScene()
    stale = stage(scene, 1)
    current = stage(scene, 2)
    assert current is not stale
    assert scene._image_fill_timeout_token is current
    scene._image_fill_timeout(stale)
    assert scene._state != "unavailable"
    assert scene._publication_manager.layers["fill"].pending is not None
    assert scene._image_fill_token is current
    assert scene._image_fill_timeout_token is current
    assert scene._image_fill_timeout_timer.isActive()


def test_latest_deadline_cancels_the_pending_fill_once():
    scene = TimerScene()
    token = stage(scene, 1)
    assert scene._image_fill_timeout_token is token
    scene._image_fill_timeout_tick()
    assert scene._state == "unavailable"
    assert scene._publication_manager.layers["fill"].pending is None
    assert scene._image_fill_token is None
    assert scene._image_fill_timeout_token is None
    assert not scene._image_fill_timeout_timer.isActive()
    # Re-delivering the same deadline is a no-op: the armed token was cleared
    # before the handler ran, so no second cancellation can happen.
    scene._image_fill_timeout_tick()
    assert scene._image_fill_token is None
    assert not scene._image_fill_timeout_timer.isActive()
    # A newer field still arms a fresh deadline.
    replacement = stage(scene, 3)
    assert scene._image_fill_timeout_token is replacement
    assert scene._image_fill_timeout_timer.isActive()


def test_cancelling_the_fill_stops_the_deadline():
    scene = TimerScene()
    stage(scene, 1)
    assert scene._image_fill_timeout_timer.isActive()
    scene._cancel_image_fill()
    assert not scene._image_fill_timeout_timer.isActive()
    assert scene._image_fill_timeout_token is None


def test_commit_stops_the_deadline():
    scene = TimerScene()
    token = stage(scene, 1)
    assert scene._image_fill_timeout_timer.isActive()
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(token)
    assert scene._image_fill_token is None
    assert not scene._image_fill_timeout_timer.isActive()
    assert scene._image_fill_timeout_token is None


def test_wave_and_wave_barbs_deadlines_are_independent():
    scene = TimerScene()
    wave = object()
    barbs = object()
    gribviewport._arm_staged_timeout(
        scene, "_wave_timeout_timer", "_wave_timeout_token", wave
    )
    gribviewport._arm_staged_timeout(
        scene, "_wave_barbs_timeout_timer", "_wave_barbs_timeout_token", barbs
    )
    assert scene._wave_timeout_token is wave
    assert scene._wave_barbs_timeout_token is barbs
    gribviewport._stop_staged_timeout(
        scene, "_wave_timeout_timer", "_wave_timeout_token"
    )
    assert not scene._wave_timeout_timer.isActive()
    assert scene._wave_barbs_timeout_token is barbs
    assert scene._wave_barbs_timeout_timer.isActive()
    scene._wave_barbs_timeout_tick()
    assert scene._wave_barbs_timeout_token is None
    assert not scene._wave_barbs_timeout_timer.isActive()
    assert not scene._image_fill_timeout_timer.isActive()


def test_staged_paths_do_not_arm_unparented_timers(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("staged fills must not arm unparented timers")

    try:
        monkeypatch.setattr(QTimer, "singleShot", staticmethod(forbidden))
    except (AttributeError, TypeError):
        # Shiboken types reject attribute assignment; enforce the same
        # invariant on the source instead: no staged deadline may be a
        # 10000 ms unparented single-shot holding a completion token.
        source = inspect.getsource(gribviewport._viewport_widget_class)
        assert not re.search(r"QTimer\.singleShot\(\s*10000", source)
        assert "staged=token" not in source
        return

    scene = TimerScene()
    token = stage(scene, 1)
    scene._fill_back.properties["prepared"] = True
    scene._try_commit_image_fill(token)
    scene._image_fill_timeout_tick()
    assert calls == []
