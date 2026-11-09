from sailworks.core.geo.element import Element
from sailworks.core.geo.track import Track
from sailworks.core.geo.trackcollection import _TrackCollection
from sailworks.core.scene import (
    KIND_LOG,
    KIND_POI,
    KIND_ROUTING,
    KIND_TRACK,
    SceneCollection,
    SceneEntry,
)


class FakeElement:
    def __init__(self, uid, name, visible=True):
        self.uid = uid
        self.name = name
        self.visible = visible


def entry(kind, element, detail=""):
    return SceneEntry(
        kind=kind,
        key=element.uid,
        name=element.name,
        visible=element.visible,
        detail=detail,
        element=element,
    )


def test_default_groups_are_created(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    names = [group.name for group in scene.groups.values()]
    assert names == ["Tracks", "Routings", "POIs", "Logs"]
    assert scene.default_group(KIND_TRACK) == "default-tracks"
    assert scene.default_group(KIND_ROUTING) == "default-routings"
    assert scene.default_group(KIND_POI) == "default-pois"
    assert scene.default_group(KIND_LOG) == "default-logs"


def test_reconcile_assigns_new_items_and_prunes_deleted(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    track = FakeElement("t1", "Morning")
    route = FakeElement("r1", "Route")
    scene.reconcile([entry(KIND_TRACK, track), entry(KIND_ROUTING, route)])

    assert scene.item(KIND_TRACK, "t1").group == "default-tracks"
    assert scene.item(KIND_ROUTING, "r1").group == "default-routings"

    scene.reconcile([entry(KIND_TRACK, track)])
    assert scene.item(KIND_ROUTING, "r1") is None


def test_group_visibility_hides_descendants_and_restores(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    track = FakeElement("t1", "Morning")
    scene.reconcile([entry(KIND_TRACK, track)])
    group = scene.create_group("Racing", "default-tracks", [(KIND_TRACK, "t1")])

    assert scene.effective_visible(KIND_TRACK, "t1")
    scene.set_group_visible(group.id, False)
    changed = scene.apply_visibility()

    assert changed == {KIND_TRACK}
    assert track.visible is False
    assert not scene.effective_visible(KIND_TRACK, "t1")

    scene.set_group_visible(group.id, True)
    scene.apply_visibility()
    assert track.visible is True


def test_item_visibility_roundtrip(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    track = FakeElement("t1", "Morning")
    scene.reconcile([entry(KIND_TRACK, track)])
    scene.set_item_visible(KIND_TRACK, "t1", False)
    scene.apply_visibility()
    assert track.visible is False

    scene.set_item_visible(KIND_TRACK, "t1", True)
    scene.apply_visibility()
    assert track.visible is True


def test_tree_and_assignments_survive_reload(tmp_path):
    path = tmp_path / "scene.json"
    scene = SceneCollection(path)
    track = FakeElement("t1", "Morning")
    scene.reconcile([entry(KIND_TRACK, track)])
    group = scene.create_group("Racing", None, [(KIND_TRACK, "t1")])
    scene.set_group_collapsed(group.id, True)
    scene.set_group_visible(group.id, False)
    scene.set_item_visible(KIND_TRACK, "t1", False)

    restored = SceneCollection(path)
    assert restored.group(group.id).name == "Racing"
    assert restored.group(group.id).collapsed is True
    assert restored.group(group.id).visible is False
    assert restored.item(KIND_TRACK, "t1").group == group.id
    assert restored.item(KIND_TRACK, "t1").visible is False


def test_nested_groups_are_found_after_reload(tmp_path):
    path = tmp_path / "scene.json"
    scene = SceneCollection(path)
    parent = scene.create_group("Parent", "default-tracks")
    child = scene.create_group("Child", parent.id)

    restored = SceneCollection(path)

    assert [group.id for group in restored.groups_in("default-tracks")] == [parent.id]
    assert [group.id for group in restored.groups_in(parent.id)] == [child.id]


def test_deleting_group_moves_contents_to_parent(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    track = FakeElement("t1", "Morning")
    scene.reconcile([entry(KIND_TRACK, track)])
    parent = scene.create_group("Parent")
    child = scene.create_group("Child", parent.id, [(KIND_TRACK, "t1")])

    assert scene.delete_group(child.id)
    assert scene.item(KIND_TRACK, "t1").group == parent.id
    assert scene.group(child.id) is None


def test_group_cannot_move_into_its_descendant(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    parent = scene.create_group("Parent")
    child = scene.create_group("Child", parent.id)

    assert not scene.move_group(parent.id, child.id)
    assert scene.group(parent.id).parent is None


def test_builtin_groups_cannot_be_deleted(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    assert not scene.delete_group("default-tracks")


def test_logs_are_not_visibility_editable(tmp_path):
    scene = SceneCollection(tmp_path / "scene.json")
    entry_ = SceneEntry(
        kind=KIND_LOG,
        key="log-1",
        name="Saturday.log",
        visible=True,
        visible_editable=False,
    )
    scene.reconcile([entry_])
    item = scene.item(KIND_LOG, "log-1")
    assert item.group == "default-logs"
    assert item.visible is True
    scene.apply_visibility()
    assert item.visible is True


def test_element_uid_roundtrips_through_json():
    element = Element("sample")
    saved = element.to_json()
    assert saved["uid"] == element.uid
    assert Element.from_json(saved).uid == element.uid


def test_collection_restores_element_uid():
    track = Track("sample", [[1.0, 2.0]])
    collection = _TrackCollection.__new__(_TrackCollection)
    collection.of = Track
    collection.elements = []
    collection.load_json({"elements": [track.to_json()]})
    assert collection[0].uid == track.uid
