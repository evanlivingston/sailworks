from io import BytesIO

import pytest

from gweatherrouting.core.gribsources.ranged import (
    HTTPStatusError,
    append_message,
    fetch_text,
    message_length,
)
from gweatherrouting.core.marine.chs import DownloadCancelledError


def synthetic_grib(payload=b"data"):
    length = 16 + len(payload) + 4
    return b"GRIB\0\0\0\2" + length.to_bytes(8, "big") + payload + b"7777"


class Response:
    def __init__(self, body, *, status=206, headers=None, chunk_size=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}
        self.chunk_size = chunk_size
        self.closed = False

    def iter_content(self, chunk_size):
        size = self.chunk_size or chunk_size
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def content_range(offset, length, total):
    return {"Content-Range": f"bytes {offset}-{offset + length - 1}/{total}"}


def test_fetch_text_is_bounded_cancellable_and_closes_response():
    response = Response(b"index", status=200)
    session = Session(response)
    assert fetch_text(session, "https://example.test/index") == "index"
    assert response.closed
    with pytest.raises(ValueError, match="size limit"):
        fetch_text(Session(Response(b"too long", status=200)), "url", max_bytes=2)

    class Cancel:
        def is_set(self):
            return True

    session = Session(Response(b"", status=200))
    with pytest.raises(DownloadCancelledError):
        fetch_text(session, "url", cancel=Cancel())
    assert not session.calls

    with pytest.raises(HTTPStatusError) as error:
        fetch_text(Session(Response(b"", status=404)), "https://example.test/missing")
    assert error.value.status_code == 404


def test_message_length_reads_exact_header_and_checks_remote_total():
    message = synthetic_grib()
    response = Response(message[:16], headers=content_range(12, 16, 1000))
    session = Session(response)
    assert message_length(session, "remote", 12) == len(message)
    assert response.closed
    headers = session.calls[0][1]["headers"]
    assert headers["Range"] == "bytes=12-27"
    assert headers["Accept-Encoding"] == "identity"

    bad = Response(message[:16], headers=content_range(0, 16, 20))
    with pytest.raises(ValueError, match="extends beyond"):
        message_length(Session(bad), "remote", 0)


@pytest.mark.parametrize(
    "response,match",
    [
        (Response(b"anything", status=200), "HTTP 206"),
        (
            Response(b"anything", headers={"Content-Range": "bytes 1-24/100"}),
            "does not match",
        ),
        (
            Response(
                b"anything",
                headers={"Content-Range": "bytes 0-23/100", "Content-Encoding": "gzip"},
            ),
            "encoding",
        ),
    ],
)
def test_append_rejects_nonexact_range_responses(response, match):
    output = BytesIO()
    length = len(synthetic_grib())
    with pytest.raises((ValueError, RuntimeError), match=match):
        append_message(Session(response), "remote", 0, length, output)
    assert response.closed


@pytest.mark.parametrize("variant", ["truncated", "excess", "header", "footer"])
def test_append_requires_exact_complete_grib_message(variant):
    message = synthetic_grib()
    body = message
    length = len(message)
    if variant == "truncated":
        body = message[:-1]
    elif variant == "excess":
        body = message + b"x"
    elif variant == "header":
        body = b"BAD!" + message[4:]
    elif variant == "footer":
        body = message[:-4] + b"0000"
    response = Response(body, headers=content_range(0, length, 1000))
    output = BytesIO()
    if variant == "excess":
        with pytest.raises(ValueError, match="excess"):
            append_message(Session(response), "remote", 0, length, output)
    else:
        with pytest.raises(ValueError):
            append_message(Session(response), "remote", 0, length, output)
    assert response.closed


def test_append_preserves_bytes_and_reports_cumulative_per_message_progress():
    message = synthetic_grib(b"wire bytes stay unchanged")
    response = Response(message, headers=content_range(20, len(message), 500))
    progress = []
    output = BytesIO()
    count = append_message(
        Session(response), "remote", 20, len(message), output, progress=progress.append
    )
    assert count == len(message)
    assert output.getvalue() == message
    assert progress[-1] == len(message)
