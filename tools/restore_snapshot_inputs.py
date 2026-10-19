"""Restore missing snapshot inputs from exact local copies; never replace a baseline."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_inventory(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def safe_path(root, relative):
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != relative
        or not path.parts
    ):
        raise ValueError(f"Unsafe manifest path: {relative}")
    current = Path(root)
    if current.is_symlink():
        raise ValueError("Symlink input root is not allowed")
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Symlink input is not allowed: {relative}")
    return current


def matches(path, entry):
    return (
        path.is_file()
        and path.stat().st_size == entry["bytes"]
        and file_hash(path) == entry["sha256"]
    )


def inventory(root):
    result = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise ValueError("Snapshot contains symlinks")
        if path.is_file():
            result.append(
                dict(
                    path=path.relative_to(root).as_posix(),
                    bytes=path.stat().st_size,
                    sha256=file_hash(path),
                )
            )
    return result


def restore(snapshot, sources, apply=False, allow_partial=False):
    snapshot = Path(snapshot)
    manifest_path = snapshot / "manifest.json"
    manifest_digest = file_hash(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    entries = manifest["files"]
    if (
        manifest["snapshot_version"] != 1
        or digest_inventory(entries) != manifest["content_sha256"]
    ):
        raise ValueError("Invalid original snapshot manifest")
    data = snapshot / "data"
    paths = {entry["path"] for entry in entries}
    if len(paths) != len(entries):
        raise ValueError("Duplicate manifest paths")
    actual = inventory(data)
    if {entry["path"] for entry in actual} - paths:
        raise ValueError(
            "Unexpected snapshot files; refusing to delete or replace them"
        )
    plan, missing = [], []
    for entry in entries:
        destination = safe_path(data, entry["path"])
        if destination.exists():
            if not matches(destination, entry):
                raise ValueError(
                    f"Existing snapshot file differs; not overwriting: {entry['path']}"
                )
            continue
        found = None
        for root in sources:
            source = safe_path(Path(root), entry["path"])
            if matches(source, entry):
                found = source
                break
        if found is None:
            missing.append(entry["path"])
        else:
            plan.append((entry, found, destination))
    result = dict(
        expected_sha256=manifest["content_sha256"],
        existing_files=len(actual),
        recoverable_files=len(plan),
        unrecoverable=missing,
        unrecoverable_files=len(missing),
        applied=False,
    )
    if (missing and not allow_partial) or not apply:
        return result
    copied = []
    for entry, source, destination in plan:
        destination.parent.mkdir(parents=True, exist_ok=True)
        safe_path(data, entry["path"])
        # Exclusive creation preserves concurrent/user work. Copy bytes, never
        # share a live source inode. On failure only our newly created file is removed.
        created = False
        try:
            with destination.open("xb") as target:
                created = True
                with source.open("rb") as origin:
                    shutil.copyfileobj(origin, target, length=1024 * 1024)
            if not matches(destination, entry):
                raise ValueError(f"Restored file checksum mismatch: {entry['path']}")
        except Exception:
            if created:
                destination.unlink()
            raise
        copied.append(entry["path"])
    if file_hash(manifest_path) != manifest_digest:
        raise ValueError("Manifest changed during restoration")
    final = inventory(data)
    expected_present = [entry for entry in entries if entry["path"] not in set(missing)]
    if final != expected_present or (
        not missing and digest_inventory(final) != manifest["content_sha256"]
    ):
        raise ValueError("Restored inventory does not match original snapshot")
    result.update(
        applied=True,
        restored_files=copied,
        restored_sha256=digest_inventory(final),
        original_manifest_unchanged=True,
        complete=not missing,
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Copy exact available files, but still exit nonzero and retain original manifest if incomplete",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise ValueError("Refusing to overwrite restoration report")
    result = restore(args.snapshot, args.source, args.apply, args.allow_partial)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "restored_files"}
        )
    )
    if result["unrecoverable"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
