# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
Offline tests for the PNW GRIB sources (NOAA HRRR, CMC HRDPS).
No network access: discovery uses an injected session double.
"""

import json
import unittest
from datetime import datetime, timezone

from gweatherrouting.core.gribsources.eccchrdps import ECCHRDPSource
from gweatherrouting.core.gribsources.noaahrrr import (
    HRRR_LONG_RANGE,
    HRRR_SHORT_RANGE,
    NOAAHRRRSource,
)


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, timeout=None):
        return FakeResponse(self.pages.get(url, ""))

    def close(self):
        pass


class TestNOAAHRRRSource(unittest.TestCase):
    def test_name(self):
        self.assertEqual(NOAAHRRRSource().name, "NOAA HRRR")

    def test_bounds_setter(self):
        source = NOAAHRRRSource()
        source.bounds = (46.0, -130.0, 50.0, -122.0)
        self.assertEqual(source.bounds, (46.0, -130.0, 50.0, -122.0))

    def test_cycle_range_long_vs_short(self):
        source = NOAAHRRRSource()
        long_cycle = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
        short_cycle = datetime(2026, 10, 15, 3, tzinfo=timezone.utc)
        self.assertEqual(source._max_hours(long_cycle), HRRR_LONG_RANGE)
        self.assertEqual(source._max_hours(short_cycle), HRRR_SHORT_RANGE)

    def test_download_list_respects_cycle_cap(self):
        source = NOAAHRRRSource()
        # Force a short (18h) cycle: 48h preset must be absent.
        source._get_available_cycles = lambda n=4: [
            datetime(2026, 10, 15, 3, tzinfo=timezone.utc)
        ]
        entries = source.get_download_list()
        self.assertTrue(entries)
        for entry in entries:
            config = json.loads(entry[4])
            self.assertLessEqual(config["hours"], HRRR_SHORT_RANGE)
            self.assertEqual(config["source"], "NOAA HRRR")

    def test_build_filter_url_uses_mslma(self):
        # HRRR pressure is MSLMA, not PRMSL (NOMADS 500s on PRMSL).
        source = NOAAHRRRSource()
        source.bounds = (47.0, -126.0, 50.0, -122.0)
        params = source._build_filter_url("20261015", "03", 1)
        self.assertEqual(params["file"], "hrrr.t03z.wrfsfcf01.grib2")
        self.assertEqual(params["dir"], "/hrrr.20261015/conus")
        self.assertEqual(params["var_MSLMA"], "on")
        self.assertNotIn("var_PRMSL", params)
        self.assertEqual(params["leftlon"], "-126.0")

    def test_identifier_round_trip(self):
        source = NOAAHRRRSource()
        identifier = json.dumps(
            {"source": "NOAA HRRR", "date": "20261015", "cycle": "03", "hours": 12}
        )
        self.assertEqual(source._source_tag(identifier), "NOAA HRRR")


class TestECCHRDPSource(unittest.TestCase):
    DATE = "20261015"
    LISTING = (
        f'href="{DATE}T12Z_MSC_HRDPS_UGRD_AGL-10m_RLatLon0.0225_PT000H.grib2" '
        f'href="{DATE}T12Z_MSC_HRDPS_VGRD_AGL-10m_RLatLon0.0225_PT000H.grib2"'
    )

    def _session(self):
        return FakeSession(
            {
                "https://dd.weather.gc.ca/": 'href="20261015/" href="20261014/"',
                f"https://dd.weather.gc.ca/{self.DATE}/WXO-DD/model_hrdps/"
                "continental/2.5km/": 'href="00/" href="12/"',
                f"https://dd.weather.gc.ca/{self.DATE}/WXO-DD/model_hrdps/"
                "continental/2.5km/12/000/": self.LISTING,
                f"https://dd.weather.gc.ca/{self.DATE}/WXO-DD/model_hrdps/"
                "continental/2.5km/00/000/": "",
            }
        )

    def test_name(self):
        self.assertEqual(ECCHRDPSource().name, "CMC HRDPS")

    def test_discover_newest_complete_cycle(self):
        source = ECCHRDPSource(session=self._session())
        self.assertEqual(source._discover(), (self.DATE, "12"))

    def test_download_list_entries(self):
        source = ECCHRDPSource(session=self._session())
        entries = source.get_download_list()
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(len(entry), 5)
            config = json.loads(entry[4])
            self.assertEqual(config["source"], "CMC HRDPS")
            self.assertEqual(config["cycle"], "12")

    def test_file_url_scheme(self):
        source = ECCHRDPSource()
        url = source._file_url(self.DATE, "00", 6, "UGRD")
        self.assertEqual(
            url,
            f"https://dd.weather.gc.ca/{self.DATE}/WXO-DD/model_hrdps/"
            "continental/2.5km/00/006/"
            f"{self.DATE}T00Z_MSC_HRDPS_UGRD_AGL-10m_RLatLon0.0225_PT006H.grib2",
        )
        pressure = source._file_url(self.DATE, "00", 6, "PRMSL")
        self.assertIn("PRMSL_MSL_RLatLon0.0225_PT006H.grib2", pressure)

    def test_estimate_size_labels_megabytes(self):
        size = ECCHRDPSource._estimate_size(48)
        self.assertIn("MB", size)


if __name__ == "__main__":
    unittest.main()
