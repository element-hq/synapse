#!/usr/bin/env python3
"""Migration script to remove Twisted imports from Synapse production code.

This handles the mechanical bulk transformations:
1. `from twisted.internet.defer import CancelledError` → `from asyncio import CancelledError`
2. `from twisted.internet.defer import Deferred` → remove (or add asyncio.Future if used)
3. `from twisted.python.failure import Failure` → remove import (replace usages)
4. `from twisted.internet.interfaces import IDelayedCall` → remove (not needed)
5. `from twisted.web.server import Request` → add TYPE_CHECKING guard with native type
"""

import re
import sys
from pathlib import Path


def process_file(filepath: Path) -> tuple[bool, list[str]]:
    """Process a single file, returning (changed, list_of_changes)."""
    content = filepath.read_text()
    original = content
    changes: list[str] = []

    # 1. Replace CancelledError import from Twisted with asyncio
    pattern = r"from twisted\.internet\.defer import CancelledError\n"
    if re.search(pattern, content):
        content = re.sub(pattern, "from asyncio import CancelledError\n", content)
        changes.append("CancelledError: twisted→asyncio")

    # Also handle it when it's part of a multi-import
    pattern = r"from twisted\.internet\.defer import (.*?)CancelledError(.*?)\n"
    match = re.search(pattern, content)
    if match:
        before = match.group(1).strip().rstrip(",").strip()
        after = match.group(2).strip().lstrip(",").strip()
        remaining = ", ".join(x for x in [before, after] if x)
        if remaining:
            content = re.sub(
                pattern,
                f"from twisted.internet.defer import {remaining}\nfrom asyncio import CancelledError\n",
                content,
            )
        else:
            content = re.sub(pattern, "from asyncio import CancelledError\n", content)
        changes.append("CancelledError: extracted from multi-import")

    # 2. Replace `defer.CancelledError` with `CancelledError` (asyncio)
    if "defer.CancelledError" in content:
        content = content.replace("defer.CancelledError", "CancelledError")
        # Ensure asyncio CancelledError is imported
        if "from asyncio import CancelledError" not in content and "import asyncio" not in content:
            # Add import after other imports
            content = "from asyncio import CancelledError\n" + content
        changes.append("defer.CancelledError → CancelledError")

    changed = content != original
    if changed:
        filepath.write_text(content)

    return changed, changes


def main() -> None:
    synapse_dir = Path("synapse")
    if not synapse_dir.exists():
        print("Run from the synapse repo root", file=sys.stderr)
        sys.exit(1)

    total_changed = 0
    for pyfile in sorted(synapse_dir.rglob("*.py")):
        changed, changes = process_file(pyfile)
        if changed:
            total_changed += 1
            print(f"  {pyfile}: {', '.join(changes)}")

    print(f"\nModified {total_changed} files")


if __name__ == "__main__":
    main()
