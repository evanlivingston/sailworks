"""Strict parsing of NOAA GFS-Wave GRIB index records."""

import re


ELEMENTS_BY_FIELD = {
    "wave_height": "HTSGW",
    "wave_period": "PERPW",
    "wave_direction": "DIRPW",
}
_FIELD_BY_ELEMENT = {element: field for field, element in ELEMENTS_BY_FIELD.items()}
_FCST = re.compile(r"(?:^|\s)(\d+)\s*hour\s*(?:fcst|forecast)(?:\s|$)", re.I)


def parse_index(text, date, cycle, expected_hour):
    """Return parsed records and requested wave records for one forecast hour."""
    records, ids, offsets = [], set(), set()
    previous_offset = -1
    pinned_reference = date + cycle
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        pieces = line.split(":")
        if len(pieces) < 6:
            raise ValueError(f"Malformed GFS-Wave index record {line_number}")
        try:
            record_id, offset = int(pieces[0]), int(pieces[1])
        except ValueError as exc:
            raise ValueError(
                f"Malformed GFS-Wave index offset at line {line_number}"
            ) from exc
        if record_id < 0 or offset < 0 or offset <= previous_offset:
            raise ValueError("GFS-Wave index IDs and offsets must increase")
        if record_id in ids or offset in offsets:
            raise ValueError("GFS-Wave index contains duplicate IDs or offsets")
        ids.add(record_id)
        offsets.add(offset)
        previous_offset = offset
        references = [part[2:] for part in pieces if part.startswith("d=")]
        if len(references) != 1 or references[0] != pinned_reference:
            raise ValueError("GFS-Wave index reference time differs from pinned run")
        element, level = pieces[3].strip(), " ".join(pieces[4].split()).lower()
        time_words = ":".join(pieces[5:]).strip().lower()
        if re.search(r"(?:^|:)\s*anl(?:\s|:|$)", time_words):
            lead = 0
        else:
            match = _FCST.search(time_words.replace(":", " "))
            lead = int(match.group(1)) if match is not None else None
        records.append(
            {
                "record_id": record_id,
                "offset": offset,
                "element": element,
                "level": level,
                "lead": lead,
            }
        )

    selected = {}
    for record in records:
        field = _FIELD_BY_ELEMENT.get(record["element"])
        if field is None:
            continue
        if record["level"] != "surface":
            raise ValueError(
                f"GFS-Wave {record['element']} has unexpected level {record['level']!r}"
            )
        if record["lead"] != expected_hour:
            continue
        if field in selected:
            raise ValueError(
                f"GFS-Wave index duplicates {record['element']} at hour {expected_hour}"
            )
        selected[field] = record
    return records, selected
