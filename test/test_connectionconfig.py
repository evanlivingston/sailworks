"""Connection settings validate before interrupting instruments."""

import unittest
from unittest.mock import MagicMock, patch

from gweatherrouting.core.connectionconfig import connection_config
from gweatherrouting.core.connectionmanager import ConnectionManager


def network(**updates):
    return dict(
        type="network",
        protocol="nmea0183",
        direction="in",
        network="tcp",
        host="localhost",
        port=10110,
        **updates,
    )


class TestConnectionConfig(unittest.TestCase):
    def test_legacy_n2k_and_serial_port_normalization(self):
        config = connection_config(
            dict(
                type="serial",
                protocol="n2k_ascii",
                direction="in",
                **{"data-port": " /dev/test ", "baudrate": "115200"},
            )
        )
        self.assertEqual(config["format"], "n2k_ascii")
        self.assertEqual(config["protocol"], "nmea2000")
        self.assertEqual(config["data-port"], "/dev/test")
        self.assertEqual(config["baudrate"], 115200)

    def test_invalid_endpoints_and_n2k_output(self):
        for updates in (
            {"host": " "},
            {"port": 0},
            {"port": 65536},
            {"network": "other"},
            {"protocol": "other"},
            {"protocol": "nmea2000", "direction": "both"},
        ):
            config = network()
            config.update(updates)
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                connection_config(config)

    def manager(self):
        with (
            patch("gweatherrouting.core.storage.Storage.load_or_save_default"),
            patch("gweatherrouting.core.storage.Storage.save"),
        ):
            manager = ConnectionManager()
        manager.storage = MagicMock()
        manager.storage.connections = [network()]
        return manager

    def test_offline_edit_saves_without_connecting(self):
        manager = self.manager()
        config = network()
        config["port"] = 20220
        with patch.object(manager, "plug_all") as plug:
            manager.replace_connections([config])
        self.assertEqual(manager.connections, [config])
        manager.storage.save.assert_called_once()
        plug.assert_not_called()
        self.assertFalse(manager.polling_active)

    def test_duplicate_rejected_before_disconnect(self):
        manager = self.manager()
        with (
            patch.object(manager, "stop_polling") as stop,
            self.assertRaises(ValueError),
        ):
            manager.replace_connections([network(), network()])
        stop.assert_not_called()
        manager.storage.save.assert_not_called()

    def test_failed_save_restores_previous_configuration_and_polling(self):
        manager = self.manager()
        previous = list(manager.connections)
        manager._polling_active = True
        manager.storage.save.side_effect = OSError("disk full")
        with (
            patch.object(manager, "stop_polling"),
            patch.object(manager, "start_polling") as start,
        ):
            with self.assertRaises(OSError):
                manager.replace_connections([])
        self.assertEqual(manager.connections, previous)
        start.assert_called_once()
        manager._polling_active = False

    def test_live_edit_stops_then_restarts(self):
        manager = self.manager()
        manager._polling_active = True
        with (
            patch.object(manager, "stop_polling") as stop,
            patch.object(manager, "start_polling") as start,
        ):
            manager.replace_connections([])
        stop.assert_called_once()
        start.assert_called_once()
        self.assertEqual(manager.connections, [])
        manager._polling_active = False

    def test_status_snapshot_remains_available_while_reconnecting(self):
        manager = self.manager()
        source = MagicMock(connected=True)
        manager.sources = {"localhost:10110": source}
        expected = manager.get_status()
        manager._sources_lock.acquire()
        try:
            self.assertEqual(manager.get_status(), expected)
        finally:
            manager._sources_lock.release()
