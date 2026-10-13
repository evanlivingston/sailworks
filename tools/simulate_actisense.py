#!/usr/bin/env python3
"""Simulate an NGT-1 USB stream on a macOS/Linux pseudo-terminal.

Run with Python 3 (no packages needed), then select the printed serial path in
Settings > Connections > Serial > NMEA 2000 — Actisense NGT-1 (input).
All positions and measurements are synthetic. Ctrl-C stops the simulator.
"""

import argparse
import math
import os
import pty
import select
import struct
import time
import tty


def encode(pgn, payload, source, milliseconds):
    header = bytes((2,)) + pgn.to_bytes(3, "little") + bytes((255, source))
    header += (milliseconds & 0xFFFFFFFF).to_bytes(4, "little") + bytes((len(payload),))
    body = bytes((0x93, len(header) + len(payload))) + header + payload
    body += bytes((-sum(body) & 255,))
    return b"\x10\x02" + body.replace(b"\x10", b"\x10\x10") + b"\x10\x03"


def sample(elapsed, latitude, longitude):
    """A synthetic sail with varying wind angle, speed and water depth."""
    twa = math.radians(95 + 50 * math.sin(elapsed / 30))
    heading = (-twa) % (2 * math.pi)
    boat_speed = 3.2 + 0.3 * math.sin(elapsed / 20)  # m/s through water
    true_wind = 6.2  # m/s, from north
    apparent_x = true_wind * math.cos(twa) + boat_speed
    apparent_y = true_wind * math.sin(twa)
    aws = math.hypot(apparent_x, apparent_y)
    awa = math.atan2(apparent_y, apparent_x) % (2 * math.pi)
    east = boat_speed * math.sin(heading) + 0.15
    north = boat_speed * math.cos(heading)
    sog = math.hypot(east, north)
    cog = math.atan2(east, north) % (2 * math.pi)
    payloads = [
        (127250, struct.pack("<BHhhB", 0, round(heading * 10000), 32767, 32767, 0), 40),
        (
            128259,
            struct.pack("<BHHBBB", 0, round(boat_speed * 100), 65535, 0, 255, 255),
            35,
        ),
        (
            129026,
            struct.pack("<BBHHH", 0, 0, round(cog * 10000), round(sog * 100), 65535),
            10,
        ),
        (
            130306,
            struct.pack("<BHHB", 0, round(aws * 100), round(awa * 10000), 2)
            + b"\xff\xff",
            35,
        ),
        (
            130306,
            struct.pack("<BHHB", 0, round(true_wind * 100), round(twa * 10000), 4)
            + b"\xff\xff",
            35,
        ),
        (
            128267,
            struct.pack("<BIhB", 0, round((25 + math.sin(elapsed / 15)) * 100), 0, 255),
            35,
        ),
        (130310, struct.pack("<BHHHB", 0, 28715, 65535, 65535, 255), 35),
        (129025, struct.pack("<ii", round(latitude * 1e7), round(longitude * 1e7)), 10),
    ]
    return (
        b"".join(
            encode(pgn, payload, source, round(elapsed * 1000))
            for pgn, payload, source in payloads
        ),
        north,
        east,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Seconds to run; default is until Ctrl-C",
    )
    args = parser.parse_args()
    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    print("SIMULATED instruments — no connection to a boat", flush=True)
    print(f"Serial port: {os.ttyname(slave)}", flush=True)
    print(
        "Select Actisense input, 230400 baud. Keep this terminal running.", flush=True
    )
    start = time.monotonic()
    previous = start
    latitude, longitude = 49.285, -123.24
    try:
        while not args.duration or time.monotonic() - start < args.duration:
            now = time.monotonic()
            elapsed = now - start
            data, north, east = sample(elapsed, latitude, longitude)
            latitude += north * (now - previous) / 1852 / 60
            longitude += (
                east * (now - previous) / 1852 / 60 / math.cos(math.radians(latitude))
            )
            previous = now
            if select.select([master], [], [], 0)[0]:
                os.read(master, 4096)  # Consume gateway setup commands.
            try:
                os.write(master, data)
            except BlockingIOError:
                pass  # No consumer yet; bounded OS buffer, no memory growth.
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        os.close(master)
        os.close(slave)


if __name__ == "__main__":
    main()
