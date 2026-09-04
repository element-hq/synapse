#!/usr/bin/env python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""An interactive script for creating and committing a changelog newsfile."""

import itertools
import os
import re
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager

import click
import git
import github
import github.Auth
from github import Github

# The changelog types from the `[tool.towncrier]` section of pyproject.toml.
CHANGELOG_TYPES = ("feature", "bugfix", "docker", "doc", "removal", "misc")


@click.command()
@click.option("--gh-token", envvar=["GH_TOKEN", "GITHUB_TOKEN"], required=False)
def cli(gh_token: str | None) -> None:
    """Create a `changelog.d/<number>.<type>` newsfile and commit it.

    Prompts for the newsfile text and type. The number should be the PR the
    change will land in: if the current branch already has an open PR we use
    its number, otherwise we guess by asking GitHub what the next issue
    number will be, since issues and PRs share a counter.

    A GitHub token isn't required, but one can be passed via --gh-token (or
    the GH_TOKEN/GITHUB_TOKEN env vars) to avoid anonymous rate limits.
    """

    repo = git.Repo(path=".", search_parent_directories=True)
    if repo.working_tree_dir is None:
        raise click.ClickException("Not in a git working tree.")

    text = click.prompt("Newsfile text").strip()
    if not text.endswith((".", "!", "?")):
        text += "."

    changelog_type = prompt_changelog_type()

    number = click.prompt(
        "PR number", type=int, default=guess_pr_number(repo, gh_token)
    )

    filename = os.path.join("changelog.d", f"{number}.{changelog_type}")
    full_path = os.path.join(repo.working_tree_dir, filename)
    if os.path.exists(full_path):
        raise click.ClickException(f"{filename} already exists!")

    with open(full_path, "w") as f:
        f.write(text + "\n")

    repo.git.add(full_path)
    repo.git.commit("-m", "Newsfile", "--", full_path)

    click.secho(f"Committed {filename}", fg="green")


def prompt_changelog_type(default: str = "misc") -> str:
    """Pick the changelog type from a numbered menu with a single keypress."""

    # Fall back to a regular prompt when stdin isn't a terminal, so the
    # script can still be driven from a pipe.
    if not sys.stdin.isatty():
        return click.prompt(
            "Type",
            type=click.Choice(CHANGELOG_TYPES),
            show_choices=True,
            default=default,
        )

    for i, changelog_type in enumerate(CHANGELOG_TYPES, start=1):
        click.echo(f"  {i}) {changelog_type}")

    while True:
        click.echo(f"Type 1-{len(CHANGELOG_TYPES)} [{default}]: ", nl=False)
        char = click.getchar()

        if char in ("\x03", "\x04"):  # Ctrl-C / Ctrl-D
            click.echo()
            raise click.Abort()

        if char in ("\r", "\n"):
            click.echo(default)
            return default

        if char.isdigit() and 1 <= int(char) <= len(CHANGELOG_TYPES):
            chosen = CHANGELOG_TYPES[int(char) - 1]
            click.echo(chosen)
            return chosen

        # Invalid key: reprint the prompt on a fresh line.
        click.echo()


@contextmanager
def spinner(message: str) -> Generator[None, None, None]:
    """Show a spinner with the given message while the block runs."""

    # Don't try to animate if output is going to a pipe.
    if not sys.stdout.isatty():
        click.echo(f"{message}...")
        yield
        return

    stop = threading.Event()

    def spin() -> None:
        for char in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            click.echo(f"\r{char} {message}...", nl=False)
            if stop.wait(0.1):
                break

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        # Wipe the spinner line.
        click.echo("\r\x1b[2K", nl=False)


def guess_pr_number(repo: git.Repo, gh_token: str | None) -> int | None:
    """Guess the PR number the newsfile should be named after.

    If the current branch already has an open PR, use its number. Otherwise
    guess the number of the next PR to be opened, i.e. one more than the most
    recently created issue or PR. Returns None if GitHub can't be reached.
    """
    if gh_token:
        gh = Github(auth=github.Auth.Token(token=gh_token))
    else:
        # Use github anonymously.
        gh = Github()

    try:
        pr_number = None
        next_number = None
        with spinner("Querying GitHub for the PR number"):
            gh_repo = gh.get_repo("element-hq/synapse")

            pr_number = find_existing_pr_number(repo, gh_repo)
            if pr_number is None:
                # Issues and PRs share a counter, and the issues endpoint
                # includes PRs, so the most recently created issue has the
                # highest number.
                latest = gh_repo.get_issues(
                    state="all", sort="created", direction="desc"
                )[0]
                next_number = latest.number + 1

        if pr_number is not None:
            click.echo(f"Found an open PR for this branch: #{pr_number}")
            return pr_number

        return next_number
    except Exception as e:
        click.echo(f"Failed to guess the PR number from GitHub: {e}")
        return None


def find_existing_pr_number(
    repo: git.Repo, gh_repo: github.Repository.Repository
) -> int | None:
    """Return the number of the open PR for the current branch, if any.

    PR heads are qualified by the owner of the repo they come from, which for
    forks isn't element-hq, so we collect candidate owners from the GitHub
    remotes of the local checkout.
    """
    if repo.head.is_detached:
        return None
    branch_name = repo.active_branch.name

    owners = set()
    for remote in repo.remotes:
        for url in remote.urls:
            match = re.search(r"github\.com[:/]([^/]+)/", url)
            if match:
                owners.add(match.group(1))

    for owner in owners:
        for pr in gh_repo.get_pulls(state="open", head=f"{owner}:{branch_name}"):
            return pr.number

    return None


if __name__ == "__main__":
    cli()
