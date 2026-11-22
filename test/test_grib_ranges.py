from io import BytesIO

import pytest

from sailworks.core.gribsources.parallel import write_ordered_forecasts
from sailworks.core.gribsources.ranged import (
    HTTPStatusError,
    append_message,
    fetch_message,
    fetch_text,
    message_length,
)
from sailworks.core.marine.chs import DownloadCancelledError


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


def test_fetch_message_with_expected_length_uses_one_exact_request():
    message = synthetic_grib(b"wire bytes")
    offset = 40
    response = Response(message, headers=content_range(offset, len(message), 5000))
    session = Session(response)
    output = BytesIO()
    progress = []

    count = fetch_message(
        session,
        "remote",
        offset,
        output,
        expected_length=len(message),
        progress=progress.append,
    )

    assert count == len(message)
    assert output.getvalue() == message
    assert progress[-1] == len(message)
    assert response.closed
    assert len(session.calls) == 1
    headers = session.calls[0][1]["headers"]
    assert headers["Range"] == f"bytes={offset}-{offset + len(message) - 1}"


def test_fetch_message_continues_past_its_window_without_truncation():
    message = synthetic_grib(b"x" * 200)
    offset = 7
    window = 32
    total = 4096

    class RangedSession(Session):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            first, last = kwargs["headers"]["Range"][len("bytes=") :].split("-")
            first, last = int(first), int(last)
            body = message[first - offset : last - offset + 1]
            return Response(body, headers=content_range(first, len(body), total))

    session = RangedSession(None)
    output = BytesIO()

    count = fetch_message(session, "remote", offset, output, window=window)

    assert count == len(message)
    assert output.getvalue() == message
    assert [call[1]["headers"]["Range"] for call in session.calls] == [
        f"bytes={offset}-{offset + window - 1}",
        f"bytes={offset + window}-{offset + len(message) - 1}",
    ]


def test_fetch_message_rejects_header_length_that_differs_from_index():
    message = synthetic_grib(b"z" * 40)
    offset = 12
    expected_length = len(message) + 8
    response = Response(message, headers=content_range(offset, len(message), 5000))
    session = Session(response)
    output = BytesIO()

    with pytest.raises(ValueError, match="does not match"):
        fetch_message(
            session, "remote", offset, output, expected_length=expected_length
        )

    assert response.closed
    assert len(session.calls) == 1
    assert output.getvalue() == b""


def test_fetch_message_rejects_empty_continuation_after_header():
    message = synthetic_grib(b"y" * 200)
    offset = 0
    window = 32
    total = 4096

    class StallingSession(Session):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                body = message[:16]
                return Response(body, headers=content_range(offset, len(body), total))
            start = offset + 16
            return Response(b"", headers=content_range(start, len(message) - 16, total))

    session = StallingSession(None)
    output = BytesIO()

    with pytest.raises(ValueError, match="truncated"):
        fetch_message(session, "remote", offset, output, window=window)

    assert len(session.calls) == 2
    assert [call[1]["headers"]["Range"] for call in session.calls] == [
        f"bytes={offset}-{offset + window - 1}",
        f"bytes={offset + 16}-{offset + len(message) - 1}",
    ]


def test_write_ordered_forecasts_streams_64k_chunks_and_returns_bytes():
    chunk_sizes = []

    class RecordingResponse(Response):
        def iter_content(self, chunk_size):
            chunk_sizes.append(chunk_size)
            return super().iter_content(chunk_size)

    response = RecordingResponse(b"GRIBdata", status=200)
    response.raise_for_status = lambda: None
    output = BytesIO()

    write_ordered_forecasts(
        output,
        (0,),
        lambda _step: response,
        lambda: None,
        lambda _percentage: None,
    )

    assert chunk_sizes == [64 * 1024]
    assert output.getvalue() == b"GRIBdata"
    assert type(output.getvalue()) is bytes
