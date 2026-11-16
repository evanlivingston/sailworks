"""Dock persistence and shutdown flush for the Qt workspace preferences."""

import os
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sailworks.core.workspacestate import WorkspaceStateStore
from sailworks.qt.workspace import (
    WorkspacePreferences,
    dock_state,
    restore_docks,
)


class FakeDock:
    def __init__(self, visible=False, floating=False):
        self.visible = visible
        self.floating = floating
        self.calls = []

    def isVisible(self):  # noqa: N802 - Qt method name
        return self.visible

    def isFloating(self):  # noqa: N802 - Qt method name
        return self.floating

    def setVisible(self, visible):  # noqa: N802 - Qt method name
        self.calls.append(("visible", visible))
        self.visible = visible

    def setFloating(self, floating):  # noqa: N802 - Qt method name
        self.calls.append(("floating", floating))
        self.floating = floating


def fake_window(dock=None):
    window = SimpleNamespace()
    if dock is not None:
        window.routing_inspector_dock = dock
    return window


class FakeTimer:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


class TestDockState(unittest.TestCase):
    def test_visibility_and_floating_roundtrip(self):
        dock = FakeDock(visible=True, floating=True)
        state = {"docks": dock_state(fake_window(dock))}
        self.assertEqual(
            state, {"docks": {"routing_inspector": {"visible": True, "floating": True}}}
        )

        target = FakeDock(visible=False, floating=False)
        restore_docks(fake_window(target), state["docks"])
        self.assertTrue(target.visible)
        self.assertTrue(target.floating)
        self.assertEqual(target.calls, [("floating", True), ("visible", True)])

        target.floating = False
        target.visible = False
        restore_docks(
            fake_window(target),
            {"routing_inspector": {"visible": False, "floating": False}},
        )
        self.assertFalse(target.visible)
        self.assertFalse(target.floating)

    def test_missing_or_invalid_docks_leave_the_dock_hidden(self):
        for saved in (
            None,
            {},
            [],
            "bad",
            {"routing_inspector": None},
            {"routing_inspector": {"visible": "yes", "floating": 1}},
        ):
            dock = FakeDock(visible=False, floating=False)
            restore_docks(fake_window(dock), saved)
            self.assertFalse(dock.visible, saved)
            self.assertEqual(dock.calls, [], saved)

    def test_window_without_a_dock_is_ignored(self):
        self.assertEqual(dock_state(fake_window()), {})
        restore_docks(fake_window(), {"routing_inspector": {"visible": True}})


class TestCloseFlush(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "workspace-qt.json"
        self.prefs = SimpleNamespace(
            window=SimpleNamespace(),
            store=WorkspaceStateStore(self.path),
            closed=False,
            restoring=False,
            error="",
            last_saved=None,
            timer=FakeTimer(),
            state={
                "docks": {"routing_inspector": {"visible": True, "floating": False}}
            },
        )
        self.prefs.save_state = lambda: self.prefs.state
        self.prefs.save = MethodType(WorkspacePreferences.save, self.prefs)
        self.prefs.close = MethodType(WorkspacePreferences.close, self.prefs)

    def test_close_flushes_the_pending_change(self):
        # A layout change landed inside the 750 ms debounce window.
        self.prefs.state = {
            "docks": {"routing_inspector": {"visible": True, "floating": True}}
        }
        self.prefs.close()
        self.assertEqual(self.prefs.timer.stopped, 1)
        self.assertTrue(self.prefs.closed)
        self.assertEqual(
            self.prefs.store.load(),
            {"docks": {"routing_inspector": {"visible": True, "floating": True}}},
        )
        self.assertEqual(self.prefs.store.load(), self.prefs.state)

        # After closing, later changes are not written.
        self.prefs.state = {
            "docks": {"routing_inspector": {"visible": False, "floating": False}}
        }
        self.prefs.save()
        self.assertEqual(
            self.prefs.store.load(),
            {"docks": {"routing_inspector": {"visible": True, "floating": True}}},
        )

    def test_save_is_suppressed_while_restoring(self):
        self.prefs.restoring = True
        self.prefs.save()
        self.assertFalse(self.path.exists())
        self.prefs.restoring = False
        self.prefs.save()
        self.assertEqual(self.prefs.store.load(), self.prefs.state)


if __name__ == "__main__":
    unittest.main()
