#!/usr/bin/env python3
"""Print a compact structural outline of Python source files.

Use this before reading a large file in full: it lists the definitions with
their line ranges so you can read only the range you need.

Usage:
    python tools/outline.py PATH [PATH ...]
    python tools/outline.py sailworks/qt/window.py --depth 1
    python tools/outline.py test/ --max-files 20

PATH may be a .py file or a directory (searched recursively for .py files).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def _signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = [a.arg for a in node.args.posonlyargs + node.args.args]
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"
    return f"class {node.name}"


def _ranges(node: ast.AST) -> tuple[int, int]:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", start) or start
    return start, end


def _entries(tree: ast.AST, depth: int, max_items: int) -> list[str]:
    lines: list[str] = []
    remaining = max_items

    def walk(body: list[ast.stmt], level: int) -> None:
        nonlocal remaining
        for node in body:
            if remaining <= 0:
                return
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            start, end = _ranges(node)
            indent = "  " * level
            lines.append(f"{indent}{start:>5}-{end:<5} {_signature(node)}")
            remaining -= 1
            if isinstance(node, ast.ClassDef) and level < depth:
                walk(node.body, level + 1)

    walk(tree.body, 0)
    return lines


def outline_file(path: Path, depth: int, max_items: int) -> str:
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"{path}: cannot read ({exc})"
    total = source.count("\n") + 1
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"{path} ({total} lines): cannot parse ({exc.msg} line {exc.lineno})"

    entries = _entries(tree, depth, max_items)
    header = f"{path} ({total} lines, {len(entries)} definitions)"
    if not entries:
        return header + "\n  (no top-level definitions)"
    return header + "\n" + "\n".join(entries)


def collect(paths: list[Path], max_files: int) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*.py") if p.is_file()))
        elif path.suffix == ".py":
            found.append(path)
        else:
            print(f"{path}: not a .py file", file=sys.stderr)
    unique = list(dict.fromkeys(found))
    if len(unique) > max_files:
        print(
            f"showing {max_files} of {len(unique)} files; narrow the path",
            file=sys.stderr,
        )
        unique = unique[:max_files]
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="class member depth to expand (default 1, 0 = top level only)",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=200,
        help="maximum definitions printed per file (default 200)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=50,
        help="maximum number of files (default 50)",
    )
    args = parser.parse_args(argv)

    files = collect(args.paths, args.max_files)
    if not files:
        print("no Python files found", file=sys.stderr)
        return 1
    print("\n\n".join(outline_file(p, args.depth, args.max_items) for p in files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
