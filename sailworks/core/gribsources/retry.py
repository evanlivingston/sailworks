"""HTTP retry helpers for NOMADS filter requests.

NOMADS answers a filter request with a 302 redirect while a run is still being
published, and with HTTP 429 (or a 5xx) when the caller is over its request
quota. Those responses are transient: retrying the same step a few times with
jittered backoff usually succeeds, while treating the first one as final aborts
a multi-step forecast download halfway through. Other 4xx responses are hard
errors (bad request, forbidden, unknown step) and are returned unchanged so the
caller can surface its own message.
"""

import logging
import random
import threading
import time

import requests

logger = logging.getLogger("sailworks")

RETRYABLE_STATUSES = frozenset({302, 429, *range(500, 600)})
MAX_RETRY_ATTEMPTS = 4
RETRY_BACKOFF_BASE = 0.5
RETRY_BACKOFF_CAP = 4.0

# Module alias so tests can patch the wait without sleeping in real time.
_sleep = time.sleep


def is_retryable_status(status):
    """Return True when ``status`` is a transient NOMADS throttle response."""
    return status in RETRYABLE_STATUSES


def backoff_delay(attempt):
    """Full-jitter exponential backoff in seconds for a zero-based attempt."""
    ceiling = min(RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * 2**attempt)
    return random.uniform(0.0, ceiling)


def _raise_if_cancelled(cancel):
    """Abort before issuing a request once the caller has cancelled."""
    if cancel is not None and cancel.is_set():
        from sailworks.core.marine.chs import DownloadCancelledError

        raise DownloadCancelledError()


def _wait(cancel, delay):
    if cancel is None:
        _sleep(delay)
        return
    if cancel.wait(delay):
        from sailworks.core.marine.chs import DownloadCancelledError

        raise DownloadCancelledError()


def get_with_retry(
    session,
    url,
    *,
    params=None,
    headers=None,
    stream=True,
    timeout,
    allow_redirects=False,
    cancel=None,
    max_attempts=MAX_RETRY_ATTEMPTS,
):
    """GET ``url``, retrying transient throttle statuses with jittered backoff.

    A non-retryable status is returned immediately so callers keep their own
    hard errors. After the final attempt the last response is returned
    unchanged, letting the caller report the status it actually saw. Responses
    replaced by a retry are closed before waiting.
    """
    response = None
    for attempt in range(max_attempts):
        _raise_if_cancelled(cancel)
        response = session.get(
            url,
            params=params,
            headers=headers,
            stream=stream,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
        if not is_retryable_status(response.status_code):
            return response
        if attempt + 1 >= max_attempts:
            break
        delay = backoff_delay(attempt)
        logger.debug(
            "Retrying %s after HTTP %d in %.2fs (attempt %d/%d)",
            url,
            response.status_code,
            delay,
            attempt + 1,
            max_attempts,
        )
        close = getattr(response, "close", None)
        if close is not None:
            close()
        _wait(cancel, delay)
    return response


class ThreadLocalSessions:
    """Per-thread ``requests.Session`` cache shared by one download.

    A ``Session`` is not guaranteed to be thread-safe, while parallel forecast
    downloads issue one request per worker thread. This keeps at most one
    session per thread, records it, and closes everything when the download
    finishes, mirroring the per-worker sessions ECMWF and ECCC already create.
    """

    def __init__(self, factory=None):
        self._factory = factory or requests.Session
        self._local = threading.local()
        self._sessions = []
        self._lock = threading.Lock()

    def get(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._factory()
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
            logger.debug("Created HTTP session for thread %s", threading.get_ident())
        return session

    def close(self):
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            close = getattr(session, "close", None)
            if close is not None:
                close()
        self._local = threading.local()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False
