#!/usr/bin/env python3
"""Check that local Markdown links resolve.

Dependency free: standard library only, no network access.  Remote links
(``http``/``https``/``mailto``) and bare anchors are reported as counts, not
checked, because they cannot be verified offline.

    python tools/check_doc_links.py                 # docs/ and README.md
    python tools/check_doc_links.py docs/adr-001-grib-rendering-architecture.md
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SKIP = ("http://", "https://", "mailto:", "#")


def local_links(path):
    """Yield ``(target, text)`` for every local link in a Markdown file."""
    text = path.read_text(encoding="utf-8")
    for match in LINK.finditer(text):
        target = match.group(1)
        if target.startswith(SKIP):
            continue
        yield target, match.group(0)


def resolve(source, target):
    """Return the path a local link points at, or None if it is not a file."""
    clean = target.split("#", 1)[0]
    if not clean:
        return None
    candidate = (source.parent / clean).resolve()
    if candidate.exists():
        return candidate
    if candidate.suffix == "":
        for suffix in (".md", ".rst", ".py"):
            sibling = candidate.with_suffix(suffix)
            if sibling.exists():
                return sibling
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files or directories to check (default: docs/ and README.md)",
    )
    args = parser.parse_args(argv)
    targets = args.paths or [ROOT / "docs", ROOT / "README.md"]
    files = []
    for target in targets:
        if target.is_dir():
            files.extend(sorted(target.rglob("*.md")))
        elif target.is_file():
            files.append(target)
        else:
            print(f"check_doc_links: no such path: {target}", file=sys.stderr)
            return 2
    checked = 0
    missing = []
    for path in files:
        for target, text in local_links(path):
            checked += 1
            if resolve(path, target) is None:
                missing.append(f"{path.relative_to(ROOT)}: {text}")
    for entry in missing:
        print(f"missing link: {entry}", file=sys.stderr)
    print(f"local links checked: {checked}; missing: {len(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
