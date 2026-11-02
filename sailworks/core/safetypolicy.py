# -*- coding: utf-8 -*-
# Copyright (C) 2017-2026 Davide Gessa
"""
Boat-safety policy for routing: TWS caps, motoring fallback, wave proxy.

All speeds and thresholds are in knots, matching polar table units. A default
policy (no caps, no motor, no wave correction) is inert: speeds pass through
unchanged and no candidate is ever pruned for safety.

Over-limit wind is a prune, not a gap: the candidate is unnavigable, like
land. Callers in current-aware paths raise the shared
``InvalidMarineLegError`` so existing candidate filters apply; the wind-only
engine sees 0 boat speed and prunes by lack of progress.

Wave correction here is a wind-speed proxy (reduction above a threshold),
not a wave-model integration. It stands in until wave GRIBs (HTSGW fetch)
land; provenance records it as such.
"""

from dataclasses import dataclass
from typing import Optional
import math


@dataclass(frozen=True)
class SafetyPolicy:
    """Edge-case guardrails applied at every boat-speed evaluation."""

    tws_max_kn: Optional[float] = None
    min_sailing_speed_kn: Optional[float] = None
    motor_speed_kn: float = 5.0
    wave_correct: bool = False
    wave_threshold_kn: float = 20.0
    wave_max_reduction: float = 0.25

    def __post_init__(self):
        for name in (
            "tws_max_kn",
            "min_sailing_speed_kn",
            "motor_speed_kn",
            "wave_threshold_kn",
        ):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(name + " must be a finite positive speed")
        if (
            not math.isfinite(self.wave_max_reduction)
            or not 0 <= self.wave_max_reduction <= 1
        ):
            raise ValueError("Wave-proxy reduction must be between zero and one")

    @property
    def active(self) -> bool:
        return bool(
            self.tws_max_kn is not None
            or self.min_sailing_speed_kn is not None
            or self.wave_correct
        )

    def is_nogo(self, tws_kn: float) -> bool:
        return self.tws_max_kn is not None and tws_kn > self.tws_max_kn

    def apply(self, tws_kn: float, base_kn: float) -> float:
        """Adjust a polar boat speed. Never called for no-go conditions."""
        speed = base_kn
        if self.wave_correct and tws_kn > self.wave_threshold_kn:
            excess = min(
                (tws_kn - self.wave_threshold_kn) / self.wave_threshold_kn, 1.0
            )
            speed *= 1.0 - self.wave_max_reduction * excess
        if self.min_sailing_speed_kn is not None and speed < self.min_sailing_speed_kn:
            speed = self.motor_speed_kn
        return speed


def wind_limit_router(algorithm):
    """Prune capped wind-only candidates instead of retaining stationary points."""

    def calculate_isochrones(self, *args, **kwargs):
        frontiers = algorithm.calculate_isochrones(self, *args, **kwargs)
        policy = policy_of(self.polar)
        accepted = [point for point in frontiers[-1] if not policy.is_nogo(point.tws)]
        if frontiers[-1] and not accepted:
            raise ValueError(
                "No navigable route: forecast winds exceed the configured "
                "TWS limit in the search area."
            )
        return [*frontiers[:-1], accepted]

    return type(
        algorithm.__name__, (algorithm,), {"calculate_isochrones": calculate_isochrones}
    )


def policy_of(polar) -> SafetyPolicy:
    """Unwrap polar chains (SafetyPolar over efficiency wrappers).

    Uses instance dictionaries only: bare test doubles auto-create
    attributes on access, so plain getattr walks would invent policies.
    Unknown polars get the inert default.
    """
    current = polar
    seen: list = []
    while current is not None and len(seen) < 8:
        namespace = getattr(current, "__dict__", None)
        if not isinstance(namespace, dict):
            break
        own = namespace.get("_policy")
        if isinstance(own, SafetyPolicy):
            return own
        nxt = namespace.get("_polar")
        if nxt is None or nxt is current or any(n is nxt for n in seen):
            break
        seen.append(current)
        current = nxt
    return SafetyPolicy()


class SafetyPolar:
    """Wrap any polar object, enforcing a SafetyPolicy on every speed query."""

    def __init__(self, polar, policy: SafetyPolicy):
        self._polar = polar
        self._policy = policy

    def __getattr__(self, name):
        # Instance-dict lookup only: dunder probes during copy/pickle must
        # raise instead of recursing through a half-built wrapper.
        if name.startswith("__"):
            raise AttributeError(name)
        inner = self.__dict__.get("_polar")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    @property
    def safety_policy(self) -> SafetyPolicy:
        return self._policy

    def _base(self, tws: float, twa: float) -> float:
        return self._polar.get_speed(tws, twa)

    def get_speed(self, tws: float, twa: float) -> float:
        if self._policy.is_nogo(tws):
            return 0.0
        return self._policy.apply(tws, self._base(tws, twa))

    def apply_array(self, ws_kn, angles, base):
        """Vectorized adjust for numpy paths. No-go entries become NaN."""
        import numpy as np

        ws_kn = np.asarray(ws_kn, dtype=float)
        base = np.asarray(base, dtype=float)
        policy = self._policy
        nogo = (
            np.zeros_like(ws_kn, dtype=bool)
            if policy.tws_max_kn is None
            else ws_kn > policy.tws_max_kn
        )
        result = base.copy()
        if policy.wave_correct:
            excess = np.clip(
                (ws_kn - policy.wave_threshold_kn) / policy.wave_threshold_kn, 0.0, 1.0
            )
            result = result * (1.0 - policy.wave_max_reduction * excess)
        if policy.min_sailing_speed_kn is not None:
            result = np.where(
                result < policy.min_sailing_speed_kn, policy.motor_speed_kn, result
            )
        return np.where(nogo, np.nan, result), nogo

    def get_reaching(self, tws: float):
        speed, twa = self._polar.get_reaching(tws)
        if self._policy.is_nogo(tws):
            return (0.0, twa)
        return (self._policy.apply(tws, speed), twa)

    def get_max_vmgtwa(self, tws: float, twa: float):
        vmg, result = self._polar.get_max_vmgtwa(tws, twa)
        if self._policy.is_nogo(tws):
            return (0.0, result)
        return (self._policy.apply(tws, vmg), result)

    def get_max_vmg_up(self, tws: float):
        vmg, twa = self._polar.get_max_vmg_up(tws)
        if self._policy.is_nogo(tws):
            return (0.0, twa)
        return (self._policy.apply(tws, vmg), twa)

    def get_max_vmg_down(self, tws: float):
        vmg, twa = self._polar.get_max_vmg_down(tws)
        if self._policy.is_nogo(tws):
            return (0.0, twa)
        return (self._policy.apply(tws, vmg), twa)

    def get_routage_speed(self, tws, twa) -> float:
        if self._policy.is_nogo(tws):
            return 0.0
        return self._policy.apply(tws, self._polar.get_routage_speed(tws, twa))

    def get_twa_routage(self, tws: float, twa: float) -> float:
        return self._polar.get_twa_routage(tws, twa)

    def describe(self) -> dict:
        policy = self._policy
        return dict(
            tws_max_kn=policy.tws_max_kn,
            min_sailing_speed_kn=policy.min_sailing_speed_kn,
            motor_speed_kn=policy.motor_speed_kn,
            wave_correct=policy.wave_correct,
            wave_note=(
                "TWS-proxy reduction pending wave GRIBs"
                if policy.wave_correct
                else None
            ),
        )
