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

import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from gweatherrouting.core import SerialDataSource
from gweatherrouting.core.nmea2000datasource import N2K_FORMATS

ctypes = ["Network", "Serial"]
ntypes = ["TCP", "UDP"]
directions = ["In", "Out", "Both"]
protocols = ["nmea0183", "nmea2000"]
protocol_labels = ["NMEA 0183", "NMEA 2000"]
baudrates = [4800, 9600, 19200, 38400, 57600, 115200, 230400]


def lower(s):
    return s.lower()


class ConnectionEditDialog:
    def __init__(self, parent, data=None):
        self.parent = parent
        self.data = data
        self.builder = Gtk.Builder()
        self.builder.add_from_file(
            os.path.abspath(os.path.dirname(__file__)) + "/connectioneditdialog.glade"
        )
        self.builder.connect_signals(self)

        self.dialog = self.builder.get_object("connection-edit-dialog")
        self.dialog.set_transient_for(parent)
        self.dialog.set_default_size(550, 300)

        self.dialog.show_all()

        self.builder.get_object("network-frame").hide()
        self.typeCombo = self.builder.get_object("type-combo")
        for x in ctypes:
            self.typeCombo.append_text(x)
        self.typeCombo.set_active(1)

        self.networkTypeCombo = self.builder.get_object("network-type-combo")
        for x in ntypes:
            self.networkTypeCombo.append_text(x)
        self.networkTypeCombo.set_active(0)

        self.directionCombo = self.builder.get_object("direction-combo")
        for x in directions:
            self.directionCombo.append_text(x)
        self.directionCombo.set_active(0)

        self.protocolCombo = self.builder.get_object("protocol-combo")
        for protocol, label in zip(protocols, protocol_labels):
            self.protocolCombo.append(protocol, label)
        self.protocolCombo.set_active(0)

        self.n2kFormatCombo = self.builder.get_object("n2k-format-combo")
        for wire_format, config in N2K_FORMATS.items():
            self.n2kFormatCombo.append(wire_format, config["label"])
        self.n2kFormatCombo.set_active(0)
        self.n2kFormatLabel = self.builder.get_object("n2k-format-label")
        self.n2kFormatLabel.hide()
        self.n2kFormatCombo.hide()

        self.serialDataPortCombo = self.builder.get_object("serial-data-port-combo")
        for x in SerialDataSource.detect():
            self.serialDataPortCombo.append_text(x)
        self.serialDataPortCombo.set_active(0)

        self.serialBaudrateCombo = self.builder.get_object("serial-baudrate-combo")
        for xb in baudrates:
            self.serialBaudrateCombo.append_text(str(xb))
        self.serialBaudrateCombo.set_active(baudrates.index(9600))

        self.protocolCombo.connect("changed", self.on_protocol_change)
        self.n2kFormatCombo.connect("changed", self.on_format_change)

        if self.data is not None:
            # Set data
            if self.data["type"] == "serial":
                self.typeCombo.set_active(1)
                self.serialDataPortCombo.get_child().set_text(self.data["data-port"])
                self.serialBaudrateCombo.set_active(
                    baudrates.index(self.data["baudrate"])
                )
            elif self.data["type"] == "network":
                self.typeCombo.set_active(0)
                self.networkTypeCombo.set_active(
                    list(map(lower, ntypes)).index(self.data["network"])
                )
                self.builder.get_object("network-host").set_text(self.data["host"])
                self.builder.get_object("network-port").set_text(str(self.data["port"]))
            saved_protocol = self.data["protocol"]
            saved_format = self.data.get("format")
            if saved_protocol in N2K_FORMATS:
                saved_format = saved_format or saved_protocol
                saved_protocol = "nmea2000"
            self.protocolCombo.set_active(protocols.index(saved_protocol))
            if saved_protocol == "nmea2000" and saved_format in N2K_FORMATS:
                self.n2kFormatCombo.set_active_id(saved_format)
            self.directionCombo.set_active(
                list(map(lower, directions)).index(self.data["direction"])
            )

    def run(self):
        return self.dialog.run()

    def destroy(self):
        return self.dialog.destroy()

    def on_type_change(self, widget):
        if self.typeCombo.get_active() == 0:
            self.builder.get_object("network-frame").show()
            self.builder.get_object("serial-frame").hide()
        else:
            self.builder.get_object("network-frame").hide()
            self.builder.get_object("serial-frame").show()

    def on_protocol_change(self, widget):
        protocol = self.protocolCombo.get_active_id()
        nmea2000 = protocol == "nmea2000"
        if nmea2000:
            self.n2kFormatLabel.show()
            self.n2kFormatCombo.show()
            self.directionCombo.set_active(0)
            if self.data is None:
                wire_format = self.n2kFormatCombo.get_active_id()
                baud = N2K_FORMATS[wire_format]["baudrate"]
                self.serialBaudrateCombo.set_active(baudrates.index(baud))
        else:
            self.n2kFormatLabel.hide()
            self.n2kFormatCombo.hide()
        self.directionCombo.set_sensitive(not nmea2000)

    def on_format_change(self, widget):
        if self.data is None and self.protocolCombo.get_active_id() == "nmea2000":
            wire_format = self.n2kFormatCombo.get_active_id()
            baud = N2K_FORMATS[wire_format]["baudrate"]
            self.serialBaudrateCombo.set_active(baudrates.index(baud))

    def save_data(self):
        protocol = self.protocolCombo.get_active_id()
        self.data = {
            "type": lower(self.typeCombo.get_active_text()),
            "protocol": protocol,
            "direction": lower(self.directionCombo.get_active_text()),
        }
        if protocol == "nmea2000":
            self.data["format"] = self.n2kFormatCombo.get_active_id()
        if self.data["type"] == "serial":
            self.data["data-port"] = self.serialDataPortCombo.get_active_text()
            self.data["baudrate"] = int(self.serialBaudrateCombo.get_active_text())
            if not self.data["data-port"]:
                raise ValueError("Choose a serial port")
        elif self.data["type"] == "network":
            self.data["network"] = lower(self.networkTypeCombo.get_active_text())
            self.data["host"] = self.builder.get_object("network-host").get_text()
            self.data["port"] = int(self.builder.get_object("network-port").get_text())

    def on_save(self, widget):
        try:
            self.save_data()
            self.dialog.response(Gtk.ResponseType.OK)
        except:
            dialog = Gtk.MessageDialog(
                self.parent, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, "Error"
            )
            dialog.format_secondary_text("Invalid data")
            dialog.run()
            dialog.destroy()

    def on_close(self, widget):
        self.dialog.response(Gtk.ResponseType.CANCEL)
