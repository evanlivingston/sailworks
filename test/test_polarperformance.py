"""Measured samples must use fresh, valid wind and an explicit speed basis."""

import tempfile
import unittest
from pathlib import Path

import pynmea2

from gweatherrouting.core.polarperformance import (
    PerformanceHistory,
    load_nmea_performance,
    wind_band,
)


def wind(angle=45, speed=12, units="N", reference="T", status="A"):
    return pynmea2.MWV("WI", "MWV", [str(angle), reference, str(speed), units, status])


def water(speed=6):
    return pynmea2.VHW("II", "VHW", ["", "T", "", "M", str(speed), "N", "", "K"])


def gps(speed=8, mode="A"):
    return pynmea2.VTG("GP", "VTG", ["", "T", "", "M", str(speed), "N", "", "K", mode])


class TestPolarPerformance(unittest.TestCase):
    def test_speed_bases_and_tacks_are_preserved(self):
        history = PerformanceHistory()
        history.ingest(water(), now=0)
        history.ingest(gps(), now=0)
        history.ingest(wind(angle=315), now=1)
        _, samples = history.snapshot()
        self.assertEqual(
            [(p.basis, p.speed) for p in samples], [("stw", 6), ("sog", 8)]
        )
        self.assertEqual([p.twa for p in samples], [315, 315])

    def test_missing_or_stale_speed_is_not_replaced_with_zero(self):
        history = PerformanceHistory()
        history.ingest(wind(), now=0)
        history.ingest(water(), now=1)
        history.ingest(wind(), now=7)
        self.assertEqual(history.snapshot()[1], ())

    def test_zero_water_speed_and_zero_wind_angle_are_valid(self):
        history = PerformanceHistory()
        history.ingest(water(0), now=0)
        history.ingest(wind(angle=0), now=1)
        sample = history.snapshot()[1][0]
        self.assertEqual((sample.speed, sample.twa), (0, 0))

    def test_apparent_invalid_or_nonfinite_wind_does_not_generate_points(self):
        history = PerformanceHistory()
        history.ingest(water(), now=0)
        for sentence in (
            wind(reference="R"),
            wind(status="V"),
            wind(speed="nan"),
            wind(speed="inf"),
            wind(speed=-1),
            wind(speed=0),
            wind(angle=361),
            wind(units="X"),
        ):
            history.ingest(sentence, now=1)
        self.assertEqual(history.snapshot()[1], ())

    def test_wind_unit_conversion(self):
        for units, speed in (("N", 10), ("M", 10 / 1.943844492), ("K", 18.52)):
            with self.subTest(units=units):
                history = PerformanceHistory()
                history.ingest(water(), now=0)
                history.ingest(wind(speed=speed, units=units), now=1)
                self.assertAlmostEqual(history.snapshot()[1][0].tws, 10)

    def test_invalid_speed_clears_previous_reading(self):
        history = PerformanceHistory()
        history.ingest(gps(), now=0)
        history.ingest(gps(mode="N"), now=1)
        history.ingest(water(), now=0)
        history.ingest(water("nan"), now=1)
        history.ingest(wind(), now=2)
        self.assertEqual(history.snapshot()[1], ())

    def test_rmc_validity(self):
        history = PerformanceHistory()
        sentence = pynmea2.parse("$GPRMC,120000,A,4807.038,N,01131.000,E,7,0,010126,,")
        history.ingest(sentence, now=0)
        history.ingest(wind(), now=1)
        self.assertEqual(history.snapshot()[1][0].basis, "sog")
        self.assertEqual(history.snapshot()[1][0].speed, 7)

    def test_throttle_bounded_history_and_clear(self):
        history = PerformanceHistory(capacity=2)
        history.ingest(water(), now=0)
        for now in (0, 0.5, 1, 2):
            history.ingest(wind(angle=now), now=now)
        self.assertEqual([p.twa for p in history.snapshot()[1]], [1, 2])
        history.clear()
        history.ingest(wind(), now=3)
        self.assertEqual(history.snapshot()[1], ())

    def test_wind_bands_and_range(self):
        self.assertEqual(wind_band(11, [6, 10, 14]), 1)
        self.assertEqual(wind_band(12.1, [6, 10, 14]), 2)
        self.assertEqual(wind_band(14, [6, 10, 14]), 2)
        self.assertIsNone(wind_band(15, [6, 10, 14]))
        self.assertIsNone(wind_band(5, [6, 10, 14]))
        self.assertIsNone(wind_band(10, []))

    def test_import_does_not_throttle_file_to_one_point(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.log"
            path.write_text(
                "\n".join(map(str, [water(), wind(), wind(angle=315), gps(), wind()]))
            )
            samples = load_nmea_performance(path)
            self.assertEqual(len(samples), 4)
            self.assertEqual(samples[1].twa, 315)

    def test_import_does_not_reuse_speed_across_long_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.log"
            path.write_text(str(water()) + "\n" + "garbage\n" * 51 + str(wind()))
            self.assertEqual(load_nmea_performance(path), ())
