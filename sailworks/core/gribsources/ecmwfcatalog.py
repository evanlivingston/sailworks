"""Safe direct-child parsing and integer validation for ECMWF HTTP listings."""

from html.parser import HTMLParser
import json
import re
from urllib.parse import unquote, urljoin, urlsplit

import requests

DATE_PATTERN = re.compile(r"^\d{8}$")
CYCLE_PATTERN = re.compile(r"^(00|06|12|18)z$")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def listed_names(base, text):
    """Accept only same-origin direct children beneath the current directory."""
    parser = _Links()
    parser.feed(text)
    base_parts = urlsplit(base)
    names = set()
    for href in parser.hrefs:
        decoded = unquote(href)
        if "\\" in decoded or any(part == ".." for part in decoded.split("/")):
            continue
        url = urlsplit(urljoin(base, href))
        if (
            url.scheme != "https"
            or url.netloc != "data.ecmwf.int"
            or url.query
            or url.fragment
            or not url.path.startswith(base_parts.path)
        ):
            continue
        relative = url.path[len(base_parts.path) :].strip("/")
        if not relative or "/" in relative or relative in (".", ".."):
            continue
        names.add(relative)
    return names


def normal_index_int(value, label):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return int(value)
    raise ValueError(f"Invalid ECMWF index {label}")


def cycle_hour(directory_name):
    match = CYCLE_PATTERN.fullmatch(directory_name)
    if match is None:
        raise ValueError("Invalid ECMWF cycle directory")
    return directory_name[:2]


def session_for(session):
    return (session, False) if session is not None else (requests.Session(), True)


def parse_pinned_identifier(identifier, source_name, allowed_fields, steps_for_cycle):
    from .options import DownloadOptions

    try:
        config = json.loads(identifier)
        if not isinstance(config, dict) or set(config) != {
            "source",
            "date",
            "cycle",
            "hours",
            "steps",
            "fields",
        }:
            raise ValueError("unexpected identifier keys")
        if config["source"] != source_name:
            raise ValueError("source mismatch")
        date_text, cycle = config["date"], config["cycle"]
        if not isinstance(date_text, str) or not DATE_PATTERN.fullmatch(date_text):
            raise ValueError("invalid pinned date")
        from datetime import datetime

        datetime.strptime(date_text, "%Y%m%d")
        if not isinstance(cycle, str) or cycle not in ("00", "06", "12", "18"):
            raise ValueError("invalid pinned cycle")
        fields = tuple(config["fields"])
        if (
            not fields
            or len(set(fields)) != len(fields)
            or set(fields) - set(allowed_fields)
        ):
            raise ValueError("invalid pinned fields")
        steps = tuple(config["steps"])
        if not steps or any(type(step) is not int for step in steps):
            raise ValueError("invalid pinned steps")
        options = DownloadOptions(config["hours"], 0, fields, steps)
        if config["hours"] != max(steps) or steps_for_cycle(options, cycle) != steps:
            raise ValueError("pinned steps do not match ECMWF schedule")
        return date_text, cycle, fields, steps
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed ECMWF download identifier") from exc


def optional_catalog_text(session, url, cancel, fetch_text, status_error):
    try:
        return fetch_text(session, url, cancel=cancel)
    except status_error as exc:
        if exc.status_code == 404:
            return None
        raise
