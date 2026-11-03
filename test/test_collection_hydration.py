import copy
from unittest.mock import patch

import pytest

from sailworks.core.geo.collection import Collection, CollectionStorage
from sailworks.core.geo.track import Track
from sailworks.core.geo.trackcollection import TrackCollection
from sailworks.core import storage


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    return tmp_path


def test_roundtrip_hydrates_without_saving(data_dir):
    original = TrackCollection()
    original.append(Track("duplicate", [[1.25, -2.5]], visible=False, color="red"))
    original.append(Track("duplicate", [[3.5, 4.75]], race_track=True))
    original.set_active(original[0])
    expected = original.to_json()
    path = data_dir / "track.json"
    before = path.read_bytes()

    # Construction may call Storage.save while loading its own metadata, but
    # hydration must neither save the collection nor write the stored file.
    with patch.object(Collection, "save") as save:
        loaded = TrackCollection()
    save.assert_not_called()
    assert path.read_bytes() == before
    assert loaded.to_json() == expected
    assert all(element.collection is loaded for element in loaded)
    assert loaded.get_active() is loaded[0]

    payload = copy.deepcopy(expected)
    previous_revision = loaded.revision
    with (
        patch.object(Collection, "save") as save,
        patch.object(CollectionStorage, "save") as storage_save,
    ):
        loaded.load_json(payload)
    save.assert_not_called()
    storage_save.assert_not_called()
    assert payload == expected
    assert path.read_bytes() == before
    assert loaded.revision > previous_revision

    loaded.append(Track("new", [[-1, 9]]))
    reloaded = TrackCollection()
    assert reloaded.to_json() == loaded.to_json()


def test_missing_elements_and_unknown_active_do_not_save(data_dir):
    collection = TrackCollection()
    collection.append(Track("old", [[1, 2]]))
    before = (data_dir / "track.json").read_bytes()
    revision = collection.revision
    with (
        patch.object(Collection, "save") as save,
        patch.object(CollectionStorage, "save") as storage_save,
    ):
        collection.load_json({"activeElement": "unknown"})
    save.assert_not_called()
    storage_save.assert_not_called()
    assert not collection.elements
    assert collection.get_active() is None
    assert collection.revision > revision
    assert (data_dir / "track.json").read_bytes() == before


def test_unknown_and_missing_active_name(data_dir):
    collection = TrackCollection()
    payload = {"elements": [Track("a", [[1, 2]]).to_json()]}
    for active in ("unknown", None):
        collection.load_json({**payload, "activeElement": active})
        assert collection.get_active() is None
    collection.load_json({**payload, "activeElement": "a"})
    assert collection.get_active() is collection[0]
    collection.load_json(payload)
    assert collection.get_active() is None


def test_failed_hydration_preserves_previous_elements(data_dir):
    collection = TrackCollection()
    old = Track("old", [[1, 2]])
    collection.append(old)
    collection.set_active(old)
    revision = collection.revision
    elements = collection.elements
    before = (data_dir / "track.json").read_bytes()
    payload = {"elements": [Track("valid", [[3, 4]]).to_json(), {}]}
    with (
        patch.object(Collection, "save") as save,
        patch.object(CollectionStorage, "save") as storage_save,
    ):
        with pytest.raises(KeyError):
            collection.load_json(payload)
    save.assert_not_called()
    storage_save.assert_not_called()
    assert collection.elements is elements
    assert collection[0] is old
    assert collection.get_active() is old
    assert collection.revision == revision
    assert (data_dir / "track.json").read_bytes() == before
