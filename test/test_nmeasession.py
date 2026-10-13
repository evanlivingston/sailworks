"""Shared source selection, file interpretation and deterministic playback."""

import tempfile
import unittest
from pathlib import Path

from gweatherrouting.core.datasource import NMEADataPacket
from gweatherrouting.core.nmeasession import NMEALog, NMEASession, read_nmea_log
from gweatherrouting.core.utils import EventDispatcher
from test.test_polarperformance import water, wind


def rmc(stamp):
    return f"$GPRMC,{stamp},A,4807.038,N,01131.000,E,7,0,010126,,"


class TestNMEASession(unittest.TestCase):
    def setUp(self):
        self.connections = EventDispatcher()
        self.session = NMEASession(self.connections)
        self.received = []
        self.session.connect("data", self.received.extend)
        self.addCleanup(self.session.close)

    def load(self, lines):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.log"
            path.write_text("\n".join(map(str, lines)))
            log = read_nmea_log(path)
        token = self.session.begin_load()
        self.session.finish_load(token, log)
        return log

    def test_overview_then_play_pause_resume_eof_and_restart(self):
        log = self.load(
            [rmc("120000"), water(), wind(), rmc("120002"), water(), wind(315)]
        )
        self.assertEqual(log.duration, 2)
        self.assertEqual(len(log.points), 2)
        overview = self.session.active_history.snapshot()[1]
        self.assertEqual(len(overview), 4)
        self.assertEqual(self.received, [])
        self.session.set_playing(True)
        self.assertEqual(self.session.active_history.snapshot()[1], ())
        self.session.advance(0.5)
        self.assertEqual(len(self.received), 3)
        self.session.set_playing(False)
        self.session.advance(60)
        self.assertEqual(len(self.received), 3)
        self.session.set_playing(True)
        self.session.speed = 2
        self.session.advance(0.75)
        self.assertFalse(self.session.playing)
        self.assertEqual(len(self.received), 6)
        self.assertEqual(self.session.active_history.snapshot()[1], overview)
        self.session.set_playing(True)
        self.session.advance(0)
        self.assertEqual(len(self.received), 9)

    def test_live_pause_and_replay_do_not_mix_sources(self):
        packets = [NMEADataPacket(s) for s in (water(), wind())]
        self.connections.dispatch("data", packets)
        self.assertEqual(len(self.session.history.snapshot()[1]), 1)
        self.session.set_streaming(False)
        self.connections.dispatch("data", packets)
        self.assertEqual(len(self.received), 2)
        self.load([water(), wind()])
        self.session.set_streaming(True)
        self.connections.dispatch("data", packets)
        self.assertEqual(len(self.received), 2)
        self.session.set_playing(True)
        self.session.advance(1)
        self.assertEqual(len(self.received), 4)
        self.session.select_source("live")
        self.connections.dispatch("data", packets)
        self.assertEqual(len(self.received), 6)

    def test_midnight_and_untimestamped_fallback(self):
        log = self.load([rmc("235959"), water(), rmc("000001")])
        self.assertTrue(log.timestamped)
        self.assertEqual(log.duration, 2)
        log = self.load([water(), wind(), wind(315)])
        self.assertFalse(log.timestamped)
        self.assertEqual(log.duration, 0.2)

    def test_failed_and_stale_loads_preserve_previous_log(self):
        old = self.load([water(), wind()])
        first = self.session.begin_load()
        second = self.session.begin_load()
        self.session.finish_load(first, NMEALog("stale.log", (), 0, False))
        self.assertTrue(self.session.loading)
        self.session.finish_load(second, error=OSError("not found"))
        self.assertIs(self.session.log, old)
        self.assertFalse(self.session.loading)
        self.assertEqual(self.session.error, "not found")

    def test_invalid_sentences_do_not_make_speed_fresh(self):
        log = self.load([water()] + ["garbage"] * 51 + [wind()])
        self.assertEqual(log.samples, ())
        self.assertEqual(log.skipped, 51)
        self.session.set_playing(True)
        self.session.advance(100)
        self.assertEqual(self.session.active_history.snapshot()[1], ())

    def test_empty_and_raw_logs_report_useful_error(self):
        with self.assertRaisesRegex(ValueError, "interpreted NMEA"):
            self.load(["garbage"])

    def test_dense_log_drains_without_dropping_packets(self):
        packet = NMEADataPacket(water())
        log = NMEALog("dense.log", ((0, packet),) * 2100, 0, True)
        self.session.finish_load(self.session.begin_load(), log)
        self.session.set_playing(True)
        self.session.advance(1)
        self.assertEqual(len(self.received), 1000)
        self.assertTrue(self.session.playing)
        self.session.advance(0)
        self.session.advance(0)
        self.assertEqual(len(self.received), 2100)
        self.assertFalse(self.session.playing)
