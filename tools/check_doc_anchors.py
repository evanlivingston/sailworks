#!/usr/bin/env python3
"""Check that local Markdown ``#anchor`` links resolve to a heading.

``check_doc_links.py`` verifies that a link's target *file* exists. This one
verifies that the fragment after ``#`` matches a heading in that file, using
GitHub's heading-slug rules. It is what catches a document moving a section
without updating the links that pointed at it.

Both local file fragments (``other.md#section``) and same-file fragments
(``#section``) are checked. Remote links are counted, not checked.

Dependency free: standard library only, no network access.

    python tools/check_doc_anchors.py                 # every Markdown file
    python tools/check_doc_anchors.py docs/README.md
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*(#+)?\s*$")
SETEXT = re.compile(r"^=+|-+$")
FENCE = re.compile(r"^\s*(```+|~~~+)(.*)$")
SKIP = ("http://", "https://", "mailto:")


def slug(text):
    """GitHub's heading slug: strip punctuation, each space becomes a hyphen."""
    text = re.sub(r"`", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return text.replace(" ", "-")


def headings(path):
    """Every heading slug in a file, in document order, de-duplicated the way
    GitHub does (the second ``Heading`` becomes ``heading-1``)."""
    found = []
    seen = {}
    fence = None
    previous = None
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = FENCE.match(line)
        if marker:
            char = marker.group(1)[0]
            if fence is None:
                fence = char
            elif char == fence:
                fence = None
            previous = None
            continue
        if fence:
            continue
        match = ATX.match(line)
        if match and (match.group(2) or match.group(3)):
            base = slug(match.group(2))
        elif SETEXT.match(line.strip()) and previous and "|" not in previous:
            # A setext heading: the previous line of text, underlined. A table
            # delimiter row is not a setext heading.
            base = slug(previous)
        else:
            previous = line
            continue
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.append(base if count == 0 else f"{base}-{count}")
        previous = None
    return set(found)


def resolve(source, target):
    """Return the Markdown file a local link points at, or None."""
    clean = target.split("#", 1)[0]
    if not clean:
        return source
    candidate = (source.parent / clean).resolve()
    if candidate.is_dir():
        candidate = candidate / "README.md"
    if candidate.exists():
        return candidate
    if candidate.suffix == "":
        sibling = candidate.with_suffix(".md")
        if sibling.exists():
            return sibling
    return None


def display(path):
    try:
        return path.resolve().relative_to(ROOT)
    except ValueError:
        return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files or directories to check (default: the whole repository)",
    )
    args = parser.parse_args(argv)
    files = []
    for target in args.paths or [ROOT]:
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
        if any(part in (".git", ".venv", "node_modules") for part in path.parts):
            continue
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            if target.startswith(SKIP):
                continue
            _, _, fragment = target.partition("#")
            if not fragment:
                continue
            resolved = resolve(path, target)
            if resolved is None:
                continue
            checked += 1
            if fragment not in headings(resolved):
                missing.append(f"{display(path)}: {target}")
    for entry in missing:
        print(f"missing anchor: {entry}", file=sys.stderr)
    print(f"anchors checked: {checked}; missing: {len(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
