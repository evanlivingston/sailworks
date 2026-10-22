#!/usr/bin/env python3
"""Check that local Markdown ``#anchor`` links resolve to a heading.

The link checker in :mod:`check_doc_links` verifies that a target *file*
exists. This one verifies that the fragment after ``#`` matches a heading in
that file, using GitHub's slug rules. It is what catches a document moving
sections around without updating the links that pointed at them.

Dependency free: standard library only, no network access.

    python tools/check_doc_anchors.py
    python tools/check_doc_anchors.py docs/README.md
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
SKIP = ("http://", "https://", "mailto:")


def slug(text):
    """GitHub-style heading slug: strip punctuation, each space becomes a hyphen."""
    text = re.sub(r"`", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return text.replace(" ", "-")


def anchors(path):
    """Every heading slug in a Markdown file."""
    found = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = HEADING.match(line)
        if match:
            found.add(slug(match.group(2)))
    return found


def resolve(source, target):
    """The Markdown file a local link points at, or None if it is not one."""
    path = target.split("#", 1)[0]
    if not path:
        return source
    candidate = (source.parent / path).resolve()
    if candidate.is_dir():
        candidate = candidate / "README.md"
    if candidate.suffix == "":
        candidate = candidate.with_suffix(".md")
    return candidate if candidate.is_file() else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files or directories to check (default: the whole repository)",
    )
    args = parser.parse_args(argv)
    targets = args.paths or [ROOT]
    files = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(target.rglob("*.md")))
        elif target.is_file():
            files.append(target)
        else:
            print(f"check_doc_anchors: no such path: {target}", file=sys.stderr)
            return 2
    checked = 0
    missing = []
    for path in files:
        if ".git" in path.parts or ".venv" in path.parts:
            continue
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(SKIP):
                continue
            _, sep, fragment = target.partition("#")
            if not sep or not fragment:
                continue
            resolved = resolve(path, target)
            if resolved is None:
                continue
            checked += 1
            if fragment not in anchors(resolved):
                missing.append(f"{path.relative_to(ROOT)}: {target}")
    for entry in missing:
        print(f"missing anchor: {entry}", file=sys.stderr)
    print(f"anchors checked: {checked}; missing: {len(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
