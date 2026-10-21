"""Native weather policy: headless byte reservations and explicit resource leases."""

from dataclasses import dataclass

import pytest

from gweatherrouting.qt.gpuweathertextures import (
    NativeFieldKey,
    NativeFieldStore,
    NativeFieldTooLarge,
    NativeWeatherBudget,
)


@dataclass
class Image:
    size: int = 8

    def sizeInBytes(self):
        return self.size


def store(budget, fail_at=None):
    allocated, destroyed = [], []

    def allocate(_image):
        if fail_at is not None and len(allocated) == fail_at:
            raise RuntimeError("allocation control")
        texture = object()
        allocated.append(texture)
        return texture

    result = NativeFieldStore(budget, allocate, destroyed.append)
    return result, allocated, destroyed


def key(name, source=0, scene=0, representation="nodes"):
    return NativeFieldKey(source, scene, name, representation)


def test_policy_import_does_not_import_qt():
    import inspect
    import gweatherrouting.qt.gpuweathertextures as module

    # Qt imports are inside the explicitly constructed adapter, not policy.
    prefix = inspect.getsource(module).split("def native_texture_context(")[0]
    assert "from PySide6" not in prefix


def test_two_contexts_share_one_limit_and_never_share_handles():
    budget = NativeWeatherBudget(64)
    left, a, _ = store(budget)
    right, b, _ = store(budget)
    images = [Image()] * 4
    left.bind(key("same-field"), images)
    right.bind(key("same-field"), images)
    assert not set(a) & set(b)
    assert budget.statistics()["current_bytes"] == 64
    assert budget.statistics()["contexts"] == 2
    left.clear()
    assert budget.statistics()["current_bytes"] == 32
    assert right.current == key("same-field")
    right.clear()
    assert budget.statistics()["current_bytes"] == 0


def test_warm_a_b_a_reuses_all_four_textures_and_balances_gui_leases():
    budget = NativeWeatherBudget(64)
    native, allocations, _destroyed = store(budget)
    images = [Image()] * 4
    first = native.bind(key("A"), images)
    native.bind(key("B"), images)
    lease = budget.acquire(native.owner, key("A"))
    assert native.bind(key("A"), images, lease) is first
    assert len(allocations) == 8
    duplicate = budget.acquire(native.owner, key("A"))
    native.bind(key("A"), images, duplicate)
    assert native.entries[key("A")].ticket.pins == 1
    native.clear()
    assert budget.statistics()["current_bytes"] == 0


def test_eviction_never_lends_unconfirmed_bytes_and_preserves_sibling_pin():
    budget = NativeWeatherBudget(96)
    left, _a, disposed = store(budget)
    right, _b, _ = store(budget)
    images = [Image()] * 4
    left.bind(key("A"), images)
    right.bind(key("visible"), images)
    left.bind(key("B"), images)
    left.bind(key("A"), images)
    assert left.bind(key("C"), images) is None
    statistics = budget.statistics()
    assert statistics["current_bytes"] == 96
    assert statistics["retiring_bytes"] == 32
    assert statistics["reserved_bytes"] == 0
    assert not disposed  # policy requested, but native owner has not retired
    left.after_sync()
    assert len(disposed) == 4
    assert key("B") not in left.entries
    assert right.current == key("visible")
    assert left.bind(key("C"), images) is not None
    assert budget.statistics()["peak_bytes"] == 96
    # Returning to evicted B is a native reload, not a fake cache hit.
    assert left.bind(key("B"), images) is None
    left.after_sync()
    assert left.bind(key("B"), images) is not None
    assert left.created == 16
    left.clear()
    right.clear()
    assert budget.statistics()["current_bytes"] == 0


@pytest.mark.parametrize("fail_at", [0, 1, 2, 3])
def test_partial_allocation_rolls_back_complete_group_reservation(fail_at):
    budget = NativeWeatherBudget(32)
    native, allocations, destroyed = store(budget, fail_at)
    with pytest.raises(RuntimeError, match="allocation control"):
        native.bind(key("A"), [Image()] * 4)
    assert destroyed == allocations
    assert not native.entries
    assert budget.statistics()["current_bytes"] == 0


def test_single_oversized_field_fails_without_allocating_or_raising_cap():
    budget = NativeWeatherBudget(31)
    native, allocations, _ = store(budget)
    with pytest.raises(NativeFieldTooLarge):
        native.bind(key("A"), [Image()] * 4)
    assert not allocations
    assert budget.byte_limit == 31
    assert budget.statistics()["current_bytes"] == 0


def test_unregister_cannot_discard_charged_resources():
    budget = NativeWeatherBudget(32)
    native, _, _ = store(budget)
    native.bind(key("A"), [Image()] * 4)
    with pytest.raises(RuntimeError, match="charged resources"):
        budget.unregister(native.owner)
    native.clear()
    budget.unregister(native.owner)
    assert budget.statistics()["contexts"] == 0


def test_delayed_lease_cancellation_cannot_unpin_new_allocation_with_same_key():
    budget = NativeWeatherBudget(32)
    native, _, _ = store(budget)
    native.bind(key("A"), [Image()] * 4)
    old = budget.acquire(native.owner, key("A"))
    budget.unpin(old)
    native.clear()
    native.bind(key("A"), [Image()] * 4)
    budget.unpin(old)
    assert native.entries[key("A")].ticket.pins == 1
    with pytest.raises(ValueError, match="retired allocation"):
        native.bind(key("A"), [Image()] * 4, old)
    assert native.entries[key("A")].ticket.pins == 1
    native.clear()


def test_source_scene_and_representation_are_distinct_cache_identities():
    assert (
        len(
            {
                key("A"),
                key("A", source=1),
                key("A", scene=1),
                key("A", representation="grid"),
            }
        )
        == 4
    )


def test_cross_context_retirement_is_only_executed_by_owning_store():
    budget = NativeWeatherBudget(32)
    left, _allocations, destroyed = store(budget)
    right, _, _ = store(budget)
    left.bind(key("inactive"), [Image()] * 4)
    left.unbind()
    assert right.bind(key("demand"), [Image()] * 4) is None
    assert not destroyed
    right.after_sync()
    assert not destroyed
    left.after_sync()
    assert len(destroyed) == 4
    assert right.bind(key("demand"), [Image()] * 4) is not None
    right.clear()
