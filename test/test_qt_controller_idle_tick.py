"""Controller tick skips session.advance unless log playback is running."""

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from sailworks.qt.controller import SessionController  # noqa: E402


def _host(playing, source, elapsed=5.0):
    from unittest.mock import Mock

    session = Mock()
    session.playing = playing
    session.source = source
    return SimpleNamespace(session=session, last_tick=time.monotonic() - elapsed)


def test_tick_does_not_advance_when_not_playing():
    host = _host(False, "log")
    before = time.monotonic()

    SessionController._tick(host)

    host.session.advance.assert_not_called()
    assert host.last_tick >= before


def test_tick_does_not_advance_for_live_source():
    host = _host(True, "live")
    before = time.monotonic()

    SessionController._tick(host)

    host.session.advance.assert_not_called()
    assert host.last_tick >= before


def test_tick_advances_while_log_playback_runs():
    host = _host(True, "log", elapsed=0.25)
    before = time.monotonic()

    SessionController._tick(host)

    host.session.advance.assert_called_once()
    elapsed = host.session.advance.call_args.args[0]
    assert elapsed >= 0.25
    assert host.last_tick >= before
