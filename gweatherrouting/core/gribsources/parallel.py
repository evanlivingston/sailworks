"""Bounded parallel fetching with deterministic forecast-file assembly."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

MAX_CONCURRENT_FORECAST_REQUESTS = 4
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


class _ParallelRequestStopped(Exception):
    """A sibling request failed; stop work that has not finished yet."""


def write_ordered_forecasts(
    outfile,
    forecast_steps,
    request,
    check_cancel,
    percentage_callback,
    *,
    max_workers=MAX_CONCURRENT_FORECAST_REQUESTS,
):
    """Fetch a bounded window concurrently, appending GRIB bytes in step order."""

    def fetch(step):
        response = request(step)
        try:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                check_cancel()
                if not chunk:
                    continue
                if len(content) + len(chunk) > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        "Forecast download exceeded 256 MiB; choose a smaller route area"
                    )
                content.extend(chunk)
            return content
        finally:
            response.close()

    return write_ordered_payloads(
        outfile,
        forecast_steps,
        fetch,
        check_cancel,
        percentage_callback,
        max_workers=max_workers,
    )


def write_ordered_payloads(
    outfile,
    steps,
    fetch,
    check_cancel,
    progress_callback,
    *,
    max_workers=MAX_CONCURRENT_FORECAST_REQUESTS,
):
    """Fetch byte payloads concurrently, then write them in input order.

    ``fetch(step)`` must return a bytes-like payload. At most ``max_workers``
    tasks are submitted at once; completed payloads are retained only within
    that bounded window. The aggregate payload size is limited to 256 MiB.
    """
    steps = tuple(steps)
    if not steps:
        return 0
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    workers = min(max_workers, len(steps))
    executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="forecast-download"
    )
    pending = {}
    next_index = 0
    total_bytes = 0
    aborted = Event()
    budget_lock = Lock()

    def bounded_fetch(step):
        check_cancel()
        if aborted.is_set():
            raise _ParallelRequestStopped()
        payload = fetch(step)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("fetch must return a bytes-like payload")
        payload = bytes(payload)
        nonlocal total_bytes
        with budget_lock:
            if total_bytes + len(payload) > MAX_DOWNLOAD_BYTES:
                raise ValueError(
                    "Forecast download exceeded 256 MiB; choose a smaller route area"
                )
            total_bytes += len(payload)
        check_cancel()
        return payload

    try:
        while next_index < workers:
            pending[next_index] = executor.submit(bounded_fetch, steps[next_index])
            next_index += 1
        written = 0
        for index in range(len(steps)):
            check_cancel()
            try:
                payload = pending.pop(index).result()
            except BaseException:
                aborted.set()
                for future in pending.values():
                    future.cancel()
                raise
            check_cancel()
            outfile.write(payload)
            written += len(payload)
            progress_callback(int(100 * (index + 1) / len(steps)))
            if next_index < len(steps):
                pending[next_index] = executor.submit(bounded_fetch, steps[next_index])
                next_index += 1
        return written
    except BaseException:
        aborted.set()
        for future in pending.values():
            future.cancel()
        raise
    finally:
        aborted.set()
        executor.shutdown(wait=True, cancel_futures=True)
