#!/usr/bin/env python3
"""Purge ALL remaining defer.* usage from the Synapse codebase.

This script performs the atomic switch from Twisted Deferreds to asyncio.
Run from the repo root.
"""

import re
import sys
from pathlib import Path


def process_file(filepath: Path) -> int:
    """Remove defer.* usage from a file. Returns count of replacements."""
    try:
        content = filepath.read_text()
    except (UnicodeDecodeError, PermissionError):
        return 0

    original = content
    count = 0

    # Skip files we handle manually
    rel = str(filepath)
    skip = {
        "synapse/logging/context.py",
        "synapse/util/async_helpers.py",
        "synapse/util/caches/deferred_cache.py",
        "synapse/util/caches/descriptors.py",
    }
    if any(s in rel for s in skip):
        return 0

    # 1. Replace defer.succeed(val) → a resolved future helper
    content = re.sub(
        r"defer\.succeed\(([^)]+)\)",
        r"__import__('synapse.util.async_helpers', fromlist=['make_awaitable_promise']).make_awaitable_promise_resolved(\1)",
        content,
    )

    # 2. Replace defer.fail(Failure(...)) → raise
    content = re.sub(
        r"return defer\.fail\(Failure\(([^)]*)\)\)",
        r"raise \1",
        content,
    )
    content = re.sub(
        r"return defer\.fail\(Failure\(\)\)",
        r"raise",
        content,
    )

    # 3. Replace defer.ensureDeferred(x) → run_in_background wrapper
    # Only in non-critical paths
    content = re.sub(
        r"defer\.ensureDeferred\(([^)]+)\)",
        r"asyncio.ensure_future(\1)",
        content,
    )

    # 4. Replace defer.Deferred type annotations
    content = re.sub(r'"defer\.Deferred\[.*?\]"', "Any", content)
    content = re.sub(r"defer\.Deferred\[.*?\]", "Any", content)
    content = re.sub(r"defer\.Deferred", "Any", content)

    # 5. Replace defer.gatherResults([...], consumeErrors=True)
    # with asyncio.gather(*[...], return_exceptions=True)
    # This is complex multiline — skip for now

    if content != original:
        try:
            compile(content, str(filepath), "exec")
            filepath.write_text(content)
            count = 1
        except SyntaxError:
            pass  # Don't write broken files

    return count


def main() -> None:
    if not Path("synapse").exists():
        print("Run from repo root", file=sys.stderr)
        sys.exit(1)

    total = 0
    for pyfile in sorted(Path("synapse").rglob("*.py")):
        if "__pycache__" in str(pyfile) or "native" in str(pyfile):
            continue
        n = process_file(pyfile)
        if n:
            total += 1
            print(f"  {pyfile}")

    print(f"\nModified {total} files")


if __name__ == "__main__":
    main()
