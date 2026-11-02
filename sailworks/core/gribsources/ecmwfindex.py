"""Strict selected-parameter validation for ECMWF per-message NDJSON indexes."""

import json

from .ecmwfcatalog import normal_index_int
from .ranged import fetch_text


def read_selected_index(
    session,
    url,
    date_text,
    cycle,
    step,
    fields,
    field_parameters,
    model_path,
    product_class,
    cancel,
):
    text = fetch_text(session, url, cancel=cancel, max_bytes=16 * 1024 * 1024)
    expected_parameters = {
        parameter for field in fields for parameter in field_parameters[field]
    }
    selected, ranges, experiment_versions = {}, [], set()
    expected_common = (
        "g",
        date_text,
        int(cycle) * 100,
        product_class,
        "fc",
        "oper",
    )
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Malformed ECMWF index JSON line") from exc
        if not isinstance(item, dict):
            raise ValueError("ECMWF index entries must be JSON objects")

        # Every row must belong to the pinned product run. Its lead, model,
        # level and experiment matter only when the parameter is selected.
        common = (
            item.get("domain"),
            str(item.get("date", "")),
            normal_index_int(item.get("time"), "time"),
            item.get("class"),
            item.get("type"),
            item.get("stream"),
        )
        if common != expected_common:
            raise ValueError("ECMWF index row belongs to a different product run")
        offset = normal_index_int(item.get("_offset"), "offset")
        length = normal_index_int(item.get("_length"), "length")
        if offset < 0 or length < 20 or length > 64 * 1024 * 1024:
            raise ValueError("ECMWF index range is invalid")
        ranges.append((offset, offset + length))

        parameter = item.get("param")
        if parameter not in expected_parameters:
            continue
        model = item.get("model")
        if (model_path == "aifs-single" and model != model_path) or (
            model is not None and model != model_path
        ):
            raise ValueError("ECMWF selected index entry has a different model")
        identity = (
            normal_index_int(item.get("step"), "step"),
            item.get("levtype"),
        )
        if identity != (step, "sfc"):
            raise ValueError(
                "Selected ECMWF index entry does not match pinned lead/level"
            )
        expver = item.get("expver")
        if expver is None or not str(expver):
            raise ValueError("ECMWF selected entry has no experiment version")
        experiment_versions.add(str(expver))
        if parameter in selected:
            raise ValueError(f"Duplicate ECMWF index parameter {parameter}")
        selected[parameter] = (offset, length)

    ranges.sort()
    if any(end > next_start for (_, end), (next_start, _) in zip(ranges, ranges[1:])):
        raise ValueError("ECMWF index contains overlapping byte ranges")
    if len(experiment_versions) != 1:
        raise ValueError("ECMWF selected index entries mix experiment versions")
    missing = expected_parameters - set(selected)
    if missing:
        raise ValueError(
            "ECMWF index is missing selected parameters: " + ", ".join(sorted(missing))
        )
    return tuple(
        selected[parameter] for field in fields for parameter in field_parameters[field]
    ), next(iter(experiment_versions))
