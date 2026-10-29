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

from gweatherrouting.core.utils import Storage


class SettingsManager(Storage):
    def __init__(self):
        Storage.__init__(self, "settings")

        self.gribArrowOpacity = 0.4
        self.gribArrowOnGround = False
        self.gribBarbDensity = "standard"
        self.marineCurrentDensity = "standard"
        self.gribWindStyle = "barbs"
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
        self.smoothZoom = True
        self.panInertia = True
        self.mapRenderer = "cpu"
        self.canadianCharts = True
        self.chartWorldVisible = True
        self.chartENCVisible = True
        self.chartRasterVisible = True
        self.chartMBTilesVisible = True
        self.chartCM93Visible = True
        self.chartSeamarksVisible = True
        self.chartVectorVisible = True

        self.load_or_save_default()
