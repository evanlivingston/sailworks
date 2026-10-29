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
    steps = tuple(forecast_steps)
    if not steps:
        return 0
    workers = min(max_workers, len(steps))
    aborted = Event()
    budget_lock = Lock()
    downloaded = [0]

    def fetch(step):
        check_cancel()
        if aborted.is_set():
            raise _ParallelRequestStopped()
        response = request(step)
        try:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                check_cancel()
                if aborted.is_set():
                    raise _ParallelRequestStopped()
                if not chunk:
                    continue
                with budget_lock:
                    if downloaded[0] + len(chunk) > MAX_DOWNLOAD_BYTES:
                        raise ValueError(
                            "Forecast download exceeded 256 MiB; "
                            "choose a smaller route area"
                        )
                    downloaded[0] += len(chunk)
                content.extend(chunk)
            check_cancel()
            return content
        finally:
            response.close()

    executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="forecast-download"
    )
    pending = {}
    next_index = 0
    bytes_written = 0
    try:
        while next_index < workers:
            pending[next_index] = executor.submit(fetch, steps[next_index])
            next_index += 1

        for index, _step in enumerate(steps):
            check_cancel()
            try:
                content = pending.pop(index).result()
            except BaseException:
                aborted.set()
                for future in pending.values():
                    future.cancel()
                raise
            check_cancel()
            outfile.write(content)
            bytes_written += len(content)
            percentage_callback(int(100 * (index + 1) / len(steps)))
            if next_index < len(steps):
                pending[next_index] = executor.submit(fetch, steps[next_index])
                next_index += 1
        return bytes_written
    except BaseException:
        aborted.set()
        for future in pending.values():
            future.cancel()
        raise
    finally:
        aborted.set()
        executor.shutdown(wait=True, cancel_futures=True)
