#!/usr/bin/env python3
"""Flag-day migration script: remove all Twisted imports from Synapse.

This script performs mechanical transformations across the entire codebase
to remove Twisted dependencies. It should be run AFTER the core modules
(context.py, async_helpers.py, clock.py, database.py) have been manually
rewritten to use asyncio-native implementations.

Usage:
    python scripts/remove_twisted.py [--dry-run] [--verbose]
"""

import argparse
import re
import sys
from pathlib import Path


class Stats:
    def __init__(self) -> None:
        self.files_modified = 0
        self.files_skipped = 0
        self.total_replacements = 0


def transform_file(filepath: Path, dry_run: bool, verbose: bool, stats: Stats) -> None:
    """Apply all mechanical transformations to a single file."""
    try:
        content = filepath.read_text()
    except (UnicodeDecodeError, PermissionError):
        return

    original = content
    changes: list[str] = []

    # Skip files we've already rewritten manually
    skip_files = {
        "synapse/logging/context.py",
        "synapse/util/async_helpers.py",
        "synapse/util/clock.py",
        "synapse/storage/database.py",
        "synapse/storage/native_database.py",
        "synapse/http/native_client.py",
        "synapse/http/native_server.py",
        "synapse/replication/tcp/native_protocol.py",
        "synapse/util/caches/future_cache.py",
        "tests/async_helpers.py",
        "tests/util/test_native_async.py",
        "scripts/remove_twisted.py",
        "scripts/migrate_twisted_imports.py",
    }
    rel = str(filepath.relative_to(Path.cwd()))
    if rel in skip_files:
        return

    # =================================================================
    # 1. CancelledError: Twisted → asyncio
    # =================================================================

    # Direct import
    if re.search(r"from twisted\.internet\.defer import CancelledError\s*\n", content):
        content = re.sub(
            r"from twisted\.internet\.defer import CancelledError\s*\n",
            "from asyncio import CancelledError\n",
            content,
        )
        changes.append("CancelledError import")

    # CancelledError in multi-import lines
    pattern = r"from twisted\.internet\.defer import (.+)"
    for m in re.finditer(pattern, content):
        imports = m.group(1)
        if "CancelledError" in imports:
            parts = [p.strip() for p in imports.split(",")]
            parts = [p for p in parts if p and p != "CancelledError"]
            if parts:
                new_line = f"from twisted.internet.defer import {', '.join(parts)}"
                content = content.replace(m.group(0), new_line)
                # Add asyncio CancelledError import if not present
                if "from asyncio import CancelledError" not in content:
                    content = content.replace(
                        new_line,
                        f"from asyncio import CancelledError\n{new_line}",
                    )
            else:
                content = content.replace(
                    m.group(0), "from asyncio import CancelledError"
                )
            changes.append("CancelledError extracted")

    # defer.CancelledError → CancelledError
    if "defer.CancelledError" in content:
        content = content.replace("defer.CancelledError", "CancelledError")
        if (
            "from asyncio import CancelledError" not in content
            and "import asyncio" not in content
        ):
            # Find a good place to add the import
            content = _add_import(content, "from asyncio import CancelledError")
        changes.append("defer.CancelledError → CancelledError")

    # =================================================================
    # 2. Remove bare Twisted defer imports that are no longer needed
    # =================================================================

    # Remove `from twisted.internet import defer` if defer is only used for
    # CancelledError (which we've already replaced)
    # We can't safely remove this automatically since defer.* may be used elsewhere
    # Just flag it for now

    # =================================================================
    # 3. defer.succeed/defer.fail → direct returns (in limited contexts)
    # =================================================================
    # These are context-dependent and hard to automate safely.
    # Flag them for manual review.

    # =================================================================
    # 4. Twisted test imports
    # =================================================================

    # MemoryReactor type hint (used in prepare() signatures)
    if "from twisted.internet.testing import MemoryReactor" in content:
        # Replace with Any since it's just a type hint
        content = content.replace(
            "from twisted.internet.testing import MemoryReactor",
            "from typing import Any as MemoryReactor  # was: MemoryReactor from Twisted",
        )
        changes.append("MemoryReactor → Any")

    if "from twisted.test.proto_helpers import MemoryReactor" in content:
        content = content.replace(
            "from twisted.test.proto_helpers import MemoryReactor",
            "from typing import Any as MemoryReactor  # was: MemoryReactor from Twisted",
        )
        changes.append("MemoryReactor (old import) → Any")

    # =================================================================
    # Apply changes
    # =================================================================

    if content != original:
        stats.files_modified += 1
        stats.total_replacements += len(changes)
        if not dry_run:
            filepath.write_text(content)
        if verbose or dry_run:
            print(f"  {rel}: {', '.join(changes)}")
    else:
        stats.files_skipped += 1


def _add_import(content: str, import_line: str) -> str:
    """Add an import line after existing imports."""
    # Find the last import line and add after it
    lines = content.split("\n")
    last_import_idx = 0
    for i, line in enumerate(lines):
        if line.startswith("import ") or line.startswith("from "):
            last_import_idx = i
    lines.insert(last_import_idx + 1, import_line)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove Twisted imports from Synapse")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--verbose", action="store_true", help="Show all changes")
    args = parser.parse_args()

    root = Path.cwd()
    if not (root / "synapse").exists():
        print("Run from the synapse repo root", file=sys.stderr)
        sys.exit(1)

    stats = Stats()

    for directory in ["synapse", "tests"]:
        for pyfile in sorted((root / directory).rglob("*.py")):
            # Skip __pycache__ and generated files
            if "__pycache__" in str(pyfile):
                continue
            if any(p in str(pyfile) for p in ["html/", "mypy-html"]):
                continue
            transform_file(pyfile, args.dry_run, args.verbose, stats)

    action = "Would modify" if args.dry_run else "Modified"
    print(f"\n{action} {stats.files_modified} files ({stats.total_replacements} replacements)")
    print(f"Skipped {stats.files_skipped} files (no changes needed)")


if __name__ == "__main__":
    main()
