"""Shared source selection, file interpretation and deterministic playback."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gweatherrouting.core.datasource import NMEADataPacket
from gweatherrouting.core.nmeasession import NMEALog, NMEASession, read_nmea_log
from gweatherrouting.core.utils import EventDispatcher
from test.test_polarperformance import water, wind


def rmc(stamp):
    return f"$GPRMC,{stamp},A,4807.038,N,01131.000,E,7,0,010126,,"


class TestNMEASession(unittest.TestCase):
    def setUp(self):
        self.connections = EventDispatcher()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.recording_directory = Path(directory.name) / "recordings"
        self.session = NMEASession(self.connections, self.recording_directory)
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

    def test_recording_is_saved_continuously_and_survives_stop_and_close(self):
        from types import SimpleNamespace

        session = self.session
        self.assertIsNone(session.recording_path)
        session.start_recording()
        path = session.recording_path
        self.assertEqual(path.parent, self.recording_directory)
        self.connections.dispatch("data", [NMEADataPacket(water())])
        self.connections.dispatch(
            "nmea2000", [SimpleNamespace(to_canboat=lambda: "raw PGN")]
        )
        # Both files are readable before stopping or closing the recorder.
        self.assertEqual(len(read_nmea_log(path).entries), 1)
        raw = Path(str(path) + ".n2k.log")
        self.assertEqual(raw.read_text(), "raw PGN\n")
        session.stop_recording()
        original = path.read_bytes()
        session.start_recording()
        self.assertNotEqual(session.recording_path, path)
        self.assertFalse(session.recorder.raw_path.exists())
        self.connections.dispatch("data", [NMEADataPacket(wind())])
        second = session.recording_path
        session.close()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(raw.read_text(), "raw PGN\n")
        self.assertEqual(len(read_nmea_log(second).entries), 1)

    def test_timestamp_collisions_preserve_existing_files_and_raw_archives(self):
        from datetime import datetime, timezone

        stamp = datetime(2026, 10, 13, 12, 34, 56, 123456, tzinfo=timezone.utc)
        self.recording_directory.mkdir()
        stem = "nmea-20261013T123456.123456Z"
        orphan_raw = self.recording_directory / f"{stem}.log.n2k.log"
        orphan_raw.write_text("existing raw archive")
        with patch("gweatherrouting.core.nmeasession.datetime") as clock:
            clock.now.return_value = stamp
            self.session.start_recording()
            first = self.session.recording_path
            self.assertEqual(first.name, f"{stem}-1.log")
            self.connections.dispatch("data", [NMEADataPacket(water())])
            original = first.read_bytes()
            self.session.stop()
            self.session.start_recording()
            self.assertEqual(self.session.recording_path.name, f"{stem}-2.log")
            self.assertEqual(first.read_bytes(), original)
            self.assertEqual(orphan_raw.read_text(), "existing raw archive")

    def test_recording_directory_error_does_not_start_or_clear_data(self):
        from unittest.mock import Mock

        self.recording_directory.write_text("not a directory")
        reset = Mock()
        self.session.connect("reset", reset)
        with self.assertRaises(OSError):
            self.session.start_recording()
        self.assertFalse(self.session.recording)
        self.assertIsNone(self.session.recording_path)
        reset.assert_not_called()

    def test_source_changes_pause_and_loading_stop_recording(self):
        for change in (
            lambda: self.session.select_source("log"),
            lambda: self.session.set_streaming(False),
            self.session.begin_load,
        ):
            with self.subTest(change=change):
                self.session.select_source("live")
                self.session.set_streaming(True)
                self.session.start_recording()
                self.assertTrue(self.session.recording)
                change()
                self.assertFalse(self.session.recording)
                self.session.start_recording()
                self.assertFalse(self.session.recording)
                if self.session.loading:
                    self.session.finish_load(self.session._generation)

    def test_close_stops_recording_and_disconnects_raw_capture(self):
        self.session.start_recording()
        path = self.session.recorder.path
        self.session.close()
        self.assertFalse(self.session.recording)
        self.assertTrue(path.exists())
        self.connections.dispatch("nmea2000", [object()])

    def test_stop_replay_preserves_position_samples_and_can_resume(self):
        self.load([rmc("120000"), water(), wind(), rmc("120010")])
        self.session.set_playing(True)
        self.session.advance(2)
        history = self.session.active_history.snapshot()[1]
        cursor = self.session.cursor
        self.session.stop()
        self.assertFalse(self.session.playing)
        self.assertEqual(self.session.position, 2)
        self.assertEqual(self.session.cursor, cursor)
        self.assertEqual(self.session.active_history.snapshot()[1], history)
        self.session.set_playing(True)
        self.session.advance(1)
        self.assertEqual(self.session.position, 3)

    def test_track_point_offsets_follow_playback_timing_with_sensor_preamble(self):
        log = self.load([water(), wind(), rmc("120000"), rmc("120010")])
        self.assertEqual(len(log.points), 2)
        self.assertEqual(
            log.point_offsets, tuple(offset for offset, packet in log.entries[2:])
        )
        self.assertGreater(log.point_offsets[0], 0)
