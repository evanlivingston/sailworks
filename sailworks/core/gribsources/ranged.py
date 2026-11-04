"""Strict bounded HTTP helpers for ECMWF/NOAA range-based GRIB downloads."""

import re

_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
GRIB_MESSAGE_LIMIT = 64 * 1024 * 1024
_TIMEOUT = (10, 60)


class HTTPStatusError(RuntimeError):
    """Unexpected HTTP status while fetching text or a byte range."""

    def __init__(self, expected, response, url):
        self.status_code = response.status_code
        self.url = url
        super().__init__(
            f"Expected HTTP {expected}, got {response.status_code} for {url}"
        )


def _check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        from sailworks.core.marine.chs import DownloadCancelledError

        raise DownloadCancelledError()


class MarkedStatusError(HTTPStatusError):
    """A failed response whose bounded body contains a caller's marker.

    Raised by :func:`fetch_text` so callers can recognise a meaningful error
    page (such as a rate-limit notice) from the response that actually failed.
    """


def _close(response):
    close = getattr(response, "close", None)
    if close is not None:
        close()


def _read_bounded(response, limit, cancel):
    body = bytearray()
    for chunk in response.iter_content(chunk_size=4096):
        _check_cancel(cancel)
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) >= limit:
            break
    return bytes(body[:limit])


def _request(session, url, *, headers=None, stream=True):
    request_headers = {"Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    return session.get(
        url,
        headers=request_headers,
        stream=stream,
        timeout=_TIMEOUT,
        allow_redirects=False,
    )


def _response_header(response, name):
    headers = getattr(response, "headers", {})
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _require_identity_encoding(response):
    encoding = _response_header(response, "Content-Encoding")
    if encoding and encoding.lower().strip() != "identity":
        raise ValueError("HTTP range response used content encoding")


def fetch_text(
    session,
    url,
    *,
    cancel=None,
    max_bytes=4 * 1024 * 1024,
    error_marker=None,
    error_statuses=(),
    error_bytes=4096,
):
    """Fetch bounded UTF-8 catalog/index text; only HTTP 200 is accepted.

    ``error_marker`` inspects the bounded body of a failed response with one of
    ``error_statuses`` before it is closed, raising :class:`MarkedStatusError`
    when the marker is present. The body of other failures is never read.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if error_marker is not None and (
        not isinstance(error_marker, bytes)
        or type(error_bytes) is not int
        or error_bytes <= 0
    ):
        raise ValueError("error_marker must be bytes with a positive error_bytes")
    _check_cancel(cancel)
    response = _request(session, url)
    try:
        if response.status_code != 200:
            if (
                error_marker is not None
                and response.status_code in error_statuses
                and error_marker in _read_bounded(response, error_bytes, cancel)
            ):
                raise MarkedStatusError(200, response, url)
            raise HTTPStatusError(200, response, url)
        _require_identity_encoding(response)
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            _check_cancel(cancel)
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("text resource exceeds configured size limit")
            chunks.append(chunk)
        _check_cancel(cancel)
        return b"".join(chunks).decode("utf-8")
    finally:
        _close(response)


def _range_total(response, offset, end, url):
    if response.status_code != 206:
        raise HTTPStatusError(206, response, url)
    _require_identity_encoding(response)
    value = _response_header(response, "Content-Range")
    match = _CONTENT_RANGE.fullmatch(value or "")
    if match is None:
        raise ValueError("Range response has invalid Content-Range")
    actual_start, actual_end, total = map(int, match.groups())
    if actual_start != offset or actual_end != end or total <= actual_end:
        raise ValueError("Range response does not match requested byte interval")
    return total


def _grib_header(header, max_bytes):
    if len(header) != 16 or header[:4] != b"GRIB" or header[7] != 2:
        raise ValueError("Range does not begin with a GRIB2 message")
    length = int.from_bytes(header[8:16], "big")
    if length < 20 or length > max_bytes:
        raise ValueError("GRIB2 message length is outside the allowed range")
    return length


def message_length(session, url, offset, *, cancel=None, max_bytes=GRIB_MESSAGE_LIMIT):
    """Read and validate only the 16-byte GRIB2 header at the remote offset."""
    if (
        type(offset) is not int
        or offset < 0
        or type(max_bytes) is not int
        or max_bytes < 20
    ):
        raise ValueError("invalid offset or message size limit")
    _check_cancel(cancel)
    response = _request(
        session,
        url,
        headers={
            "Range": f"bytes={offset}-{offset + 15}",
            "Accept-Encoding": "identity",
        },
    )
    try:
        total = _range_total(response, offset, offset + 15, url)
        content = bytearray()
        for chunk in response.iter_content(chunk_size=16):
            _check_cancel(cancel)
            if chunk:
                content.extend(chunk)
                if len(content) > 16:
                    raise ValueError("GRIB header range returned excess bytes")
        _check_cancel(cancel)
        length = _grib_header(bytes(content), max_bytes)
        if offset + length > total:
            raise ValueError("GRIB message extends beyond remote object")
        return length
    finally:
        _close(response)


def append_message(
    session,
    url,
    offset,
    length,
    output,
    *,
    cancel=None,
    progress=None,
    max_bytes=GRIB_MESSAGE_LIMIT,
):
    """Append one GRIB2 message from a strict 206.

    ``progress`` receives cumulative bytes for this message after each chunk.
    """
    if (
        type(offset) is not int
        or offset < 0
        or type(length) is not int
        or length < 20
        or type(max_bytes) is not int
        or max_bytes < 20
        or length > max_bytes
    ):
        raise ValueError("invalid message range or size limit")
    _check_cancel(cancel)
    end = offset + length - 1
    response = _request(
        session,
        url,
        headers={"Range": f"bytes={offset}-{end}", "Accept-Encoding": "identity"},
    )
    try:
        _range_total(response, offset, end, url)
        count = 0
        header = bytearray()
        tail = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            _check_cancel(cancel)
            if not chunk:
                continue
            if count + len(chunk) > length:
                raise ValueError("Range response contains excess GRIB bytes")
            if len(header) < 16:
                header.extend(chunk[: 16 - len(header)])
            tail.extend(chunk)
            if len(tail) > 4:
                del tail[:-4]
            output.write(chunk)
            count += len(chunk)
            if progress is not None:
                progress(count)
        _check_cancel(cancel)
        if count != length:
            raise ValueError("Range response is truncated")
        if _grib_header(bytes(header), max_bytes) != length:
            raise ValueError("GRIB header length does not match indexed message length")
        if bytes(tail) != b"7777":
            raise ValueError("GRIB2 message has no end marker")
        return count
    finally:
        _close(response)
