"""Capture-only device inventory; not an instrument fast-packet decoder.

Field layout verified against canboat's docs/canboat.json, PGNs 60928 and
126996: https://github.com/canboat/canboat/blob/master/docs/canboat.json
NAME manufacturer is bits 21..31; product fields occupy 134 bytes. Numeric
codes are retained and displayed with offline CANboat manufacturer names.
Identity is observational:
an unrecorded address claim cannot be inferred from instrument traffic.
Fast-packet header masks/offsets also checked against canboat's
analyzer/analyzer.c; this collector deliberately rejects
out-of-order fragments rather than attempting speculative recovery.
"""

from dataclasses import dataclass, field
from typing import Optional

from .n2kmanufacturers import manufacturer_name

# Condensed descriptions from CANboat v9.0.0 docs/canboat.json. The full
# dataset is Apache-2.0; see data/CANBOAT-LICENSE.txt.
CANBOAT_DEVICE_CLASSES = {
    0: "Reserved for NMEA 2000",
    10: "System tools",
    20: "Safety systems",
    25: "Internetwork device",
    30: "Electrical distribution",
    35: "Electrical generation",
    40: "Steering and control surfaces",
    50: "Propulsion",
    60: "Navigation",
    70: "Communication",
    75: "Sensor communication interface",
    80: "Instrumentation / general systems",
    85: "External environment",
    90: "Internal environment",
    100: "Deck / cargo / fishing equipment",
    110: "Human interface",
    120: "Display",
    125: "Entertainment",
}

CANBOAT_PGN_DESCRIPTIONS = {
    59392: "ISO Acknowledgement",
    59904: "ISO Request",
    60928: "ISO Address Claim",
    61184: "Manufacturer-specific addressed message",
    65013: "Utility Phase A AC Power",
    65014: "Utility Phase A Basic AC Quantities",
    65016: "Utility Total AC Power",
    65408: "Airmar Depth Quality Factor",
    126208: "NMEA Group Function",
    126720: "Manufacturer-specific fast-packet message",
    126992: "System Time",
    126993: "Heartbeat",
    126996: "Product Information",
    126998: "Configuration Information",
    127245: "Rudder",
    127245: "Rudder",
    127250: "Vessel Heading",
    127251: "Rate of Turn",
    127257: "Attitude",
    127258: "Magnetic Variation",
    127488: "Engine Parameters, Rapid Update",
    127489: "Engine Parameters, Dynamic",
    127488: "Engine Parameters, Rapid Update",
    127489: "Engine Parameters, Dynamic",
    127493: "Transmission Parameters, Dynamic",
    127501: "Binary Switch Bank Status",
    127505: "Fluid Level",
    127506: "DC Detailed Status",
    127507: "Charger Status",
    127508: "Battery Status",
    127509: "Inverter Status",
    128259: "Speed (water referenced)",
    128267: "Water Depth",
    128275: "Distance Log",
    129025: "Position, Rapid Update",
    129026: "COG & SOG, Rapid Update",
    129029: "GNSS Position Data",
    129038: "AIS Class A Position Report",
    129039: "AIS Class B Position Report",
    129040: "AIS Class B Extended Position Report",
    129041: "AIS Aids to Navigation Report",
    129283: "Cross Track Error",
    129284: "Navigation Data",
    129539: "GNSS DOPs",
    129540: "GNSS Satellites in View",
    129793: "AIS UTC and Date Report",
    129794: "AIS Class A Static and Voyage Data",
    129797: "AIS Binary Broadcast Message",
    129809: "AIS Class B Static Data, Part A",
    129810: "AIS Class B Static Data, Part B",
    130306: "Wind Data",
    130310: "Environmental Parameters (obsolete)",
    130311: "Environmental Parameters",
    130312: "Temperature",
    130316: "Temperature, Extended Range",
    130820: "Manufacturer-specific message",
    130944: "Airmar POST",
}


def canboat_device_class(device):
    """Return the CANboat NAME device-class description, if a NAME was captured."""
    if device.name is None:
        return None
    code = (device.name >> 49) & 0x7F
    description = CANBOAT_DEVICE_CLASSES.get(code)
    return f"{description} ({code})" if description else f"Unknown class ({code})"


def canboat_pgn_description(pgn):
    """Return the condensed CANboat PGN description, if known."""
    return CANBOAT_PGN_DESCRIPTIONS.get(int(pgn))


@dataclass(frozen=True)
class ProductInformation:
    protocol_version: Optional[int]
    product_code: Optional[int]
    model: str
    software: str
    model_version: str
    serial: str
    certification: Optional[int]
    load_equivalency: Optional[int]


@dataclass(frozen=True)
class CapturedDevice:
    source: int
    epoch: int
    name: Optional[int]
    manufacturer: Optional[int]
    product: Optional[ProductInformation]
    frames: int
    pgn_counts: tuple
    other_pgn_frames: int
    first_frame: int
    last_frame: int
    identity_limited: bool = False

    @property
    def name_hex(self):
        return f"0x{self.name:016X}" if self.name is not None else None


def captured_device_from_dict(data):
    """Restore the typed device record stored in the log-library JSON index."""
    product = data.get("product")
    if isinstance(product, dict):
        product = ProductInformation(**product)
    return CapturedDevice(
        source=data["source"],
        epoch=data["epoch"],
        name=data.get("name"),
        manufacturer=data.get("manufacturer"),
        product=product,
        frames=data["frames"],
        pgn_counts=tuple(tuple(pair) for pair in data.get("pgn_counts", ())),
        other_pgn_frames=data.get("other_pgn_frames", 0),
        first_frame=data.get("first_frame", 0),
        last_frame=data.get("last_frame", 0),
        identity_limited=data.get("identity_limited", False),
    )


def source_label(devices, source):
    """Never name an address-wide track after just one of its occupants."""
    occupants = [device for device in devices if device.source == source]
    fallback = f"Source {source}"
    if len(occupants) != 1 or occupants[0].identity_limited:
        return fallback
    product = occupants[0].product
    if product and product.model:
        return f"{product.model} (Source {source})"
    manufacturer = manufacturer_name(occupants[0].manufacturer)
    return f"{manufacturer} (Source {source})" if manufacturer else fallback


def _number(value, bits):
    return value if value < (1 << bits) - 3 else None


def _text(data):
    # STRING_FIX is ASCII, terminated/padded by NUL or FF. Do not let captured
    # control characters become UI markup or multiline picker labels.
    raw = data.split(b"\x00", 1)[0].split(b"\xff", 1)[0]
    return "".join(chr(value) if 32 <= value < 127 else "?" for value in raw).strip()


def _product(data):
    return ProductInformation(
        _number(int.from_bytes(data[:2], "little"), 16),
        _number(int.from_bytes(data[2:4], "little"), 16),
        *(_text(data[start : start + 32]) for start in (4, 36, 68, 100)),
        _number(data[132], 8),
        _number(data[133], 8),
    )


@dataclass
class _Epoch:
    source: int
    epoch: int
    first_frame: int
    name: Optional[int] = None
    product: Optional[ProductInformation] = None
    frames: int = 0
    last_frame: int = 0
    pgn_counts: dict = field(default_factory=dict)
    other_pgn_frames: int = 0
    identity_limited: bool = False


@dataclass
class _Packet:
    data: bytearray
    next_index: int
    started: int
    timestamp: object


class DeviceCollector:
    """Observe all raw CAN frames before any instrument allowlist.

    Only exact 134-byte product messages are assembled, in strict order.
    Assemblies are bounded by size, concurrent count, frame age and UTC age.
    A new NAME invalidates every incomplete product for that address. The
    first claim may identify preceding anonymous fixes, but never inherits an
    already captured anonymous product (its ownership is unknowable).
    """

    MAX_PENDING = 64
    MAX_FRAME_AGE = 512
    MAX_SECONDS = 5
    MAX_EPOCHS = 8  # per address; final bucket becomes explicitly unidentified
    MAX_PGNS = 128  # per epoch, with a counted overflow bucket

    def __init__(self):
        self.frames = 0
        self.epochs = {}
        self.pending = {}
        self.issues = {}

    def _issue(self, text):
        self.issues[text] = self.issues.get(text, 0) + 1

    def _clear_source(self, source):
        for key in list(self.pending):
            if key[0] == source:
                del self.pending[key]
                self._issue("product assembly interrupted by identity change")

    def _new_epoch(self, source):
        epochs = self.epochs.setdefault(source, [])
        if len(epochs) >= self.MAX_EPOCHS:
            current = epochs[-1]
            current.name = current.product = None
            current.identity_limited = True
            self._issue("device identity epoch limit reached")
            return current
        current = _Epoch(source, len(epochs) + 1, self.frames)
        epochs.append(current)
        return current

    def observe(self, message):
        self.frames += 1
        for key, packet in list(self.pending.items()):
            age = (message.timestamp - packet.timestamp).total_seconds()
            if (
                self.frames - packet.started > self.MAX_FRAME_AGE
                or age > self.MAX_SECONDS
                or age < 0
            ):
                del self.pending[key]
                self._issue("expired incomplete product information")
        source = message.source
        epochs = self.epochs.get(source)
        current = epochs[-1] if epochs else self._new_epoch(source)
        data = message.data
        if message.pgn == 60928:
            if len(data) == 8 and data != b"\xff" * 8:
                name = int.from_bytes(data, "little")
                if not current.identity_limited:
                    if current.name != name:
                        self._clear_source(source)
                        if current.name is not None or current.product is not None:
                            current = self._new_epoch(source)
                        if not current.identity_limited:
                            current.name = name
            else:
                self._issue("invalid address claim")
        if message.pgn == 126996 and not current.identity_limited:
            product = self._fragment(message)
            if product is not None:
                if current.product is not None and current.product != product:
                    # Conflicting products without an observed claim are not
                    # evidence that either belongs to the previous NAME.
                    self._clear_source(source)
                    current = self._new_epoch(source)
                    self._issue("conflicting products at one source address")
                if not current.identity_limited:
                    current.product = product
        current.frames += 1
        current.last_frame = self.frames
        if message.pgn in current.pgn_counts or len(current.pgn_counts) < self.MAX_PGNS:
            current.pgn_counts[message.pgn] = current.pgn_counts.get(message.pgn, 0) + 1
        else:
            current.other_pgn_frames += 1

    def _fragment(self, message):
        data = message.data
        if not data or len(data) > 8:
            self._issue("invalid product fragment length")
            return None
        sequence, index = data[0] >> 5, data[0] & 31
        key = (message.source, message.destination, sequence)
        if index == 0:
            if key in self.pending:
                del self.pending[key]
                self._issue("restarted incomplete product information")
            if len(data) != 8 or data[1] != 134:
                self._issue("invalid product information size")
                return None
            if len(self.pending) >= self.MAX_PENDING:
                del self.pending[next(iter(self.pending))]
                self._issue("product assembly capacity reached")
            self.pending[key] = _Packet(
                bytearray(data[2:]), 1, self.frames, message.timestamp
            )
            return None
        packet = self.pending.get(key)
        if packet is None:
            self._issue("orphan product fragment")
            return None
        needed = min(7, 134 - len(packet.data))
        if index != packet.next_index or len(data) < needed + 1:
            del self.pending[key]
            self._issue("out-of-order or truncated product fragment")
            return None
        packet.data.extend(data[1 : needed + 1])
        packet.next_index += 1
        if len(packet.data) == 134:
            del self.pending[key]
            return _product(packet.data)
        return None

    def devices(self):
        return tuple(
            CapturedDevice(
                state.source,
                state.epoch,
                state.name,
                (
                    _number((state.name >> 21) & 0x7FF, 11)
                    if state.name is not None
                    else None
                ),
                state.product,
                state.frames,
                tuple(sorted(state.pgn_counts.items())),
                state.other_pgn_frames,
                state.first_frame,
                state.last_frame,
                state.identity_limited,
            )
            for source in sorted(self.epochs)
            for state in self.epochs[source]
        )

    def diagnostics(self):
        messages = [
            f"Device metadata: {text} ({count:,})."
            for text, count in self.issues.items()
        ]
        if self.pending:
            messages.append(
                f"Device metadata: {len(self.pending)} incomplete product message(s) at EOF."
            )
        return tuple(messages)
