"""Tests for coherent layer publication and atomic rebasing (#119)."""

from sailworks.qt.viewportpublication import (
    LayerRole,
    LayerIdentity,
    LayerPlacement,
    PublicationBudget,
    PublicationManager,
)


def test_layer_identity():
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    assert ident.role == LayerRole.BARBS
    assert ident.generation == 1
    assert ident.source_key == ("f1", "s1", "c1")
    assert str(ident).startswith("barbs:gen=1:abc123")


def test_layer_placement():
    placement = LayerPlacement(x=10.5, y=20.0, width=100, height=100, scale=2.0)
    assert placement.x == 10.5
    assert placement.scale == 2.0


def test_pending_placement_does_not_replace_visible_until_adoption():
    manager = PublicationManager()
    visible = LayerPlacement(x=1)
    pending = LayerPlacement(x=2)
    assert manager.stage_resources("chart", "first", visible, {}, 1, "first")
    assert manager.verify_and_adopt("chart", lambda _: True, "first")[0]
    assert manager.stage_resources("chart", "second", pending, {}, 1, "second")
    layer = manager.layers["chart"]
    assert layer.placement is visible
    assert layer.pending_placement is pending
    assert not manager.verify_and_adopt("chart", lambda _: True, "first")[0]
    assert layer.placement is visible
    manager.reject_pending("chart", expected_token="second")
    assert layer.placement is visible
    assert layer.pending_placement is None
    assert manager.stage_resources("chart", "third", pending, {}, 1, "third")
    assert manager.verify_and_adopt("chart", lambda _: True, "third")[0]
    assert layer.placement is pending


def test_layer_state_transitions():
    from sailworks.qt.viewportpublication import LayerState

    state = LayerState(role=LayerRole.BARBS)
    assert state.visible is None
    assert state.pending is None

    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    state.pending = ident
    assert state.has_pending()

    state.commit_pending()
    assert state.visible == ident
    assert state.pending is None
    assert not state.has_pending()

    state.visible = LayerIdentity(LayerRole.BARBS, 2, ("f2", "s2", "c2"), "def456")
    assert state.is_stale(1)
    assert not state.is_stale(2)


def test_budget_allocation():
    budget = PublicationBudget(global_cap_bytes=100)
    assert budget.can_allocate(LayerRole.BARBS, 30)
    assert budget.allocate(LayerRole.BARBS, 30)
    assert budget.current_usage[LayerRole.BARBS] == 30
    assert budget.can_allocate(LayerRole.BARBS, 40)
    assert not budget.allocate(LayerRole.BARBS, 80)  # would exceed layer cap
    budget.release(LayerRole.BARBS, 20)
    assert budget.current_usage[LayerRole.BARBS] == 10


def test_budget_global_cap():
    budget = PublicationBudget(global_cap_bytes=100)
    assert budget.allocate(LayerRole.BARBS, 60)
    assert budget.allocate(LayerRole.FILL, 30)
    assert not budget.allocate(LayerRole.CHART, 20)  # would exceed global cap


def test_manager_stage_and_adopt():
    from sailworks.qt.viewportpublication import (
        PublicationManager,
        LayerIdentity,
        LayerPlacement,
    )

    mgr = PublicationManager()
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")

    assert mgr.stage_resources(
        LayerRole.BARBS,
        ident,
        LayerPlacement(x=10, y=10, width=100, height=100),
        {"image": b"data"},
        1000,
    )
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.pending is not None
    assert layer.pending.generation == 1

    ok, msg = mgr.verify_and_adopt(LayerRole.BARBS, verify_fn=lambda r: "image" in r)
    assert ok
    assert msg == "adopted"
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.visible is not None
    assert layer.pending is None


def test_manager_reject_pending():
    from sailworks.qt.viewportpublication import (
        PublicationManager,
        LayerIdentity,
        LayerPlacement,
    )

    mgr = PublicationManager()
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    placement = LayerPlacement()
    mgr.stage_resources(LayerRole.BARBS, ident, placement, {"image": b"data"}, 1000)
    mgr.reject_pending(LayerRole.BARBS)
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.pending is None
    assert layer.resources == {}


def test_reject_pending_only_rejects_matching_token():
    mgr = PublicationManager()
    token = object()
    assert mgr.stage_resources(
        "barbs", "new", LayerPlacement(), {"image": b"data"}, 100, token
    )
    mgr.reject_pending("barbs", expected_token=object())
    assert mgr.layers["barbs"].pending == "new"
    assert mgr.budget.current_usage["barbs"] == 100
    mgr.reject_pending("barbs", expected_token=token)
    assert mgr.layers["barbs"].pending is None
    assert mgr.budget.current_usage["barbs"] == 0


def test_stale_completion_token_after_rebase_and_replacement():
    mgr = PublicationManager()
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f", "s", "c"), "same")
    mgr.stage_resources(
        LayerRole.BARBS,
        ident,
        LayerPlacement(),
        {"old": b"x"},
        100,
        completion_token="old",
    )
    mgr.rebase_all(object(), 2)
    mgr.stage_resources(
        LayerRole.BARBS,
        ident,
        LayerPlacement(),
        {"new": b"y"},
        200,
        completion_token="new",
    )
    layer = mgr.layers[LayerRole.BARBS]
    before = (
        layer.pending,
        dict(layer.resources),
        mgr.budget.current_usage[LayerRole.BARBS],
        layer.budget_bytes,
    )
    called = []
    assert mgr.verify_and_adopt(
        LayerRole.BARBS, lambda r: called.append(r) or True, expected_token="old"
    ) == (False, "stale completion")
    assert not called
    assert (
        layer.pending,
        layer.resources,
        mgr.budget.current_usage[LayerRole.BARBS],
        layer.budget_bytes,
    ) == before
    assert mgr.verify_and_adopt(
        LayerRole.BARBS, lambda r: True, expected_token="new"
    ) == (True, "adopted")
    assert layer.visible == ident


def test_stale_completion_cannot_replace_visible_state():
    mgr = PublicationManager()
    old = LayerIdentity(LayerRole.BARBS, 1, ("f", "s", "c"), "old")
    new = LayerIdentity(LayerRole.BARBS, 2, ("f", "s", "c"), "new")
    mgr.stage_resources(LayerRole.BARBS, old, LayerPlacement(), {"old": b"x"}, 100)
    mgr.verify_and_adopt(LayerRole.BARBS, lambda r: True)
    mgr.stage_resources(
        LayerRole.BARBS,
        new,
        LayerPlacement(),
        {"new": b"y"},
        200,
        completion_token="current",
    )
    layer = mgr.layers[LayerRole.BARBS]
    visible, usage = layer.visible, mgr.budget.current_usage[LayerRole.BARBS]
    assert mgr.verify_and_adopt(
        LayerRole.BARBS, lambda r: True, expected_token="obsolete"
    ) == (False, "stale completion")
    assert layer.visible == visible
    assert mgr.budget.current_usage[LayerRole.BARBS] == usage
    assert layer.pending == new


def test_rebase_all():
    from sailworks.qt.viewportpublication import (
        PublicationManager,
        LayerIdentity,
        LayerPlacement,
    )

    mgr = PublicationManager()
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    placement = LayerPlacement()
    mgr.stage_resources(LayerRole.BARBS, ident, placement, {"img": b"x"}, 1000)
    mgr.verify_and_adopt(LayerRole.BARBS, lambda r: True)

    class FakeCamera:
        pass

    cam = FakeCamera()
    report = mgr.rebase_all(cam, 2)
    assert "rebased" in report
    assert "rejected" in report
    assert "retained" in report
    assert report["new_generation"] == 2
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.visible is None


def test_budget_per_layer_caps():
    from sailworks.qt.viewportpublication import PublicationBudget

    budget = PublicationBudget(global_cap_bytes=200)
    budget.layer_caps[LayerRole.BARBS] = 50
    assert budget.allocate(LayerRole.BARBS, 30)
    assert not budget.allocate(LayerRole.BARBS, 30)  # would exceed 50


def test_concurrent_access():
    import threading

    mgr = PublicationManager()
    errors = []

    def writer(role, ident):
        try:
            mgr.stage_resources(role, ident, LayerPlacement(), {}, 100)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(
            target=writer,
            args=(
                LayerRole.BARBS,
                LayerIdentity(LayerRole.BARBS, i, ("f", "s", "c"), "h"),
            ),
        )
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.pending is not None


def test_budget_ownership_replacement_rejection_and_adoption():
    budget = PublicationBudget(global_cap_bytes=200)
    budget.layer_caps[LayerRole.BARBS] = 200
    mgr = PublicationManager(budget)
    ident = lambda n: LayerIdentity(LayerRole.BARBS, n, (), str(n))
    stage = lambda i, size, token=None: mgr.stage_resources(
        LayerRole.BARBS,
        i,
        LayerPlacement(),
        {"n": i.content_hash},
        size,
        completion_token=token,
    )

    assert stage(ident(1), 50)
    assert stage(ident(2), 60)
    layer = mgr.layers[LayerRole.BARBS]
    assert budget.current_usage[LayerRole.BARBS] == 60
    assert layer.pending_budget_bytes == 60
    assert not stage(ident(3), 250)
    assert layer.pending == ident(2)
    assert budget.current_usage[LayerRole.BARBS] == 60
    assert mgr.verify_and_adopt(LayerRole.BARBS, lambda _: True) == (True, "adopted")
    assert budget.current_usage[LayerRole.BARBS] == 60

    assert stage(ident(3), 40)
    assert budget.current_usage[LayerRole.BARBS] == 100
    assert mgr.verify_and_adopt(LayerRole.BARBS, lambda _: True) == (True, "adopted")
    assert budget.current_usage[LayerRole.BARBS] == 40


def test_pending_replacement_uses_net_budget_and_preserves_failed_state():
    budget = PublicationBudget(global_cap_bytes=100)
    budget.layer_caps["barbs"] = 80
    mgr = PublicationManager(budget)
    assert mgr.stage_resources("fill", "other", LayerPlacement(), {}, 20)
    assert mgr.stage_resources("barbs", "visible", LayerPlacement(), {"v": 1}, 30)
    assert mgr.commit_pending("barbs") == (True, "adopted")
    assert mgr.stage_resources("barbs", "old", LayerPlacement(), {"p": 1}, 50, "old")
    layer = mgr.layers["barbs"]

    assert mgr.stage_resources("barbs", "same", LayerPlacement(), {"p": 2}, 50, "same")
    assert budget.current_usage["barbs"] == 80
    assert budget.total_used() == 100
    assert mgr.commit_pending("barbs", "old") == (False, "stale completion")
    before = (layer.__dict__.copy(), budget.current_usage.copy())
    assert not mgr.stage_resources(
        "barbs", "too-large", LayerPlacement(x=5), {"p": 3}, 51, "failed"
    )
    assert (layer.__dict__, budget.current_usage) == before
    assert layer.visible == "visible"
    assert layer.visible_resources == {"v": 1}

    assert mgr.stage_resources(
        "barbs", "smaller", LayerPlacement(), {"p": 4}, 35, "small"
    )
    assert layer.pending_budget_bytes == 35
    assert layer.visible_budget_bytes == 30
    assert budget.current_usage["barbs"] == 65
    assert budget.total_used() == 85
    assert mgr.commit_pending("barbs", "same") == (False, "stale completion")


def test_manager_commit_pending_releases_replaced_visible_budget_and_noops():
    budget = PublicationBudget(global_cap_bytes=200)
    budget.layer_caps[LayerRole.BARBS] = 200
    mgr = PublicationManager(budget)
    role = LayerRole.BARBS
    ident = lambda n: LayerIdentity(role, n, (), str(n))

    assert mgr.commit_pending(role) == (False, "no pending resources")
    assert mgr.stage_resources(role, ident(1), LayerPlacement(), {}, 50)
    assert mgr.commit_pending(role) == (True, "adopted")
    assert budget.current_usage[role] == 50

    assert mgr.stage_resources(role, ident(2), LayerPlacement(), {}, 60)
    assert budget.current_usage[role] == 110
    assert mgr.commit_pending(role) == (True, "adopted")
    assert mgr.layers[role].visible == ident(2)
    assert mgr.layers[role].visible_budget_bytes == 60
    assert budget.current_usage[role] == 60
    assert mgr.commit_pending(role) == (False, "no pending resources")
    assert budget.current_usage[role] == 60


def test_rebase_releases_stale_visible_and_pending_independently():
    mgr = PublicationManager()
    role = LayerRole.BARBS
    old = LayerIdentity(role, 1, (), "old")
    mgr.stage_resources(role, old, LayerPlacement(), {}, 100)
    mgr.verify_and_adopt(role, lambda _: True)
    pending = LayerIdentity(role, 2, (), "pending")
    mgr.stage_resources(role, pending, LayerPlacement(), {}, 50, "token")
    mgr.rebase_all(object(), 3)
    layer = mgr.layers[role]
    assert layer.visible is None and layer.pending is None
    assert layer.visible_budget_bytes == layer.pending_budget_bytes == 0
    assert layer.completion_token is None
    assert mgr.budget.current_usage[role] == 0

    retained = LayerIdentity(role, 4, (), "retained")
    mgr.stage_resources(role, retained, LayerPlacement(), {}, 70)
    mgr.verify_and_adopt(role, lambda _: True)
    mgr.rebase_all(object(), 4)
    assert layer.visible == retained
    assert layer.visible_budget_bytes == 70
    assert mgr.budget.current_usage[role] == 70


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
