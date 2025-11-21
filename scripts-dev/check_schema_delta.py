#!/usr/bin/env python3

# Check that no schema deltas have been added to the wrong version.
#
# Also checks that schema deltas do not try and create or drop indices.

import re
from typing import Any

import click
import git

SCHEMA_FILE_REGEX = re.compile(r"^synapse/storage/schema/(.*)/delta/(.*)/(.*)$")
INDEX_CREATION_REGEX = re.compile(
    r"CREATE .*INDEX .*ON ([a-z_0-9]+)", flags=re.IGNORECASE
)
INDEX_DELETION_REGEX = re.compile(r"DROP .*INDEX ([a-z_0-9]+)", flags=re.IGNORECASE)
TABLE_CREATION_REGEX = re.compile(
    r"CREATE .*TABLE.* ([a-z_0-9]+)\s*\(", flags=re.IGNORECASE
)

# The base branch we want to check against. We use the main development branch
# on the assumption that is what we are developing against.
DEVELOP_BRANCH = "develop"


@click.command()
@click.option(
    "--force-colors",
    is_flag=True,
    flag_value=True,
    default=None,
    help="Always output ANSI colours",
)
def main(force_colors: bool) -> None:
    # Return code. Set to non-zero when we encounter an error
    return_code = 0

    click.secho(
        "+++ Checking schema deltas are in the right folder",
        fg="green",
        bold=True,
        color=force_colors,
    )

    click.secho("Updating repo...")

    repo = git.Repo()
    repo.remote().fetch(refspec=DEVELOP_BRANCH)

    click.secho("Getting current schema version...")

    r = repo.git.show(f"origin/{DEVELOP_BRANCH}:synapse/storage/schema/__init__.py")

    locals: dict[str, Any] = {}
    exec(r, locals)
    current_schema_version = locals["SCHEMA_VERSION"]

    diffs: list[git.Diff] = repo.remote().refs[DEVELOP_BRANCH].commit.diff(None)

    # Get the schema version of the local file to check against current schema on develop
    with open("synapse/storage/schema/__init__.py") as file:
        local_schema = file.read()
    new_locals: dict[str, Any] = {}
    exec(local_schema, new_locals)
    local_schema_version = new_locals["SCHEMA_VERSION"]

    if local_schema_version != current_schema_version:
        # local schema version must be +/-1 the current schema version on develop
        if abs(local_schema_version - current_schema_version) != 1:
            click.secho(
                f"The proposed schema version has diverged more than one version from {DEVELOP_BRANCH}, please fix!",
                fg="red",
                bold=True,
                color=force_colors,
            )
            click.get_current_context().exit(1)

        # right, we've changed the schema version within the allowable tolerance so
        # let's now use the local version as the canonical version
        current_schema_version = local_schema_version

    click.secho(f"Current schema version: {current_schema_version}")

    seen_deltas = False
    bad_delta_files = []
    changed_delta_files = []
    for diff in diffs:
        if diff.b_path is None:
            # We don't lint deleted files.
            continue

        match = SCHEMA_FILE_REGEX.match(diff.b_path)
        if not match:
            continue

        changed_delta_files.append(diff.b_path)

        if not diff.new_file:
            continue

        seen_deltas = True

        _, delta_version, _ = match.groups()

        if delta_version != str(current_schema_version):
            bad_delta_files.append(diff.b_path)

    if not seen_deltas:
        click.secho(
            "No deltas found.",
            fg="green",
            bold=True,
            color=force_colors,
        )
        return

    if bad_delta_files:
        bad_delta_files.sort()

        click.secho(
            "Found deltas in the wrong folder!",
            fg="red",
            bold=True,
            color=force_colors,
        )

        for f in bad_delta_files:
            click.secho(
                f"\t{f}",
                fg="red",
                bold=True,
                color=force_colors,
            )

        click.secho()
        click.secho(
            f"Please move these files to delta/{current_schema_version}/",
            fg="red",
            bold=True,
            color=force_colors,
        )

    else:
        click.secho(
            f"All deltas are in the correct folder: {current_schema_version}!",
            fg="green",
            bold=True,
            color=force_colors,
        )

    # Make sure we process them in order. This sort works because deltas are numbered
    # and delta files are also numbered in order.
    changed_delta_files.sort()

    # Now check that we're not trying to create or drop indices. If we want to
    # do that they should be in background updates. The exception is when we
    # create indices on tables we've just created.
    created_tables = set()
    for delta_file in changed_delta_files:
        with open(delta_file) as fd:
            delta_lines = fd.readlines()

        for line in delta_lines:
            # Strip SQL comments
            line = line.split("--", maxsplit=1)[0]

            # Check and track any tables we create
            match = TABLE_CREATION_REGEX.search(line)
            if match:
                table_name = match.group(1)
                created_tables.add(table_name)

            # Check for dropping indices, these are always banned
            match = INDEX_DELETION_REGEX.search(line)
            if match:
                clause = match.group()

                click.secho(
                    f"Found delta with index deletion: '{clause}' in {delta_file}",
                    fg="red",
                    bold=True,
                    color=force_colors,
                )
                click.secho(
                    " ↪ These should be in background updates.",
                )
                return_code = 1

            # Check for index creation, which is only allowed for tables we've
            # created.
            match = INDEX_CREATION_REGEX.search(line)
            if match:
                clause = match.group()
                table_name = match.group(1)
                if table_name not in created_tables:
                    click.secho(
                        f"Found delta with index creation for existing table: '{clause}' in {delta_file}",
                        fg="red",
                        bold=True,
                        color=force_colors,
                    )
                    click.secho(
                        " ↪ These should be in background updates (or the table should be created in the same delta).",
                    )
                    return_code = 1

    click.get_current_context().exit(return_code)


if __name__ == "__main__":
    main()
