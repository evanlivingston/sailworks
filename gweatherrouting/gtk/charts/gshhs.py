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

import logging
import math
from collections import OrderedDict
import time
from threading import Thread

import gi
import requests
import numpy as np

try:
    import shapely
except ImportError:
    shapely = None
from osgeo import ogr

gi.require_version("Gtk", "3.0")

from gi.repository import Gdk, GLib, Gtk

from gweatherrouting.gtk.widgets.mapwidget import MapPoint

from gweatherrouting.common import resource_path
from gweatherrouting.core.core import LinePointValidityProvider
from gweatherrouting.core.storage import DATA_DIR, TEMP_DIR

from .chartlayer import ChartLayer

logger = logging.getLogger("gweatherrouting")


class AskDownloadDialog(Gtk.Dialog):
    def __init__(self, parent, gshhs_missing, osm_missing):
        super().__init__(title="Map Data Setup", transient_for=parent, flags=0)
        self.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            "Download",
            Gtk.ResponseType.OK,
        )
        self.set_default_size(350, 180)
        self.set_border_width(10)

        box = self.get_content_area()
        box.set_spacing(10)

        label = Gtk.Label(label="Some map data is missing.\nSelect what to download:")
        label.set_xalign(0)
        box.add(label)

        self.gshhs_check = None
        self.osm_check = None

        if gshhs_missing:
            self.gshhs_check = Gtk.CheckButton(
                label="Base world contour lines (~140 MB)"
            )
            self.gshhs_check.set_active(True)
            box.add(self.gshhs_check)

        if osm_missing:
            self.osm_check = Gtk.CheckButton(label="OpenSeaMap sea marks (~2 MB)")
            self.osm_check.set_active(True)
            box.add(self.osm_check)

        self.show_all()

    @property
    def download_gshhs(self):
        return self.gshhs_check is not None and self.gshhs_check.get_active()

    @property
    def download_osm(self):
        return self.osm_check is not None and self.osm_check.get_active()


class OSMAskDownloadDialog(Gtk.Dialog):
    def __init__(self, parent):
        super().__init__(title="OpenSeaMap Download", transient_for=parent, flags=0)
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        self.set_default_size(250, 100)
        self.set_border_width(10)
        label = Gtk.Label(
            label="OpenSeaMap data is missing,\ndo you want to download them (~2MB)?"
        )
        box = self.get_content_area()
        box.add(label)
        self.show_all()


class OSMDownloadDialog(Gtk.Dialog):
    def __init__(self, parent):
        super().__init__(title="OpenSeaMap Download", transient_for=parent, flags=0)
        self.parent = parent
        self.set_default_size(250, 60)
        self.set_border_width(10)
        self.pb = Gtk.ProgressBar()
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.label = Gtk.Label(label="Download in progress")
        b.pack_start(self.label, True, True, 20)
        b.pack_start(self.pb, True, True, 20)
        box = self.get_content_area()
        box.add(b)
        self.show_all()

        self.thread = Thread(target=self.download, args=())
        self.thread.start()

    def percentage_callback(self, percentage, d, t):
        Gdk.threads_enter()
        self.pb.set_fraction(percentage / 100)
        self.label.set_text(f"Downloading: {percentage}%")
        Gdk.threads_leave()

    def callback(self, success):
        Gdk.threads_enter()
        if success:
            edialog = Gtk.MessageDialog(
                self.parent, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.OK, "Done"
            )
            edialog.format_secondary_text("OpenSeaMap downloaded")
            edialog.run()
            edialog.destroy()
            self.response(Gtk.ResponseType.OK)
        else:
            logger.error("Error downloading OpenSeaMap")

            edialog = Gtk.MessageDialog(
                self.parent, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, "Error"
            )
            edialog.format_secondary_text("Cannot download OpenSeaMap")
            edialog.run()
            edialog.destroy()
            self.response(Gtk.ResponseType.CANCEL)
        Gdk.threads_leave()

    def download(self):
        uri = "https://github.com/dakk/osm-seamarks/raw/master/seamarks.pbf"
        logger.info("Downloading openseamap")

        response = requests.get(uri, stream=True)
        total_length = response.headers.get("content-length")
        last_signal_percent = -1
        f = open(DATA_DIR + "/seamarks.pbf", "wb")

        if total_length is None:
            pass
        else:
            dl = 0
            total_length_i = int(total_length)
            for data in response.iter_content(chunk_size=4096):
                dl += len(data)
                f.write(data)
                done = int(100 * dl / total_length_i)

                if last_signal_percent != done:
                    self.percentage_callback(done, dl, total_length_i)
                    last_signal_percent = done

        f.close()
        logger.info("OpenSeaMap download completed")

        self.callback(True)


class GSHHSAskDownloadDialog(Gtk.Dialog):
    def __init__(self, parent):
        super().__init__(title="Base Map Download", transient_for=parent, flags=0)
        self.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        self.set_default_size(250, 100)
        self.set_border_width(10)
        label = Gtk.Label(
            label="Base world countourn lines are missing,\ndo you want to download them (~140MB)?"
        )
        box = self.get_content_area()
        box.add(label)
        self.show_all()


class GSHHSDownloadDialog(Gtk.Dialog):
    def __init__(self, parent):
        super().__init__(title="Base Map Download", transient_for=parent, flags=0)
        self.parent = parent
        self.set_default_size(250, 60)
        self.set_border_width(10)
        self.pb = Gtk.ProgressBar()
        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.label = Gtk.Label(label="Download in progress")
        b.pack_start(self.label, True, True, 20)
        b.pack_start(self.pb, True, True, 20)
        box = self.get_content_area()
        box.add(b)
        self.show_all()

        self.thread = Thread(target=self.download, args=())
        self.thread.start()

    def percentage_callback(self, percentage, d, t):
        Gdk.threads_enter()
        self.pb.set_fraction(percentage / 100)
        self.label.set_text(f"Downloading: {percentage}%")
        Gdk.threads_leave()

    def callback(self, success):
        Gdk.threads_enter()
        if success:
            edialog = Gtk.MessageDialog(
                self.parent, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.OK, "Done"
            )
            edialog.format_secondary_text("Base map downloaded")
            edialog.run()
            edialog.destroy()
            self.response(Gtk.ResponseType.OK)
        else:
            logger.error("Error downloading GSHHS")

            edialog = Gtk.MessageDialog(
                self.parent, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, "Error"
            )
            edialog.format_secondary_text("Cannot download base map")
            edialog.run()
            edialog.destroy()
            self.response(Gtk.ResponseType.CANCEL)
        Gdk.threads_leave()

    def download(self):
        import zipfile

        # uri = "https://www.ngdc.noaa.gov/mgg/shorelines/data/gshhg/oldversions/" +
        # "version2.3.6/gshhg-shp-2.3.6.zip"
        uri = (
            "https://github.com/dakk/gweatherrouting/releases/download/"
            + "gshhs2.3.6/gshhg-shp-2.3.6.zip"
        )
        logger.info("Downloading gshhs base map")

        response = requests.get(uri, stream=True)
        total_length = response.headers.get("content-length")
        last_signal_percent = -1
        f = open(TEMP_DIR + "/gshhs2.3.6.zip", "wb")

        if total_length is None:
            pass
        else:
            dl = 0
            total_length_i = int(total_length)
            for data in response.iter_content(chunk_size=4096):
                dl += len(data)
                f.write(data)
                done = int(100 * dl / total_length_i)

                if last_signal_percent != done:
                    self.percentage_callback(done, dl, total_length_i)
                    last_signal_percent = done

        f.close()
        logger.info("GSHHS base map download completed")

        zipfile.ZipFile(TEMP_DIR + "/gshhs2.3.6.zip").extractall(DATA_DIR)
        self.callback(True)


class CombinedDownloadDialog(Gtk.Dialog):
    def __init__(self, parent, download_gshhs, download_osm):
        super().__init__(title="Downloading Map Data", transient_for=parent, flags=0)
        self.parent = parent
        self.download_gshhs = download_gshhs
        self.download_osm = download_osm
        self.success_gshhs = False
        self.success_osm = False

        self.set_default_size(350, 100)
        self.set_border_width(10)

        b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.label = Gtk.Label(label="Preparing download...")
        self.pb = Gtk.ProgressBar()
        b.pack_start(self.label, True, True, 10)
        b.pack_start(self.pb, True, True, 10)
        box = self.get_content_area()
        box.add(b)
        self.show_all()

        self.thread = Thread(target=self._run_downloads, args=())
        self.thread.start()

    def _update_progress(self, text, fraction):
        GLib.idle_add(self._apply_progress, text, fraction)

    def _apply_progress(self, text, fraction):
        self.label.set_text(text)
        self.pb.set_fraction(fraction)
        return GLib.SOURCE_REMOVE

    def _download_file(self, uri, dest, label_prefix):
        response = requests.get(uri, stream=True)
        total_length = response.headers.get("content-length")
        last_signal_percent = -1

        with open(dest, "wb") as f:
            if total_length is None:
                f.write(response.content)
            else:
                dl = 0
                total_length_i = int(total_length)
                for data in response.iter_content(chunk_size=4096):
                    dl += len(data)
                    f.write(data)
                    done = int(100 * dl / total_length_i)
                    if last_signal_percent != done:
                        self._update_progress(f"{label_prefix}: {done}%", done / 100.0)
                        last_signal_percent = done

    def _run_downloads(self):
        import zipfile

        try:
            if self.download_gshhs:
                uri = (
                    "https://github.com/dakk/gweatherrouting/releases/download/"
                    + "gshhs2.3.6/gshhg-shp-2.3.6.zip"
                )
                self._update_progress("Downloading base map...", 0)
                self._download_file(uri, TEMP_DIR + "/gshhs2.3.6.zip", "Base map")
                self._update_progress("Extracting base map...", 1.0)
                zipfile.ZipFile(TEMP_DIR + "/gshhs2.3.6.zip").extractall(DATA_DIR)
                self.success_gshhs = True
                logger.info("GSHHS base map download completed")

            if self.download_osm:
                uri = "https://github.com/dakk/osm-seamarks/raw/master/seamarks.pbf"
                self._update_progress("Downloading OpenSeaMap...", 0)
                self._download_file(uri, DATA_DIR + "/seamarks.pbf", "OpenSeaMap")
                self.success_osm = True
                logger.info("OpenSeaMap download completed")

            self._finish(True)
        except Exception:
            logger.exception("Error during map data download")
            self._finish(False)

    def _finish(self, success):
        GLib.idle_add(self._show_result, success)

    def _show_result(self, success):
        if success:
            edialog = Gtk.MessageDialog(
                self.parent, 0, Gtk.MessageType.INFO, Gtk.ButtonsType.OK, "Done"
            )
            edialog.format_secondary_text("Map data downloaded successfully")
            edialog.run()
            edialog.destroy()
            self.response(Gtk.ResponseType.OK)
        else:
            edialog = Gtk.MessageDialog(
                self.parent,
                0,
                Gtk.MessageType.ERROR,
                Gtk.ButtonsType.OK,
                "Error",
            )
            edialog.format_secondary_text("An error occurred during download")
            edialog.run()
            edialog.destroy()
            self.response(Gtk.ResponseType.CANCEL)
        return GLib.SOURCE_REMOVE


# 	GSHHS_shp: Shapefiles (polygons) derived from shorelines.
# 		   These are named GSHHS_<resolution>_L<level>.*
# 	WDBII_shp: Political boundaries and rivers (lines).
# 		   These are called
# 		   WDBII_border_<resolution>_L<level>.*
# 		   WDBII_river_<resolution>_L<level>.*

# where <resolution> is one of f,h,i,l,c and <level> is an integer.
# All data sets come in 5 different resolutions:
# 	f : Full resolution.  These contain the maximum resolution
# 	    of this data and has not been decimated.
# 	h : High resolution.  The Douglas-Peucker line reduction was
# 	    used to reduce data size by ~80% relative to full.
# 	i : Intermediate resolution.  The Douglas-Peucker line reduction was
# 	    used to reduce data size by ~80% relative to high.
# 	l : Low resolution.  The Douglas-Peucker line reduction was
# 	    used to reduce data size by ~80% relative to intermediate.
# 	c : Crude resolution.  The Douglas-Peucker line reduction was
# 	    used to reduce data size by ~80% relative to low.

# For each resolution there are several levels; these depends on the data type.

# The shoreline data are distributed in 6 levels:

# Level 1: Continental land masses and ocean islands, except Antarctica.
# Level 2: Lakes
# Level 3: Islands in lakes
# Level 4: Ponds in islands within lakes
# Level 5: Antarctica based on ice front boundary.
# Level 6: Antarctica based on grounding line boundary.

from .vectordrawer.simplechartdrawer import SimpleChartDrawer


class GSHHSVectorChart(ChartLayer, LinePointValidityProvider):
    def __init__(self, path, settings_manager, metadata=None):
        super().__init__(path, "vector", settings_manager, metadata)

        self.last_time = {"c": 0, "l": 0, "i": 0, "h": 0, "f": 0}
        self.force_downscale = False

        self.drawer = SimpleChartDrawer(settings_manager)
        drv = ogr.GetDriverByName("ESRI Shapefile")
        self.vector_files = {}
        for x in ["f", "h", "i", "l", "c"]:
            for y in [1]:  # ,2,3,4,5,6]:
                f = drv.Open(
                    path + "/GSHHS_shp/" + x + "/GSHHS_" + x + "_L" + str(y) + ".shp"
                )
                if f is not None:
                    self.vector_files[x + str(y)] = f

        self.lpvFile = drv.Open(path + "/GSHHS_shp/h/GSHHS_h_L1.shp")

        f = open(
            resource_path("gweatherrouting", "data/countries_en.txt"),
            "r",
        )
        self.countries = list(
            filter(
                lambda x: len(x) == 4, map(lambda x: x.split(";"), f.read().split("\n"))
            )
        )
        f.close()

    def on_register(self, on_tick_handler=None):
        pass

    def _routing_source(self, layer_index, feature):
        key = (layer_index, feature.GetFID())
        if key not in self._routing_sources:
            geometry = feature.GetGeometryRef().Clone()
            if not geometry.IsValid():
                geometry = geometry.MakeValid()
            if geometry is None:
                raise ValueError("Unable to repair routing shoreline geometry")
            self._routing_sources[key] = geometry
            if len(self._routing_sources) > 64:
                self._routing_sources.popitem(last=False)
        self._routing_sources.move_to_end(key)
        return self._routing_sources[key]

    def _routing_land(self, west, south, east, north):
        """Cache exact local shoreline geometry, repairing invalid source polygons."""
        # Tile bounds make consecutive integration queries share the same clip.
        bounds = (
            math.floor(west * 4) / 4 - 0.001,
            math.floor(south * 4) / 4 - 0.001,
            math.ceil(east * 4) / 4 + 0.001,
            math.ceil(north * 4) / 4 + 0.001,
        )
        if not hasattr(self, "_routing_clips"):
            self._routing_clips: OrderedDict[tuple, tuple] = OrderedDict()
            self._routing_sources: OrderedDict[tuple, ogr.Geometry] = OrderedDict()
        if bounds in self._routing_clips:
            self._routing_clips.move_to_end(bounds)
            return self._routing_clips[bounds]
        left, bottom, right, top = bounds
        clip = ogr.CreateGeometryFromWkt(
            f"POLYGON (({left} {bottom}, {right} {bottom}, {right} {top}, "
            f"{left} {top}, {left} {bottom}))"
        )
        geometries = []
        for i in range(self.lpvFile.GetLayerCount()):
            layer = self.lpvFile.GetLayerByIndex(i)
            layer.SetSpatialFilterRect(*bounds)
            layer.ResetReading()
            for feature in layer:
                geometry = self._routing_source(i, feature).Intersection(clip)
                if geometry is None:
                    raise ValueError("Unable to clip routing shoreline geometry")
                if not geometry.IsEmpty():
                    geometries.append((geometry, geometry.CreatePreparedGeometry()))
        self._routing_clips[bounds] = tuple(geometries)
        if len(self._routing_clips) > 64:
            self._routing_clips.popitem(last=False)
        return self._routing_clips[bounds]

    def _native_routing_land(self, land):
        """Convert each immutable clipped polygon once for native batch predicates."""
        if not hasattr(self, "_routing_native_clips"):
            self._routing_native_clips: OrderedDict = OrderedDict()
        key = id(land)
        if key not in self._routing_native_clips:
            polygons = []
            for geometry, _ in land:
                native = shapely.from_wkb(bytes(geometry.ExportToWkb()))
                shapely.prepare(native)
                polygons.append((*geometry.GetEnvelope(), native))
            # Retain land itself so its identity cannot be reused while cached.
            self._routing_native_clips[key] = (land, tuple(polygons))
            if len(self._routing_native_clips) > 64:
                self._routing_native_clips.popitem(last=False)
        self._routing_native_clips.move_to_end(key)
        return self._routing_native_clips[key][1]

    def points_validity(self, latlons):
        """Screen polygon envelopes in arrays, then use exact containment."""
        if not len(latlons):
            return []
        points = np.asarray(latlons, dtype=float)
        latitudes, longitudes = points[:, 0], points[:, 1]
        land = self._routing_land(
            longitudes.min(), latitudes.min(), longitudes.max(), latitudes.max()
        )
        valid = np.ones(len(points), dtype=bool)
        if shapely is not None and getattr(self, "use_native_geometry", True):
            for west, east, south, north, native in self._native_routing_land(land):
                possible = np.flatnonzero(
                    valid
                    & (longitudes >= west)
                    & (longitudes <= east)
                    & (latitudes >= south)
                    & (latitudes <= north)
                )
                valid[possible] &= ~shapely.contains_xy(
                    native, longitudes[possible], latitudes[possible]
                )
            return valid.tolist()
        for geometry, prepared in land:
            west, east, south, north = geometry.GetEnvelope()
            possible = np.flatnonzero(
                valid
                & (longitudes >= west)
                & (longitudes <= east)
                & (latitudes >= south)
                & (latitudes <= north)
            )
            for i in possible:
                point = ogr.Geometry(ogr.wkbPoint)
                point.AddPoint(float(longitudes[i]), float(latitudes[i]))
                if (prepared or geometry).Contains(point):
                    valid[i] = False
        return valid.tolist()

    def lines_validity(self, latlons):
        """Screen envelopes without weakening the exact segment intersection."""
        if not len(latlons):
            return []
        lines = np.asarray(latlons, dtype=float)
        left = np.minimum(lines[:, 1], lines[:, 3])
        right = np.maximum(lines[:, 1], lines[:, 3])
        bottom = np.minimum(lines[:, 0], lines[:, 2])
        top = np.maximum(lines[:, 0], lines[:, 2])
        land = self._routing_land(left.min(), bottom.min(), right.max(), top.max())
        valid = np.ones(len(lines), dtype=bool)
        if shapely is not None and getattr(self, "use_native_geometry", True):
            segments = None
            for west, east, south, north, native in self._native_routing_land(land):
                possible = np.flatnonzero(
                    valid
                    & (right >= west)
                    & (left <= east)
                    & (top >= south)
                    & (bottom <= north)
                )
                if not len(possible):
                    continue
                if segments is None:
                    segments = shapely.linestrings(lines.reshape(-1, 2, 2)[:, :, ::-1])
                valid[possible] &= ~shapely.intersects(native, segments[possible])
            return valid.tolist()
        for geometry, prepared in land:
            west, east, south, north = geometry.GetEnvelope()
            possible = np.flatnonzero(
                valid
                & (right >= west)
                & (left <= east)
                & (top >= south)
                & (bottom <= north)
            )
            for i in possible:
                lat1, lon1, lat2, lon2 = lines[i]
                line = ogr.Geometry(ogr.wkbLineString)
                line.AddPoint(float(lon1), float(lat1))
                line.AddPoint(float(lon2), float(lat2))
                if (prepared or geometry).Intersects(line):
                    valid[i] = False
        return valid.tolist()

    def do_draw(self, gpsmap, cr):
        bounding_geometry = self.get_bounding_geometry(gpsmap)

        # Get the correct quality for zoom level
        scale = gpsmap.get_scale()
        if scale > 5000:
            q = "c"
        elif scale > 500:  # or self.last_time['i'] > 1.0:
            q = "l"
        elif scale > 200 or self.force_downscale:  # or self.last_time['h'] > 1.0
            q = "i"
        elif scale >= 15 or self.last_time["f"] > 0.25:
            q = "h"
        elif scale < 15:
            q = "f"

        t = time.time()
        self.drawer.draw(gpsmap, cr, self.vector_files[q + "1"], bounding_geometry)
        self.last_time[q] = int(time.time() - t)

        if scale < 4500:
            for c in self.countries:
                # TODO: check if x[2],x[3] is inside the bounding_geometry

                # render country name
                cr.set_source_rgba(0, 0, 0, 0.8)
                cr.set_font_size(9)
                x, y = gpsmap.convert_geographic_to_screen(
                    MapPoint.new_degrees(float(c[2]), float(c[3]))
                )
                cr.move_to(x, y)
                cr.show_text(c[1])

    def do_render(self, gpsmap):
        pass

    def do_busy(self):
        return False

    def do_button_press(self, gpsmap, gdkeventbutton):
        return False
