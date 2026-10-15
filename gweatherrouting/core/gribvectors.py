"""Read GRIB2 vector-reference flags without decoding packed field data."""


def vector_flags(path):
    """One flag per field, including submessages; None means unknown template.

    GDAL exposes product metadata but not section 3's vector-reference flag.
    Non-GRIB fixtures and legacy GRIB1 retain the existing east/north assumption.
    """
    flags: list[bool | None] = []
    with open(path, "rb") as stream:
        while True:
            start = stream.tell()
            header = stream.read(16)
            if not header or header[:4] != b"GRIB" or header[7] != 2:
                return flags
            end = start + int.from_bytes(header[8:16], "big")
            if end < start + 20:
                raise ValueError("Invalid GRIB2 message length")
            relative = None
            while stream.tell() < end - 4:
                position = stream.tell()
                section = stream.read(5)
                length = int.from_bytes(section[:4], "big")
                if len(section) != 5 or length < 5 or position + length > end - 4:
                    raise ValueError("Invalid GRIB2 section length")
                if section[4] == 3:
                    grid = section + stream.read(min(length - 5, 76))
                    template = int.from_bytes(grid[12:14], "big")
                    offset = {0: 54, 1: 54, 40: 54, 10: 46, 20: 46, 30: 46}.get(
                        template
                    )
                    relative = bool(grid[offset] & 8) if offset is not None else None
                elif section[4] == 7:
                    flags.append(relative)
                stream.seek(position + length)
            if stream.read(4) != b"7777":
                raise ValueError("Truncated GRIB2 message")
            if stream.tell() != end:
                raise ValueError("Invalid GRIB2 message boundary")
