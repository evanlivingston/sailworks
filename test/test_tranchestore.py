from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from gweatherrouting.core.forecasttranche import (
    AnalysisSelection,
    Artifact,
    ForecastTranche,
    ModelConfiguration,
    ModelEntry,
)
from gweatherrouting.core.tranchestore import TrancheStore


def test_register_publish_activate_and_restart(tmp_path):
    store = TrancheStore(tmp_path)
    source = tmp_path / "download.grb"
    source.write_bytes(b"fixture bytes")
    artifact = store.register_file(source)
    source.write_bytes(b"changed source")
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    models = (("gfs", "GFS"), ("hrrr", "HRRR"))
    configs = tuple(
        ModelConfiguration(model, ("wind",), (0, 1, 2, 3), "native", 0, (), 0)
        for _, model in models
    )
    entries = tuple(
        ModelEntry(
            name,
            model,
            run,
            "control",
            ("wind",),
            ("10m",),
            (0, 1, 2, 3),
            "0.25deg",
            (run,),
            (artifact,),
            run,
        )
        for name, model in models
    )
    tranche = ForecastTranche("refresh", run, configs, entries)
    store.publish(tranche)
    store.activate(AnalysisSelection("refresh", "hrrr"))
    reopened = TrancheStore(tmp_path)
    resolved = reopened.active()
    assert resolved.selection.entry_id == "hrrr" and len(resolved.tranche.entries) == 2
    assert (
        resolved.entry.model_id == "HRRR"
        and resolved.artifact_paths[0].read_bytes() == b"fixture bytes"
    )


def test_rejected_switch_and_published_manifest_are_preserved(tmp_path, monkeypatch):
    store = TrancheStore(tmp_path)
    source = tmp_path / "f"
    source.write_bytes(b"x")
    artifact = store.register_file(source)
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    entry = ModelEntry(
        "entry",
        "GFS",
        run,
        "control",
        ("wind",),
        ("10m",),
        (0, 1, 2, 3),
        "0.25deg",
        (run,),
        (artifact,),
        run,
    )
    config = ModelConfiguration("GFS", ("wind",), (0, 1, 2, 3), "native", 0, (), 0)
    tranche = ForecastTranche("one", run, (config,), (entry,))
    store.publish(tranche)
    store.activate(AnalysisSelection("one", "entry"))
    old = store.active_path.read_bytes()
    with pytest.raises(ValueError):
        store.activate(AnalysisSelection("one", "missing"))
    assert store.active_path.read_bytes() == old
    conflict = ModelEntry(
        "other",
        "GFS",
        run,
        "control",
        ("wind",),
        ("10m",),
        (0, 1, 2, 3),
        "0.25deg",
        (run,),
        (artifact,),
        run,
    )
    with pytest.raises(FileExistsError):
        store.publish(ForecastTranche("one", run, (config,), (conflict,)))
    replace = store._atomic_active
    monkeypatch.setattr(
        store,
        "_atomic_active",
        lambda *args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError):
        store.activate(AnalysisSelection("one", "entry"))
    assert store.active_path.read_bytes() == old
    monkeypatch.setattr(store, "_atomic_active", replace)


def test_missing_or_corrupt_artifacts_rejected(tmp_path):
    store = TrancheStore(tmp_path)
    source = tmp_path / "f"
    source.write_bytes(b"x")
    artifact = store.register_file(source)
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    entry = ModelEntry(
        "entry",
        "GFS",
        run,
        "control",
        ("wind",),
        ("10m",),
        (0, 1, 2, 3),
        "0.25deg",
        (run,),
        (artifact,),
        run,
    )
    config = ModelConfiguration("GFS", ("wind",), (0, 1, 2, 3), "native", 0, (), 0)
    store.publish(ForecastTranche("one", run, (config,), (entry,)))
    (tmp_path / artifact.path).chmod(0o644)
    (tmp_path / artifact.path).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        store.activate(AnalysisSelection("one", "entry"))
    (tmp_path / artifact.path).unlink()
    with pytest.raises(FileNotFoundError):
        store.resolve(AnalysisSelection("one", "entry"))


def test_duplicate_registration_and_concurrent_conflicting_publication(
    tmp_path, monkeypatch
):
    store = TrancheStore(tmp_path)
    source = tmp_path / "input"
    source.write_bytes(b"same content")
    first = store.register_file(source)
    assert store.register_file(source) == first
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    config = ModelConfiguration("GFS", ("wind",), (0, 1, 2, 3), "native", 0, (), 0)
    entries = [
        ModelEntry(
            name,
            "GFS",
            run,
            "control",
            ("wind",),
            ("10m",),
            (0, 1, 2, 3),
            "0.25deg",
            (run,),
            (first,),
            run,
        )
        for name in ("entry-a", "entry-b")
    ]
    tranches = [ForecastTranche("race", run, (config,), (entry,)) for entry in entries]
    barrier = Barrier(2)
    original = store._stage_json

    def stage(payload):
        path = original(payload)
        barrier.wait()
        return path

    monkeypatch.setattr(store, "_stage_json", stage)

    def publish(tranche):
        try:
            store.publish(tranche)
            return "published"
        except FileExistsError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, tranches))
    assert sorted(results) == ["conflict", "published"]


def test_pointer_precommit_failures_keep_binding_and_clean_staging(
    tmp_path, monkeypatch
):
    store = TrancheStore(tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"bytes")
    artifact = store.register_file(source)
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    config = ModelConfiguration("GFS", ("wind",), (0, 1, 2, 3), "native", 0, (), 0)
    entry = ModelEntry(
        "entry",
        "GFS",
        run,
        "control",
        ("wind",),
        ("10m",),
        (0, 1, 2, 3),
        "0.25deg",
        (run,),
        (artifact,),
        run,
    )
    tranche = ForecastTranche("batch", run, (config,), (entry,))
    store.publish(tranche)
    store.activate(AnalysisSelection("batch", "entry"))
    previous = store.active_path.read_bytes()
    selection = AnalysisSelection("batch", "entry")

    original_fsync = __import__("os").fsync
    calls = 0

    def fail_file_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("staged file fsync")
        return original_fsync(fd)

    monkeypatch.setattr("gweatherrouting.core.tranchestore.os.fsync", fail_file_fsync)
    with pytest.raises(OSError):
        store.activate(selection)
    assert store.active_path.read_bytes() == previous
    assert not list(tmp_path.glob(".active-*"))
    monkeypatch.setattr("gweatherrouting.core.tranchestore.os.fsync", original_fsync)

    monkeypatch.setattr(
        store, "_fsync_dir", lambda _: (_ for _ in ()).throw(OSError("dir fsync"))
    )
    with pytest.raises(OSError):
        store.activate(selection)
    assert store.active_path.read_bytes() == previous
    assert not list(tmp_path.glob(".active-*"))
    monkeypatch.undo()

    import gweatherrouting.core.tranchestore as module

    monkeypatch.setattr(
        module.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("replace"))
    )
    with pytest.raises(OSError):
        store.activate(selection)
    assert store.active_path.read_bytes() == previous
    assert not list(tmp_path.glob(".active-*"))


def test_symlinked_backing_files_are_rejected(tmp_path):
    store = TrancheStore(tmp_path)
    source = tmp_path / "source"
    source.write_bytes(b"bytes")
    artifact = store.register_file(source)
    run = datetime(2025, 1, 1, tzinfo=timezone.utc)
    config = ModelConfiguration("GFS", ("wind",), (0, 1, 2, 3), "native", 0, (), 0)
    entry = ModelEntry(
        "entry",
        "GFS",
        run,
        "control",
        ("wind",),
        ("10m",),
        (0, 1, 2, 3),
        "0.25deg",
        (run,),
        (artifact,),
        run,
    )
    store.publish(ForecastTranche("batch", run, (config,), (entry,)))
    store.activate(AnalysisSelection("batch", "entry"))
    blob = tmp_path / artifact.path
    blob.unlink()
    blob.symlink_to(source)
    with pytest.raises(ValueError):
        store.resolve(AnalysisSelection("batch", "entry"))
    blob.unlink()
    blob.write_bytes(b"bytes")
    blob.chmod(0o444)
    store.active_path.unlink()
    store.active_path.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError):
        store.active()
    store.active_path.unlink()
    manifest = store.manifests / "batch.json"
    manifest.unlink()
    manifest.symlink_to(source)
    with pytest.raises(ValueError):
        store.load("batch")
