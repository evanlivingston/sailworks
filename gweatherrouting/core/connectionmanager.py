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
import time
from threading import Event, Lock, Thread
from typing import Dict, List

from gweatherrouting.core import DataSource, NetworkDataSource, SerialDataSource
from gweatherrouting.core.utils import EventDispatcher, Storage

from .connectionconfig import connection_config, connection_key
from .actisensedatasource import ActisenseDataSource
from .nmea2000datasource import N2K_PROTOCOLS, NMEA2000DataSource

logger = logging.getLogger("gweatherrouting")


# Every connection can be of type:
# - serial: data port, baudrate, protocol
# - connection: tcpudp, host, data port, protocol


class ConnectionManagerStorage(Storage):
    def __init__(self):
        Storage.__init__(self, "conn-manager")
        self.connections = []
        self.load_or_save_default()


def create_data_source(config):
    """Build a source from its transport and wire-format configuration."""
    protocol = config["protocol"]
    if protocol in N2K_PROTOCOLS:
        wire_format = config.get("format", protocol)
        if wire_format == "nmea2000":
            wire_format = "actisense"
        if wire_format == "actisense" and config["type"] == "serial":
            return ActisenseDataSource(
                wire_format,
                config["direction"],
                config["data-port"],
                config["baudrate"],
            )
        return NMEA2000DataSource(config)
    if config["type"] == "serial":
        return SerialDataSource(
            protocol,
            config["direction"],
            config["data-port"],
            config["baudrate"],
        )
    return NetworkDataSource(
        protocol,
        config["direction"],
        config["host"],
        config["port"],
        config["network"],
    )


# { type: 'serial|network', protocol: 'nmea0183|nmea2000', direction: 'in|out|both' }
# NMEA 2000 connections also store the gateway's output format separately.
# Serial: { data-port: '/dev/ttyACM0', baudrate: 9600 }
# Network: { network: 'tcp|udp', host: 'localhost', port: '1234' }


class ConnectionManager(EventDispatcher):
    def __init__(self):
        EventDispatcher.__init__(self)
        self.storage = ConnectionManagerStorage()
        self.stop_event = Event()
        self.sources: Dict[str, DataSource] = {}
        self._sources_lock = Lock()
        self._packets: Dict[str, int] = {}
        self._polling_active = False
        self._last_status: List[dict] = []
        self.thread = None

    def __del__(self):
        self.stop_event.set()
        for ds in self.sources.values():
            try:
                ds.close()
            except Exception:
                pass

    def stop_polling(self):
        logger.info("Polling stopped")
        self._polling_active = False
        self.stop_event.set()
        if self.thread:
            self.thread.join()
            self.thread = None
        with self._sources_lock:
            for key, ds in self.sources.items():
                try:
                    ds.close()
                except Exception:
                    logger.debug("Error closing data source %s", key)
            self.sources = {}

    @property
    def connections(self):
        return self.storage.connections

    def get_status(self) -> List[dict]:
        """Return a snapshot of each connection's status and stats."""
        # Reconnection holds the source lock while opening transports. Status
        # displays must remain responsive during a slow network connection.
        if not self._sources_lock.acquire(blocking=False):
            return list(self._last_status)
        try:
            sources = dict(self.sources)
        finally:
            self._sources_lock.release()
        result = []
        for key, ds in sources.items():
            result.append(
                {
                    "name": key,
                    "connected": ds.connected,
                    "packets": self._packets.get(key, 0),
                }
            )
        self._last_status = result
        return result

    @property
    def polling_active(self) -> bool:
        return self._polling_active

    def plug_all(self):
        logger.info("Plugging all connections")
        with self._sources_lock:
            for key, ds in self.sources.items():
                try:
                    ds.close()
                except Exception:
                    logger.debug("Error closing data source %s", key)
            self.sources = {}
            for x in self.storage.connections:
                if x["type"] == "serial":
                    key = x["data-port"]
                elif x["type"] == "network":
                    key = x["host"] + ":" + str(x["port"])
                else:
                    continue

                source = create_data_source(x)
                self.sources[key] = source
                if source.connect():
                    logger.info("Data source %s connected", key)

    def replace_connections(self, configs):
        """Save an edited set, reconnecting only when polling was already active.

        Call from one lifecycle owner, outside the polling thread. Validation
        happens before interrupting live instruments; failed saves retain the
        previous in-memory configuration and restart its polling.
        """
        configs = [connection_config(config) for config in configs]
        keys = [connection_key(config) for config in configs]
        if len(set(keys)) != len(keys):
            raise ValueError("A connection already uses this serial port or host/port")
        previous = self.connections
        active = self.polling_active
        self.stop_polling()
        try:
            self.storage.connections = configs
            self.storage.save()
        except Exception:
            # Bypass Storage's automatic save while restoring the previous value.
            if isinstance(self.storage, dict):
                dict.__setitem__(self.storage, "connections", previous)
                self.storage.__dict__["connections"] = previous
            else:
                self.storage.connections = previous
            raise
        finally:
            if active:
                self.start_polling()

    def add_connection(self, d):
        if d["protocol"] in N2K_PROTOCOLS and d["direction"] != "in":
            raise ValueError("NMEA 2000 connections require input direction")
        if d["type"] == "serial":
            if not list(
                filter(
                    lambda x: x["type"] == "serial"
                    and x["data-port"] == d["data-port"],
                    self.connections,
                )
            ):
                self.storage.connections.append(d)
        elif d["type"] == "network":
            if not list(
                filter(
                    lambda x: x["type"] == "network"
                    and x["host"] == d["host"]
                    and x["port"] == d["port"],
                    self.connections,
                )
            ):
                self.storage.connections.append(d)

        self.storage.save()
        self.plug_all()

    def remove_connection(self, d):
        self.storage.connections.remove(d)
        self.storage.save()
        self.plug_all()

    def poll(self):
        dd = []
        rf = 0

        with self._sources_lock:
            sources_snapshot = dict(self.sources)

        for key, ds in sources_snapshot.items():
            if not ds.connected:
                continue

            rf += 1
            try:
                d = ds.read()
                raw = getattr(ds, "raw_messages", [])
                if raw:
                    self.dispatch("nmea2000", raw)
                if len(d):
                    dd += d
                self._packets[key] = self._packets.get(key, 0) + (
                    len(raw) if raw else len(d)
                )
            except Exception as e:
                logger.error("Error reading data from source: %s", e)
                ds.connected = False

        if len(dd) > 0:
            self.dispatch("data", dd)

        if rf < len(sources_snapshot):
            logger.info("Reconnecting in 5 seconds...")
            self.stop_event.wait(timeout=5)
            self.plug_all()

        return len(dd)

    def poll_loop(self, b):
        while not self.stop_event.is_set():
            try:
                n = self.poll()
            except Exception as e:
                logger.error("Error polling data: %s", e)
                n = 0
            if n > 0:
                time.sleep(0.05)
            else:
                time.sleep(0.5)

    def start_polling(self):
        logger.info("Polling started")
        self._polling_active = True
        self._packets = {}
        self.stop_event.clear()
        self.plug_all()
        self.thread = Thread(target=self.poll_loop, args=(True,))
        self.thread.start()
