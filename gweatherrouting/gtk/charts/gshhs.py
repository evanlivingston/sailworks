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
from threading import Thread

import gi
import requests

gi.require_version("Gtk", "3.0")

from gi.repository import Gdk, GLib, Gtk


from gweatherrouting.core.storage import DATA_DIR, TEMP_DIR

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

from gweatherrouting.rendering.gshhs import GSHHSVectorChart  # noqa: F401
