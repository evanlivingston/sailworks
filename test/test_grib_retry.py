"""Retry classification, backoff bounds and per-thread session reuse."""

import threading
from io import BytesIO

import pytest

from sailworks.core.gribsources import parallel, retry
from sailworks.core.gribsources import noaagfs
from sailworks.core.gribsources.parallel import write_ordered_forecasts
from sailworks.core.gribsources.ranged import HTTPStatusError, fetch_text


class FakeResponse:
    def __init__(self, body=b"", status=200):
        self.body = body
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class ScriptedSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(b"", status=self.statuses.pop(0))


@pytest.mark.parametrize("status", [302, 429, 500, 502, 503, 504])
def test_retryable_status_is_retried(monkeypatch, status):
    monkeypatch.setattr(retry, "_sleep", lambda _delay: None)
    session = ScriptedSession([status, 200])

    response = retry.get_with_retry(session, "url", timeout=(10, 60))

    assert response.status_code == 200
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [400, 403, 404])
def test_hard_status_is_returned_after_one_call(status):
    session = ScriptedSession([status, 200])

    response = retry.get_with_retry(session, "url", timeout=(10, 60))

    assert response.status_code == status
    assert len(session.calls) == 1


def test_backoff_delay_stays_within_its_jitter_bound():
    for attempt in range(6):
        ceiling = min(retry.RETRY_BACKOFF_CAP, retry.RETRY_BACKOFF_BASE * 2**attempt)
        for _ in range(200):
            assert 0.0 <= retry.backoff_delay(attempt) <= ceiling


def test_exhausted_retries_return_the_last_response(monkeypatch):
    monkeypatch.setattr(retry, "_sleep", lambda _delay: None)
    session = ScriptedSession([429] * 10)

    response = retry.get_with_retry(session, "url", timeout=(10, 60))

    assert response.status_code == 429
    assert len(session.calls) == retry.MAX_RETRY_ATTEMPTS


def test_fetch_text_surfaces_an_exhausted_throttle(monkeypatch):
    monkeypatch.setattr(retry, "_sleep", lambda _delay: None)
    session = ScriptedSession([429] * 10)

    with pytest.raises(HTTPStatusError) as error:
        fetch_text(session, "url")

    assert error.value.status_code == 429
    assert len(session.calls) == retry.MAX_RETRY_ATTEMPTS


def test_a_throttled_step_does_not_cancel_its_siblings(monkeypatch):
    monkeypatch.setattr(retry, "_sleep", lambda _delay: None)
    lock = threading.Lock()
    attempts = {0: 0, 1: 0}

    class Session:
        def get(self, _url, *, params, **_kwargs):
            step = params["step"]
            with lock:
                attempts[step] += 1
                attempt = attempts[step]
            if step == 0 and attempt <= 2:
                return FakeResponse(b"throttled", status=429)
            return FakeResponse(b"GRIB" + bytes((step,)))

    session = Session()
    requested = []

    def request(step):
        requested.append(step)
        return retry.get_with_retry(
            session, "url", params={"step": step}, timeout=(10, 60)
        )

    output = BytesIO()
    written = write_ordered_forecasts(
        output,
        (0, 1),
        request,
        lambda: None,
        lambda _percentage: None,
        model="GFS",
    )

    assert sorted(requested) == [0, 1]
    assert attempts == {0: 3, 1: 1}
    assert output.getvalue() == b"GRIB\x00GRIB\x01"
    assert written == 10


def test_thread_local_sessions_reuse_one_session_per_thread_and_close_all():
    created = []

    class Session:
        def __init__(self):
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    sessions = retry.ThreadLocalSessions(Session)
    first = sessions.get()
    assert sessions.get() is first

    other = []
    thread = threading.Thread(target=lambda: other.append(sessions.get()))
    thread.start()
    thread.join()

    assert other[0] is not first
    assert len(created) == 2

    sessions.close()
    assert all(session.closed for session in created)


def test_gfs_download_borrows_at_most_one_session_per_worker(monkeypatch):
    created = []
    lock = threading.Lock()

    class Session:
        def __init__(self):
            self.gets = 0
            with lock:
                created.append(self)

        def get(self, _url, **_kwargs):
            with lock:
                self.gets += 1
            return FakeResponse(b"GRIB\x00")

        def close(self):
            pass

    monkeypatch.setattr(noaagfs.requests, "Session", Session)
    source = noaagfs.NOAAGFSSource()

    source._write_forecasts(
        BytesIO(),
        tuple(range(8)),
        "20261015",
        "12",
        lambda _percentage: None,
        lambda: None,
    )

    assert 1 <= len(created) <= parallel.MAX_CONCURRENT_FORECAST_REQUESTS
    assert sum(session.gets for session in created) == 8
    assert max(session.gets for session in created) >= 2
