"""Measured polar samples, independent of the predicted polar and GPS position."""

import math
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque

import pynmea2


@dataclass(frozen=True)
class PerformanceSample:
    twa: float  # degrees clockwise from the bow, preserving tack
    tws: float  # knots
    speed: float  # knots
    basis: str  # stw or sog; never silently substitute one for the other


def wind_band(tws, winds):
    """Nearest predicted TWS, with midpoint boundaries and no extrapolation."""
    if not winds or not min(winds) <= tws <= max(winds):
        return None
    return min(range(len(winds)), key=lambda i: abs(winds[i] - tws))


def _number(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Expected a finite, nonnegative measurement")
    return value


class PerformanceHistory:
    """Pair each true-wind report with fresh speed, keeping bounded history.

    Live input uses monotonic seconds (five-second freshness, one sample/sec
    per speed basis). Untimestamped file input uses sentence distance instead;
    callers explicitly choose that policy rather than inventing sample times.
    """

    def __init__(self, capacity=20000, max_age=5.0, interval=1.0):
        self.samples: Deque[PerformanceSample] = deque(maxlen=capacity)
        self.max_age = max_age
        self.interval = interval
        self.lock = Lock()
        self.speeds = {}
        self.last_sample = {}
        self.revision = 0

    def clear(self):
        with self.lock:
            self.samples.clear()
            self.speeds.clear()
            self.last_sample.clear()
            self.revision += 1

    def snapshot(self):
        with self.lock:
            return self.revision, tuple(self.samples)

    def ingest(self, sentence, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            self._ingest(sentence, now)

    def _ingest(self, sentence, now):
        kind = getattr(sentence, "sentence_type", "")
        if kind in ("VHW", "VTG", "RMC"):
            self._speed(sentence, kind, now)
        elif kind == "MWV":
            self._wind(sentence, now)

    def _speed(self, sentence, kind, now):
        basis = "stw" if kind == "VHW" else "sog"
        try:
            if kind == "VHW":
                value = sentence.water_speed_knots
            elif kind == "VTG":
                if getattr(sentence, "faa_mode", "") == "N":
                    raise ValueError("Invalid GPS fix")
                value = sentence.spd_over_grnd_kts
            else:
                if sentence.status != "A":
                    raise ValueError("Invalid GPS fix")
                value = sentence.spd_over_grnd
            self.speeds[basis] = (_number(value), now)
        except (ValueError, TypeError, AttributeError):
            self.speeds.pop(basis, None)

    def _wind(self, sentence, now):
        try:
            if sentence.reference != "T" or sentence.status != "A":
                return
            factor = {"N": 1, "M": 1.943844492, "K": 1 / 1.852}[
                sentence.wind_speed_units
            ]
            tws = _number(sentence.wind_speed) * factor
            twa = _number(sentence.wind_angle)
            if twa > 360 or tws <= 0:
                return
        except (ValueError, TypeError, AttributeError, KeyError):
            return
        for basis, (speed, when) in self.speeds.items():
            if not 0 <= now - when <= self.max_age:
                continue
            if now - self.last_sample.get(basis, -math.inf) < self.interval:
                continue
            self.samples.append(PerformanceSample(twa % 360, tws, speed, basis))
            self.last_sample[basis] = now
            self.revision += 1


def load_nmea_performance(path):
    """Read interpreted NMEA logs; pair speed within 50 preceding sentences.

    These recordings have no receipt timestamps, so there is no time-based
    resampling. Raw N2K archives should use their interpreted .log companion.
    """
    history = PerformanceHistory(max_age=50, interval=0)
    with open(path, encoding="utf-8", errors="replace") as stream:
        for index, line in enumerate(stream):
            try:
                history.ingest(pynmea2.parse(line.strip()), now=index)
            except (ValueError, pynmea2.ParseError):
                continue
    return history.snapshot()[1]
