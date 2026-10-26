"""Small deterministic GRIB2/ranged-HTTP fixtures for the AI-GFS provider."""


def grib_message(tag, size=64):
    if size < 20:
        raise ValueError("GRIB fixture must fit section header and trailer")
    body = bytes([tag]) * (size - 20)
    return b"GRIB\x00\x00\x00\x02" + size.to_bytes(8, "big") + body + b"7777"


def index_record(record_id, offset, date, cycle, element, level, hour):
    valid = "anl" if hour == 0 else f"{hour} hour fcst"
    return f"{record_id}:{offset}:d={date}{cycle}:{element}:{level}:{valid}:surface"


def lead_fixture(date="20261025", cycle="12", hour=0, include_apcp=False):
    bands = [
        ("UGRD", "10 m above ground", 1),
        ("VGRD", "10 m above ground", 2),
        ("TMP", "2 m above ground", 3),
        ("PRMSL", "mean sea level", 4),
    ]
    if include_apcp:
        bands.append(("APCP", "surface", 5))
    messages, records, offset = [], [], 0
    for record_id, (element, level, tag) in enumerate(bands, 1):
        message = grib_message(tag)
        records.append(
            index_record(record_id, offset, date, cycle, element, level, hour)
        )
        messages.append(message)
        offset += len(message)
    return "\n".join(records), b"".join(messages), tuple(bands)


def index_for_hours(hours, date="20261025", cycle="12", apcp_hours=()):
    values = {}
    for hour in hours:
        text, data, bands = lead_fixture(
            date, cycle, hour, include_apcp=hour in apcp_hours
        )
        values[hour] = (text, data, bands)
    return values
