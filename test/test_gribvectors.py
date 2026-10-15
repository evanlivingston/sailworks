"""Vector flags survive concatenation and GRIB2 submessages."""

import tempfile
import unittest
from pathlib import Path

from gweatherrouting.core.gribvectors import vector_flags


def message(template, flag, fields=1):
    grid = bytearray(81)
    grid[:4] = len(grid).to_bytes(4, "big")
    grid[4] = 3
    grid[12:14] = template.to_bytes(2, "big")
    grid[54 if template in (0, 1, 40) else 46] = flag
    data = bytes(grid) + (b"\x00\x00\x00\x05\x07" * fields) + b"7777"
    return b"GRIB\x00\x00\x00\x02" + (16 + len(data)).to_bytes(8, "big") + data


class TestGribVectorFlags(unittest.TestCase):
    def test_flags_follow_each_field_and_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wind.grib2"
            path.write_bytes(message(30, 8, 2) + message(1, 48) + message(999, 8))
            self.assertEqual(vector_flags(path), [True, True, False, None])
            path.write_bytes(message(30, 8)[:-8])
            with self.assertRaises(ValueError):
                vector_flags(path)
