# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

For detail about GNU see <http://www.gnu.org/licenses/>.
"""

from sailworks.core.utils import Storage


class SettingsManager(Storage):
    def __init__(self):
        Storage.__init__(self, "settings")

        self.gribArrowOpacity = 0.4
        self.gribArrowOnGround = False
        self.gribBarbDensity = "standard"
        self.marineCurrentDensity = "standard"
        self.gribWindStyle = "barbs"
        self.gribWindGradient = True
        self.gribWindGradientOpacity = 0.12
        self.gribWindGradientSmooth = True
        self.gribWindGradientLevels = 15
        self.gribWaveOpacity = 0.85
        # Persisted so the wave layer survives a restart; the wind layer has
        # always reset, which reads as data loss once waves are in use.
        self.gribWaveVisible = False
        self.gribWaveSmooth = True
        self.gribWaveLevels = 9
        # Wave direction arrows reuse the wind barbs' density modes, with
        # their own setting so the two glyph layers thin independently.
        self.gribWaveDensity = "standard"
        # Display-only wave source key; empty means the routing forecast.
        # Restored only when it resolves to a published wave choice.
        self.gribWaveSource = ""
        self.weatherRenderer = "viewport"
        self.gribIsobarsVisible = True
        self.gribIsobarInterval = 4.0
        self.gribIsobarOpacity = 0.85
        self.gribIsobarLabels = True
        self.vectorCharts = []
        self.rasterCharts = []
        self.mbtilesCharts = []
        self.chartPalette = "cm93"
        self.developerMode = False
        # Retain each successfully decoded EBL segment in the library by
        # default; future imports only, never retroactive deletion.
        self.keepEblOriginals = True
        self.smoothZoom = True
        self.panInertia = True
        self.mapRenderer = "cpu"
        self.weatherNextCredentialsPath = ""
        self.canadianCharts = True
        self.chartWorldVisible = True
        self.chartSatelliteVisible = False
        self.satellitePack = ""
        self.chartENCVisible = True
        self.chartRasterVisible = True
        self.chartMBTilesVisible = True
        self.chartCM93Visible = True
        self.chartSeamarksVisible = True
        self.chartVectorVisible = True

        self.load_or_save_default()
