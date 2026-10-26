"""Coverage screening for a published GRIB-backed model source."""

import unittest
from datetime import timedelta

from gweatherrouting.core.grib import Grib
from gweatherrouting.qt.weathercoverage import coverage_text
from test import test_grib_sampling as sampling_fixture
from test.test_grib_sampling import START


class PublishedWeatherCoverageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sampling_fixture.TestGribSampling()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_view_notice_distinguishes_space_time_model_and_field_gaps(self):
        raw = self.fixture.file(
            self.fixture.field(0) + self.fixture.field(3), process=96
        )
        source = Grib.parse(raw.path)
        self.addCleanup(source.close)
        models, fields = ("GFS",), ("wind", "pressure")
        readers = {"GFS": [source]}
        self.assertEqual(
            coverage_text(models, fields, START, [(0.5, 0.5)], readers), ""
        )
        outside = coverage_text(models, fields, START, [(40, 40)], readers)
        self.assertIn("wind unavailable", outside)
        self.assertIn("pressure unavailable", outside)
        partial = coverage_text(
            models, fields, START, [(0.5, 0.5), (40, 40)], readers
        )
        self.assertIn("wind partial", partial)
        expired = coverage_text(
            models, fields, START + timedelta(hours=4), [(0.5, 0.5)], readers
        )
        self.assertIn("unavailable", expired)
        other_model = coverage_text(
            ("HRRR",), ("wind",), START, [(0.5, 0.5)], readers
        )
        self.assertEqual(other_model, "HRRR: wind unavailable")
