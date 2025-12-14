#!/usr/bin/env python3

# Check that no schema deltas have been added to the wrong version.
#
# Also checks that schema deltas do not try and create or drop indices.

import re
from typing import Any

import click
import git
import sqlglot
import sqlglot.expressions

SCHEMA_FILE_REGEX = re.compile(r"^synapse/storage/schema/(.*)/delta/(.*)/(.*)$")

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

        # Mark this run as not successful, but continue so that we report *all*
        # errors.
        return_code = 1
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

    success = check_schema_delta(changed_delta_files, force_colors)
    if not success:
        return_code = 1

    click.get_current_context().exit(return_code)


def check_schema_delta(delta_files: list[str], force_colors: bool) -> bool:
    """Check that the given schema delta files do not create or drop indices
    inappropriately.

    Index creation is only allowed on tables created in the same set of deltas.

    Index deletion is never allowed and should be done in background updates.

    Returns:
        True if all checks succeeded, False if at least one failed.
    """

    # The tables created in this delta
    created_tables = set[str]()

    # The indices created/dropped in this delta, each a tuple of (table_name, sql)
    created_indices = list[tuple[str, str]]()

    # The indices dropped in this delta, just the sql
    dropped_indices = list[str]()

    for delta_file in delta_files:
        with open(delta_file) as fd:
            delta_contents = fd.read()

        # Assume the SQL dialect from the file extension, defaulting to Postgres.
        sql_lang = "postgres"
        if delta_file.endswith(".sqlite"):
            sql_lang = "sqlite"

        statements = sqlglot.parse(delta_contents, read=sql_lang)

        for statement in statements:
            if isinstance(statement, sqlglot.expressions.Create):
                if statement.kind == "TABLE":
                    assert isinstance(statement.this, sqlglot.expressions.Schema)
                    assert isinstance(statement.this.this, sqlglot.expressions.Table)

                    table_name = statement.this.this.name
                    created_tables.add(table_name)
                elif statement.kind == "INDEX":
                    assert isinstance(statement.this, sqlglot.expressions.Index)

                    table_name = statement.this.args["table"].name
                    created_indices.append((table_name, statement.sql()))
            elif isinstance(statement, sqlglot.expressions.Drop):
                if statement.kind == "INDEX":
                    dropped_indices.append(statement.sql())

    success = True
    for table_name, clause in created_indices:
        if table_name not in created_tables:
            click.secho(
                f"Found delta with index creation for existing table: '{clause}'",
                fg="red",
                bold=True,
                color=force_colors,
            )
            click.secho(
                " ↪ These should be in background updates (or the table should be created in the same delta).",
            )
            success = False

    for clause in dropped_indices:
        click.secho(
            f"Found delta with index deletion: '{clause}'",
            fg="red",
            bold=True,
            color=force_colors,
        )
        click.secho(
            " ↪ These should be in background updates.",
        )
        success = False

    return success


if __name__ == "__main__":
    main()
