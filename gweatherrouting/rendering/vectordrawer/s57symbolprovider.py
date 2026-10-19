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

import json
import logging
from typing import cast

import cairo
from bs4 import BeautifulSoup
from bs4.element import Tag


from gweatherrouting.common import resource_path
from gweatherrouting.core.storage import DATA_DIR

logger = logging.getLogger("gweatherrouting")


def _int_attribute(tag: Tag, name: str) -> int:
    """Return an integer attribute of a tag.

    The S57 symbol XML stores every numeric attribute as plain decimal
    text, so the attribute read from the tag is always a string.
    """
    return int(cast(str, tag[name]))


class S57SymbolProvider:
    def __init__(self, settings_manager):
        self.settings_manager = settings_manager
        self.symbols = {}

        self.p_day = cairo.ImageSurface.create_from_png(
            resource_path("gweatherrouting", "data/s57/rastersymbols-day.png")
        )
        self.p_dark = cairo.ImageSurface.create_from_png(
            resource_path("gweatherrouting", "data/s57/rastersymbols-dark.png")
        )

        self.p_select = self.p_day
        self._sprites = {}
        self.on_chart_palette_changed(settings_manager.chartPalette)
        settings_manager.register_on_change(
            "chartPalette", self.on_chart_palette_changed
        )

        # Try to load cached data
        try:
            with open(DATA_DIR + "/s57_symbols.json", "r") as f:
                self.symbols = json.load(f)
                logger.debug("Loaded cached S57 symbols")
                return
        except:
            logger.warning("Cached S57 symbols not found, loading from XML")
            self.symbols = {}

        # Load symbols
        data = open(
            resource_path("gweatherrouting", "data/s57/chartsymbols.xml")
        ).read()

        soup = BeautifulSoup(data, "html.parser")

        for row in soup.find_all("symbol"):
            try:
                name_tag = row.find("name")
                if name_tag is None:
                    continue
                name = name_tag.text

                description_tag = row.find("description")
                description = (
                    description_tag.text if description_tag is not None else ""
                )

                bm = row.find("bitmap")
                if bm is None:
                    continue

                distance = bm.find("distance")
                pivot_tag = bm.find("pivot")
                origin_tag = bm.find("origin")
                gloc_tag = bm.find("graphics-location")
                if (
                    distance is None
                    or pivot_tag is None
                    or origin_tag is None
                    or gloc_tag is None
                ):
                    continue

                dim = _int_attribute(bm, "width"), _int_attribute(bm, "height")
                dist = _int_attribute(distance, "max"), _int_attribute(distance, "min")
                pivot = _int_attribute(pivot_tag, "x"), _int_attribute(pivot_tag, "y")
                origin = _int_attribute(origin_tag, "x"), _int_attribute(
                    origin_tag, "y"
                )
                gloc = _int_attribute(gloc_tag, "x"), _int_attribute(gloc_tag, "y")

                self.symbols[name] = {
                    "description": description,
                    "dim": dim,
                    "dist": dist,
                    "pivot": pivot,
                    "origin": origin,
                    "gloc": gloc,
                }

            except:
                continue

        with open(DATA_DIR + "/s57_symbols.json", "w") as f:
            json.dump(self.symbols, f)
            f.close()
            logger.debug("Saved cached S57 symbols")

    def on_chart_palette_changed(self, v):
        if v == "dark":
            self.p_select = self.p_dark
        else:
            self.p_select = self.p_day

    def draw(self, cr, n, xx, yy):
        s = self.symbols[n]
        key = (id(self.p_select), n)
        img = self._sprites.get(key)
        if img is None:
            img = cairo.ImageSurface(cairo.FORMAT_ARGB32, *s["dim"])
            sprite_cr = cairo.Context(img)
            sprite_cr.set_source_surface(self.p_select, -s["gloc"][0], -s["gloc"][1])
            sprite_cr.paint()
            self._sprites[key] = img
        cr.set_source_surface(img, xx - s["pivot"][0], yy - s["pivot"][1])
        cr.paint()

    def draw_light(self, cr, xx, yy, tags, major):
        cr.move_to(xx + 12, yy + 4)
        try:
            txt = (
                tags["seamark:light:character"]
                + "."
                + tags["seamark:light:colour"][0].upper()
                + "."
                + tags["seamark:light:period"]
                + "s"
            )
            cr.show_text(txt)
        except:
            y = yy + 4
            i = 1
            while True:
                try:
                    txt = (
                        tags["seamark:light:" + str(i) + ":character"]
                        + "."
                        + tags["seamark:light:" + str(i) + ":colour"][0].upper()
                        + "."
                        + tags["seamark:light:" + str(i) + ":period"]
                        + "s"
                    )
                except:
                    break
                cr.move_to(xx + 12, y)
                cr.show_text(txt)
                y += 12
                i += 1

        self.draw(cr, "LIGHTS13", xx, yy)
