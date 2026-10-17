"""Core notification scheduling is injected at the UI boundary."""

import unittest
from unittest.mock import patch

from gweatherrouting.core import Core
from gweatherrouting.core.datasource import NMEADataPacket
from test.test_nmeasession import rmc


class TestCoreScheduling(unittest.TestCase):
    def setUp(self):
        for target in (
            "gweatherrouting.core.storage.Storage.load_or_save_default",
            "gweatherrouting.core.storage.Storage.save",
            "gweatherrouting.core.gribmanager.GribManager.refresh_local_gribs",
            "gweatherrouting.core.polarmanager.PolarManager.refresh_local_polars",
        ):
            mock = patch(target)
            mock.start()
            self.addCleanup(mock.stop)
        self.queued = []
        with patch(
            "gweatherrouting.core.connectionmanager.ConnectionManager.plug_all"
        ) as plug:
            with patch(
                "gweatherrouting.core.connectionmanager.ConnectionManager.start_polling"
            ) as start:
                self.core = Core(
                    schedule=lambda callback, *args: self.queued.append(
                        (callback, args)
                    ),
                    start_connections=False,
                )
            start.assert_not_called()
        plug.assert_not_called()
        self.addCleanup(self.core.nmea_session.close)

    def drain(self):
        for callback, args in self.queued:
            callback(*args)
        self.queued.clear()

    def test_frontend_controls_notification_delivery(self):
        events = []
        self.core.connect("boat_position", lambda boat: events.append(boat.latitude))
        self.core.connectionManager.dispatch(
            "data", [NMEADataPacket.parse(str(rmc("120000")))]
        )
        self.assertFalse(events)
        self.drain()
        self.assertEqual(events, [48.1173])

    def test_source_reset_discards_queued_positions(self):
        events = []
        self.core.connect("boat_position", events.append)
        self.core.connectionManager.dispatch(
            "data", [NMEADataPacket.parse(str(rmc("120000")))]
        )
        self.core._reset_nmea(None)
        self.drain()
        self.assertFalse(events)


class TestDefaultScheduling(unittest.TestCase):
    def test_default_delivers_synchronously_without_importing_gi(self):
        import sys
        from gweatherrouting.core.scheduling import schedule_idle

        with patch.dict(sys.modules, {"gi": None, "gi.repository": None}):
            calls = []
            result = schedule_idle(lambda value: calls.append(value) or 42, "event")
        self.assertEqual(calls, ["event"])
        self.assertEqual(result, 42)
