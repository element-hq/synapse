#!/usr/bin/env python3
#
# Get a diff showing the change in the database schema.
#
# Usage:
#   # Compare against develop (default):
#   PGUSER=postgres PGPASSWORD=postgres scripts-dev/schema_diff.py
#
#   # Compare against a specific branch/commit:
#   PGUSER=postgres PGPASSWORD=postgres scripts-dev/schema_diff.py --base origin/release-v1.100

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_DIR = "synapse/storage/schema"
MAKE_FULL_SCHEMA_SCRIPT = REPO_ROOT / "scripts-dev" / "make_full_schema.sh"


def run_make_full_schema(output_dir: Path) -> None:
    """Run make_full_schema.sh, piping the password via stdin."""
    pg_user = os.environ.get("PGUSER", "")
    pg_password = os.environ.get("PGPASSWORD", "")
    if not pg_user:
        print("ERROR: PGUSER environment variable not set.", file=sys.stderr)
        sys.exit(1)
    if not pg_password:
        print("ERROR: PGPASSWORD environment variable not set.", file=sys.stderr)
        sys.exit(1)

    cmd: list[str] = [
        # Use faketime here for schema deltas that are wall-clock sensitive under SQLite
        # We must only use faketime at this level because freezing the clock
        # seems to cause `poetry install` to hang when recompiling our Rust module
        "faketime",
        "-f",
        "2001-05-25 12:42:42",
        "poetry",
        "run",
        str(MAKE_FULL_SCHEMA_SCRIPT),
        "-p",
        pg_user,
        "-o",
        str(output_dir),
        "-c",
        "-n",
        "9999",
    ]

    print(f"Running: {' '.join(cmd)}", file=sys.stderr)

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=REPO_ROOT,
        text=True,
    )
    stdout, _ = proc.communicate(input=pg_password + "\n")
    # Forward script output to stderr so stdout stays clean for markdown
    if stdout:
        print(stdout, file=sys.stderr, end="")
    if proc.returncode != 0:
        print(
            f"ERROR: make_full_schema.sh failed with exit code {proc.returncode}",
            file=sys.stderr,
        )
        sys.exit(proc.returncode)


def diff_schemas(
    before_dir: Path, after_dir: Path, before_ref: str, after_ref: str
) -> str:
    """Diff SQLite and Postgres full schemas, return a Markdown report."""
    parts: list[str] = [
        "## Schema Diff",
        "",
        "Please check that this looks as expected!",
        "",
    ]

    for db in ["common", "main", "state"]:
        for engine in ["sqlite", "postgres"]:
            filename = f"full.sql.{engine}"

            before_file = before_dir / db / "full_schemas" / "9999" / filename

            after_file = after_dir / db / "full_schemas" / "9999" / filename

            if not before_file.exists():
                raise RuntimeError(f"No before file found for {db = }, {engine = }")
            if not after_file.exists():
                raise RuntimeError(f"No after file found for {db = }, {engine = }")

            result = subprocess.run(
                ["diff", "-U", "10", str(before_file), str(after_file)],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                parts.append(f"### {db} ({engine})\n\nUnchanged\n")
            else:
                parts.append(f"### {db} ({engine})\n\n```diff\n{result.stdout}\n```\n")

    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show database schema changes")
    parser.add_argument(
        "--base",
        default="develop",
        help="Base commit/branch to compare against (default: develop)",
    )
    args = parser.parse_args()

    # Create temp output directory with before/after subdirectories
    with tempfile.TemporaryDirectory(prefix="schema_diff_") as tmpdir:
        after_dir = Path(tmpdir) / "after"
        before_dir = Path(tmpdir) / "before"
        after_dir.mkdir()
        before_dir.mkdir()

        print("\n--- Running make_full_schema.sh (after) ---", file=sys.stderr)
        run_make_full_schema(after_dir)

        # Checkout base and run make_full_schema.sh
        print(
            f"\n--- Checking out {args.base} and running make_full_schema.sh (before) ---",
            file=sys.stderr,
        )

        # Save current ref so we can return to it without detaching.
        # (Not useful in CI, but is useful for local development.)
        # If we are on a named branch, use the branch name; otherwise use the SHA.
        head_ref = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        ).stdout.strip()

        before_sha = subprocess.run(
            ["git", "rev-parse", args.base],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        ).stdout.strip()
        after_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        ).stdout.strip()

        # Check if working tree is dirty before potentially stashing
        status = subprocess.run(
            [
                "git",
                "status",
                # Machine-readable output for easy parsing
                "--porcelain",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        ).stdout.strip()

        did_stash = False
        if status:
            print("Stashing local changes before checkout...", file=sys.stderr)
            subprocess.run(
                [
                    "git",
                    "stash",
                    "push",
                    "--include-untracked",
                    "-m",
                    "schema_diff temporary stash",
                ],
                cwd=REPO_ROOT,
                check=True,
            )
            did_stash = True

        try:
            subprocess.run(["git", "checkout", args.base], cwd=REPO_ROOT, check=True)

            # Refresh dependencies
            print("Installing dependencies for base commit...", file=sys.stderr)
            subprocess.run(
                ["poetry", "install", "--extras", "postgres"],
                cwd=REPO_ROOT,
                check=True,
                # Poetry install is noisy, so pipe its stdout to stderr
                stdout=sys.stderr,
            )

            run_make_full_schema(before_dir)
        finally:
            print("Returning to HEAD...", file=sys.stderr)
            subprocess.run(
                [
                    "git",
                    "checkout",
                    head_ref,
                ],
                cwd=REPO_ROOT,
                check=True,
            )
            if did_stash:
                subprocess.run(["git", "stash", "pop"], cwd=REPO_ROOT, check=True)
                print("✓ Restored stashed changes.", file=sys.stderr)

        # Diff
        print("\n--- Diffing schemas ---", file=sys.stderr)
        markdown = diff_schemas(before_dir, after_dir, before_sha, after_sha)

        print(markdown)


if __name__ == "__main__":
    main()
