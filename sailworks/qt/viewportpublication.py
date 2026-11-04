"""Coherent layer publication state and atomic rebasing.

This module provides the core publication state machine for retained geographic
layers. It separates visible/pending resource sets, enforces native budgets,
and provides an atomic rebase operation that rebases all retained layers and
rejects pending work in a single transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import threading


from enum import Enum


class LayerRole(str, Enum):
    BARBS = "barbs"
    FILL = "fill"
    BARBS_BACK = "barbs_back"
    FILL_BACK = "fill_back"
    CHART = "chart"
    STREAMLINES = "streamlines"


class ResourceKind(str, Enum):
    IMAGE = "image"
    TEXTURE = "texture"
    NATIVE = "native"


@dataclass(frozen=True)
class LayerIdentity:
    """Immutable identity of a published layer resource."""

    role: str
    generation: int
    source_key: tuple
    content_hash: str

    def __str__(self) -> str:
        role = self.role.value if isinstance(self.role, LayerRole) else self.role
        return f"{role}:gen={self.generation}:{self.content_hash[:8]}"


@dataclass
class LayerPlacement:
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0


@dataclass
class LayerState:
    """Complete publication state for one layer role."""

    role: str
    visible: Any = None
    pending: Any = None
    placement: "LayerPlacement" = field(default_factory=lambda: LayerPlacement())
    pending_placement: Optional["LayerPlacement"] = None
    resources: dict = field(default_factory=dict)
    visible_resources: dict = field(default_factory=dict)
    native_ready: bool = False
    native_frame_observed: bool = False
    visible_budget_bytes: int = 0
    pending_budget_bytes: int = 0
    completion_token: Any = None

    @property
    def budget_bytes(self) -> int:
        """Compatibility view of the currently staged or visible reservation."""
        return self.pending_budget_bytes or self.visible_budget_bytes

    @budget_bytes.setter
    def budget_bytes(self, value: int) -> None:
        # Preserve legacy direct state setup; manager accounting uses ownership fields.
        if self.pending is not None:
            self.pending_budget_bytes = value
        else:
            self.visible_budget_bytes = value

    def is_stale(self, current_generation: int) -> bool:
        if self.visible is None:
            return False
        return self.visible.generation != current_generation

    def has_pending(self) -> bool:
        return self.pending is not None

    def clear_pending(self) -> None:
        self.pending = None
        self.pending_placement = None

    def commit_pending(self) -> None:
        if self.pending is not None:
            self.visible = self.pending
            self.pending = None
            if self.pending_placement is not None:
                self.placement = self.pending_placement
            self.pending_placement = None
            self.completion_token = None
            self.visible_resources = self.resources
            self.resources = {}
            self.visible_budget_bytes = self.pending_budget_bytes
            self.pending_budget_bytes = 0


class PublicationBudget:
    """Native memory budget enforcement with per-layer and global caps."""

    def __init__(self, global_cap_bytes: int = 128 * 1024 * 1024):
        self.global_cap = global_cap_bytes
        self.layer_caps = {
            "barbs": 32 * 1024 * 1024,
            "fill": 32 * 1024 * 1024,
            "barbs_back": 16 * 1024 * 1024,
            "fill_back": 16 * 1024 * 1024,
            "chart": 64 * 1024 * 1024,
            "streamlines": 8 * 1024 * 1024,
        }
        self.current_usage = {
            "barbs": 0,
            "fill": 0,
            "barbs_back": 0,
            "fill_back": 0,
            "chart": 0,
            "streamlines": 0,
        }

    def can_allocate(self, role: str, bytes_needed: int) -> bool:
        return (
            self.current_usage[role] + bytes_needed
            <= self.layer_caps.get(role, self.global_cap)
            and sum(self.current_usage.values()) + bytes_needed <= self.global_cap
        )

    def allocate(self, role: str, bytes_allocated: int) -> bool:
        if not self.can_allocate(role, bytes_allocated):
            return False
        self.current_usage[role] += bytes_allocated
        return True

    def release(self, role: str, bytes_released: int) -> None:
        self.current_usage[role] = max(0, self.current_usage[role] - bytes_released)

    def total_used(self) -> int:
        return sum(self.current_usage.values())

    def utilization(self) -> float:
        return self.total_used() / self.global_cap if self.global_cap > 0 else 0.0


class PublicationManager:
    """Manages coherent layer publication with atomic rebasing."""

    def __init__(self, budget=None):
        self.layers = {
            "barbs": LayerState(role="barbs"),
            "fill": LayerState(role="fill"),
            "barbs_back": LayerState(role="barbs_back"),
            "fill_back": LayerState(role="fill_back"),
            "chart": LayerState(role="chart"),
            "streamlines": LayerState(role="streamlines"),
        }
        self.budget = budget or PublicationBudget()
        self.current_generation = 0
        self.reference_camera = None
        self._lock = threading.RLock()
        self._pending_completions = {
            "barbs": [],
            "fill": [],
            "barbs_back": [],
            "fill_back": [],
            "chart": [],
            "streamlines": [],
        }

    def stage_resources(
        self,
        role: str,
        identity,
        placement,
        resources,
        budget_bytes: int,
        completion_token=None,
    ) -> bool:
        """Stage resources for a layer; returns False if budget exceeded."""
        with self._lock:
            layer = self.layers[role]
            old_budget = layer.pending_budget_bytes if layer.pending is not None else 0
            delta = budget_bytes - old_budget
            if delta >= 0:
                if not self.budget.allocate(role, delta):
                    return False
            else:
                self.budget.release(role, -delta)
            layer.pending_budget_bytes = budget_bytes
            layer.pending = identity
            layer.completion_token = completion_token
            layer.pending_placement = placement
            layer.resources = resources
            if layer.visible is None:
                layer.native_ready = False
                layer.native_frame_observed = False
            return True

    def verify_and_adopt(
        self, role, verify_fn, expected_token=None
    ) -> tuple[bool, str]:
        """Verify staged resources and adopt as visible if ready."""
        with self._lock:
            layer = self.layers[role]
            if layer.pending is None:
                return False, "no pending resources"
            if expected_token is not None and layer.completion_token != expected_token:
                return False, "stale completion"
            if not verify_fn(layer.resources):
                self._dispose_pending(role, layer)
                return False, "verification failed"
            # Adopt
            self.budget.release(role, layer.visible_budget_bytes)
            layer.visible = layer.pending
            if layer.pending_placement is not None:
                layer.placement = layer.pending_placement
            layer.pending_placement = None
            layer.visible_resources = layer.resources
            layer.resources = {}
            layer.pending = None
            layer.visible_budget_bytes = layer.pending_budget_bytes
            layer.pending_budget_bytes = 0
            layer.completion_token = None
            layer.native_ready = True
            layer.native_frame_observed = True
            return True, "adopted"

    def commit_pending(self, role, expected_token=None) -> tuple[bool, str]:
        """Adopt staged resources under the manager lock without extra checks."""
        return self.verify_and_adopt(role, lambda _: True, expected_token)

    def reject_pending(self, role: str, expected_token=None) -> None:
        """Reject staged resources and release budget."""
        with self._lock:
            layer = self.layers[role]
            if layer.pending is not None and (
                expected_token is None or layer.completion_token == expected_token
            ):
                self._dispose_pending(role, layer)
                if layer.visible is None:
                    layer.native_ready = False
                    layer.native_frame_observed = False

    def withdraw_visible(self, role: str) -> None:
        """Retire a scene-withdrawn front even if its data generation matches.

        A reference change is not a field generation change; comparing those
        unrelated counters can otherwise retain an incompatible front.
        """
        with self._lock:
            layer = self.layers[role]
            self.budget.release(role, layer.visible_budget_bytes)
            layer.visible = None
            layer.visible_resources = {}
            layer.visible_budget_bytes = 0
            layer.native_ready = False
            layer.native_frame_observed = False

    def _dispose_pending(self, role, layer):
        self.budget.release(role, layer.pending_budget_bytes)
        layer.pending_budget_bytes = 0
        layer.pending = None
        layer.pending_placement = None
        layer.completion_token = None
        layer.resources = {}

    def set_generation(self, generation: int) -> None:
        self.current_generation = generation

    def set_reference_camera(self, camera) -> None:
        self.reference_camera = camera

    def rebase_all(self, new_camera, new_generation: int) -> dict:
        """Atomically rebase all layers to new camera/generation."""
        with self._lock:
            return self._rebase_all_locked(new_camera, new_generation)

    def _rebase_all_locked(self, new_camera, new_generation: int) -> dict:
        report = {
            "rebased": [],
            "rejected": [],
            "retained": [],
            "new_generation": new_generation,
            "new_camera": str(new_camera),
        }
        self.current_generation = new_generation
        self.reference_camera = new_camera

        for role, layer in self.layers.items():
            if layer.visible is not None:
                if layer.visible.generation == new_generation:
                    report["retained"].append(role)
                else:
                    # Stale visible - reject
                    self.budget.release(role, layer.visible_budget_bytes)
                    layer.visible = None
                    layer.visible_resources = {}
                    layer.visible_budget_bytes = 0
                    layer.native_ready = False
                    layer.native_frame_observed = False
                    report["rejected"].append(role)
            if layer.pending is not None:
                # Reject all pending work from old reference
                self._dispose_pending(role, layer)
                if layer.visible is None:
                    layer.native_ready = False
                    layer.native_frame_observed = False
                report["rejected"].append(f"{role}(pending)")

        return report

    def get_visible_state(self) -> dict:
        return {
            role: {
                "visible": str(layer.visible) if layer.visible else None,
                "pending": str(layer.pending) if layer.pending else None,
                "placement": layer.placement.__dict__,
                "native_ready": layer.native_ready,
                "budget_bytes": layer.budget_bytes,
            }
            for role, layer in self.layers.items()
        }

    def await_completion(self, role: str, timeout: float = 5.0) -> bool:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            layer = self.layers[role]
            if layer.pending is None:
                return layer.visible is not None
            time.sleep(0.01)
        return False
