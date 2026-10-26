"""Shared source selection, file interpretation and deterministic playback."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from gweatherrouting.core.datasource import NMEADataPacket
from gweatherrouting.core.nmea2000 import N2KMessage, instruments
from gweatherrouting.core.nmeasession import (
    NMEALog,
    NMEASession,
    read_nmea_log,
)
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

    def test_ripple_delete_preserves_nmea_timestamps_and_undoes(self):
        original = self.load([rmc("120000"), rmc("120010"), rmc("120020")])
        original_times = [entry[1].data.timestamp for entry in original.entries]
        original_numeric = {
            field.key: tuple(field.offsets) for field in original.numeric_series
        }
        points = original.points
        offsets = original.point_offsets

        self.assertTrue(self.session.delete_log_range(5, 15))
        edited = self.session.log
        self.assertEqual([offset for offset, _ in edited.entries], [0, 10])
        self.assertEqual(
            [packet.data.timestamp for _, packet in edited.entries],
            [original_times[0], original_times[2]],
        )
        self.assertEqual(edited.point_offsets, (offsets[0], offsets[2] - 10))
        self.assertEqual(edited.points, (points[0], points[2]))
        self.assertTrue(original_numeric)
        self.assertTrue(
            all(tuple(field.offsets) == (0, 10) for field in edited.numeric_series)
        )
        self.assertTrue(self.session.log_edits_dirty)

        self.assertTrue(self.session.undo_log_edit())
        self.assertIs(self.session.log, original)
        self.assertFalse(self.session.log_edits_dirty)

    def test_saved_edit_becomes_undo_baseline(self):
        self.load([rmc("120000"), rmc("120010"), rmc("120020")])
        self.assertTrue(self.session.delete_log_range(5, 15))
        self.assertTrue(self.session.mark_log_saved())
        self.assertFalse(self.session.log_edits_dirty)
        self.assertTrue(self.session.undo_log_edit())
        self.assertTrue(self.session.log_edits_dirty)

    def test_ripple_delete_can_remove_an_empty_interval(self):
        original = self.load([rmc("120000"), rmc("120010"), rmc("120020")])
        original_packets = [packet for _, packet in original.entries]
        self.assertTrue(self.session.delete_log_range(2, 5))
        self.assertEqual([offset for offset, _ in self.session.log.entries], [0, 7, 17])
        self.assertEqual(
            [packet for _, packet in self.session.log.entries], original_packets
        )
        self.assertEqual(self.session.log.duration, 17)

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

    def seek(self, position, limit=2):
        self.assertTrue(self.session.begin_seek(position))
        while not self.session.seek_batch(limit):
            pass

    def test_preview_suspends_replay_without_mutating_replay_state(self):
        self.load([rmc("120000"), water(), wind(), rmc("120010")])
        self.session.set_playing(True)
        self.session.advance(1)
        before = (
            self.session.position,
            self.session.cursor,
            self.session.active_history.snapshot(),
            list(self.received),
        )
        self.assertTrue(self.session.preview(8))
        self.session.advance(20)
        self.assertEqual(self.session.display_position, 8)
        self.assertTrue(self.session.playing)
        self.assertEqual(
            before,
            (
                self.session.position,
                self.session.cursor,
                self.session.active_history.snapshot(),
                self.received,
            ),
        )

    def test_seeks_reconstruct_prefix_history_and_reset_consumers_once(self):
        log = self.load(
            [rmc("120000"), water(), wind(), rmc("120005"), wind(315), rmc("120010")]
        )
        resets = []
        self.session.connect("reset", lambda _: resets.append(True))
        for target in (7, 1, 10, 0, 7):
            with self.subTest(target=target):
                self.received.clear()
                previous_resets = len(resets)
                self.seek(target)
                expected = [
                    packet for offset, packet in log.entries if offset <= target
                ]
                self.assertEqual(self.received, expected)
                self.assertEqual(len(resets), previous_resets + 1)
                self.assertEqual(self.session.cursor, len(expected))
                self.assertEqual(self.session.position, target)
                self.assertIsNone(self.session.preview_position)
                self.assertFalse(self.session.overview)
                reference = NMEASession(EventDispatcher(), self.recording_directory)
                try:
                    reference.finish_load(reference.begin_load(), log)
                    reference.set_playing(True)
                    reference.advance(target)
                    self.assertEqual(
                        self.session.active_history.snapshot()[1],
                        reference.active_history.snapshot()[1],
                    )
                finally:
                    reference.close()

    def test_seek_preserves_play_intent_except_at_eof(self):
        self.load([rmc("120000"), water(), wind(), rmc("120010")])
        for playing in (False, True):
            self.session.set_playing(playing)
            self.seek(5)
            self.assertEqual(self.session.playing, playing)
            self.session.advance(1)
            self.assertEqual(self.session.position, 6 if playing else 5)
        self.seek(100)
        self.assertEqual(self.session.position, 10)
        self.assertFalse(self.session.playing)
        self.seek(-1)
        self.assertEqual(self.session.position, 0)

    def test_million_entry_seek_is_bounded_and_cancellable(self):
        packet = NMEADataPacket(water())
        log = NMEALog("million.log", ((0, packet),) * 1000000, 0, True)
        self.session.finish_load(self.session.begin_load(), log)
        self.session.begin_seek(0)
        self.assertEqual(self.session.cursor, 0)
        self.assertFalse(self.session.seek_batch())
        self.assertEqual(self.session.cursor, 1000)
        self.assertFalse(self.session.seek_batch(7))
        self.assertEqual(self.session.cursor, 1007)
        self.session.select_source("live")
        self.assertTrue(self.session.seek_batch())
        self.assertEqual(self.session.cursor, 1007)
        self.assertIsNone(self.session.preview_position)

    def test_load_restart_and_close_cancel_pending_seek_and_preview(self):
        self.load([rmc("120000"), rmc("120010")])
        for cancel in (
            self.session.restart,
            self.session.begin_load,
            self.session.close,
        ):
            self.session.begin_seek(9)
            cancel()
            self.assertTrue(self.session.seek_batch())
            self.assertFalse(self.session.seeking)
            self.assertIsNone(self.session.preview_position)
            if self.session.loading:
                self.session.finish_load(self.session._generation)

    def test_seek_before_first_fix_does_not_consume_future_position(self):
        log = self.load([water(), wind(), rmc("120000"), rmc("120010")])
        self.seek(0)
        self.assertLess(self.session.position, log.point_offsets[0])
        self.assertEqual(self.received, [log.entries[0][1]])

    def test_rudder_and_engine_pgns_are_instrument_fields(self):
        from gweatherrouting.core.nmeasession import NumericFieldCollector

        now = datetime.now(timezone.utc)
        rudder = bytes([0]) + int(0.1 / 0.0001).to_bytes(2, "little", signed=True)
        rudder += int(-0.2 / 0.0001).to_bytes(2, "little", signed=True)
        rpm = bytes([0]) + int(1500 / 0.125).to_bytes(2, "little")
        oil_temp = bytes([0]) + (100).to_bytes(2, "little")
        oil_temp += int((85 + 273.15) / 0.125).to_bytes(2, "little", signed=True)
        oil_temp += int((90 + 273.15) / 0.125).to_bytes(2, "little", signed=True)
        oil_temp += (14 * 20).to_bytes(2, "little")
        oil_temp += (5000).to_bytes(2, "little")
        oil_temp += (100).to_bytes(4, "little")

        collector = NumericFieldCollector()
        for offset, pgn, data in (
            (0.0, 127245, rudder),
            (1.0, 127488, rpm),
            (2.0, 127489, oil_temp),
        ):
            packet = instruments(N2KMessage(now, 6, pgn, 255, 4, 0, data))[0]
            collector.add(offset, packet)

        fields = {field.key: field for field in collector.snapshot()}
        self.assertIn("RUD.position_deg", fields)
        self.assertIn("ENG.rpm", fields)
        self.assertIn("ENG.engine_temp_c", fields)
        self.assertAlmostEqual(fields["RUD.position_deg"].values[0], -11.5)
        self.assertAlmostEqual(fields["ENG.rpm"].values[0], 1500.0)
        self.assertAlmostEqual(fields["ENG.engine_temp_c"].values[0], 90.0)
