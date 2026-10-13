"""NMEA 2000 gateway inputs for serial and network transports."""

import logging
import select
import socket

import serial  # type: ignore[import-untyped]

from .actisense import ActisenseDecoder, frame
from .nmea2000 import N2KAsciiDecoder, instruments

logger = logging.getLogger("gweatherrouting")

N2K_FORMATS = {
    "actisense": {
        "label": "NMEA 2000 — Actisense BST",
        "baudrate": 230400,
        "decoder": ActisenseDecoder,
        "configure_gateway": True,
    },
    "n2k_ascii": {
        "label": "NMEA 2000 — N2K ASCII",
        "baudrate": 115200,
        "decoder": N2KAsciiDecoder,
        "configure_gateway": False,
    },
}
N2K_PROTOCOLS = frozenset(N2K_FORMATS)


class NMEA2000DataSource:
    """Read complete PGNs from a gateway using a selected wire format.

    Transport and gateway format are configuration choices. Every decoder
    emits the same N2KMessage model before PGN conversion or raw recording.
    """

    def __init__(self, config):
        self.config = config
        self.protocol = config["protocol"]
        self.direction = config.get("direction", "in")
        self.connection_type = config["type"]
        self.baudrate = config.get("baudrate", 230400)
        self.connected = False
        self.serial_conn = None
        self.socket_conn = None
        self.raw_messages = []

        if self.direction != "in":
            raise ValueError("NMEA 2000 connections currently support input only")
        if self.protocol not in N2K_PROTOCOLS:
            raise ValueError("Unsupported NMEA 2000 gateway format")
        self.decoder = self._new_decoder()

    def _new_decoder(self):
        return N2K_FORMATS[self.protocol]["decoder"]()

    @property
    def name(self):
        if self.connection_type == "serial":
            return self.config["data-port"]
        return f"{self.config['host']}:{self.config['port']}"

    def connect(self):
        self.close()
        self.decoder = self._new_decoder()
        self.raw_messages = []
        try:
            if self.connection_type == "serial":
                self.serial_conn = serial.Serial(
                    self.config["data-port"],
                    baudrate=self.baudrate,
                    timeout=0,
                    write_timeout=1,
                )
                writer = self.serial_conn.write
            elif self.connection_type == "network":
                kind = (
                    socket.SOCK_STREAM
                    if self.config["network"] == "tcp"
                    else socket.SOCK_DGRAM
                )
                self.socket_conn = socket.socket(socket.AF_INET, kind)
                self.socket_conn.settimeout(2)
                self.socket_conn.connect((self.config["host"], self.config["port"]))
                writer = self.socket_conn.send
            else:
                raise ValueError("Unsupported NMEA 2000 transport")

            if N2K_FORMATS[self.protocol]["configure_gateway"]:
                # This gateway-local command selects receive-all output; it
                # does not transmit vessel-control PGNs onto the bus.
                writer(frame(0xA1, b"\x11\x02\x00"))
            self.connected = True
            return True
        except (OSError, serial.SerialException, ValueError):
            logger.exception("Cannot connect to NMEA 2000 input %s", self.name)
            self.close()
            return False

    def _read_chunk(self):
        if self.serial_conn is not None:
            available = self.serial_conn.in_waiting
            return self.serial_conn.read(min(available, 65536)) if available else b""
        if self.socket_conn is not None:
            if not select.select([self.socket_conn], [], [], 0)[0]:
                return b""
            chunk = self.socket_conn.recv(65536)
            if not chunk and self.config["network"] == "tcp":
                self.connected = False
            return chunk
        return b""

    def read(self):
        self.raw_messages = []
        if not self.connected:
            return []
        try:
            chunk = self._read_chunk()
            if not chunk:
                return []
            self.raw_messages = self.decoder.feed(chunk)
        except (OSError, serial.SerialException):
            logger.warning("NMEA 2000 connection lost: %s", self.name)
            self.close()
            return []
        packets = []
        for message in self.raw_messages:
            packets.extend(instruments(message))
        return packets

    def close(self):
        for attr in ("serial_conn", "socket_conn"):
            connection = getattr(self, attr)
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
                setattr(self, attr, None)
        self.connected = False
