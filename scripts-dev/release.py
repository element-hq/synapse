#!/usr/bin/env python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

"""An interactive script for doing a release. See `cli()` below."""

import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from os import path
from tempfile import TemporaryDirectory
from typing import Any, List, Match, Optional, Union

import attr
import click
import commonmark
import git
from click.exceptions import ClickException
from git import GitCommandError, Repo
from github import BadCredentialsException, Github
from packaging import version


def run_until_successful(
    command: str, *args: Any, **kwargs: Any
) -> subprocess.CompletedProcess:
    while True:
        completed_process = subprocess.run(command, *args, **kwargs)
        exit_code = completed_process.returncode
        if exit_code == 0:
            # successful, so nothing more to do here.
            return completed_process

        print(f"The command {command!r} failed with exit code {exit_code}.")
        print("Please try to correct the failure and then re-run.")
        click.confirm("Try again?", abort=True)


@click.group()
def cli() -> None:
    """An interactive script to walk through the parts of creating a release.

    Requirements:
      - The dev dependencies be installed, which can be done via:

            pip install -e .[dev]

      - A checkout of the sytest repository at ../sytest
      - A checkout of the complement repository at ../complement

    Then to use:

        ./scripts-dev/release.py prepare

        # ... ask others to look at the changelog ...

        ./scripts-dev/release.py tag

        # wait for assets to build, either manually or with:
        ./scripts-dev/release.py wait-for-actions

        ./scripts-dev/release.py publish

        ./scripts-dev/release.py upload

        ./scripts-dev/release.py merge-back

        # Optional: generate some nice links for the announcement
        ./scripts-dev/release.py announce

    Alternatively, `./scripts-dev/release.py full` will do all the above
    as well as guiding you through the manual steps.

    If the env var GH_TOKEN (or GITHUB_TOKEN) is set, or passed into the
    `tag`/`publish` command, then a new draft release will be created/published.
    """


@cli.command()
def prepare() -> None:
    _prepare()


def _prepare() -> None:
    """Do the initial stages of creating a release, including creating release
    branch, updating changelog and pushing to GitHub.
    """

    # Make sure we're in a git repo.
    synapse_repo = get_repo_and_check_clean_checkout()
    sytest_repo = get_repo_and_check_clean_checkout("../sytest", "sytest")
    complement_repo = get_repo_and_check_clean_checkout("../complement", "complement")

    click.secho("Updating Synapse and Sytest git repos...")
    synapse_repo.remote().fetch()
    sytest_repo.remote().fetch()
    complement_repo.remote().fetch()

    # Get the current version and AST from root Synapse module.
    current_version = get_package_version()

    # Figure out what sort of release we're doing and calcuate the new version.
    rc = click.confirm("RC", default=True)
    if current_version.pre:
        # If the current version is an RC we don't need to bump any of the
        # version numbers (other than the RC number).
        if rc:
            new_version = "{}.{}.{}rc{}".format(
                current_version.major,
                current_version.minor,
                current_version.micro,
                current_version.pre[1] + 1,
            )
        else:
            new_version = "{}.{}.{}".format(
                current_version.major,
                current_version.minor,
                current_version.micro,
            )
    else:
        # If this is a new release cycle then we need to know if it's a minor
        # or a patch version bump.
        release_type = click.prompt(
            "Release type",
            type=click.Choice(("minor", "patch")),
            show_choices=True,
            default="minor",
        )

        if release_type == "minor":
            if rc:
                new_version = "{}.{}.{}rc1".format(
                    current_version.major,
                    current_version.minor + 1,
                    0,
                )
            else:
                new_version = "{}.{}.{}".format(
                    current_version.major,
                    current_version.minor + 1,
                    0,
                )
        else:
            if rc:
                new_version = "{}.{}.{}rc1".format(
                    current_version.major,
                    current_version.minor,
                    current_version.micro + 1,
                )
            else:
                new_version = "{}.{}.{}".format(
                    current_version.major,
                    current_version.minor,
                    current_version.micro + 1,
                )

    # Confirm the calculated version is OK.
    if not click.confirm(f"Create new version: {new_version}?", default=True):
        click.get_current_context().abort()

    # Switch to the release branch.
    parsed_new_version = version.parse(new_version)

    # We assume for debian changelogs that we only do RCs or full releases.
    assert not parsed_new_version.is_devrelease
    assert not parsed_new_version.is_postrelease

    release_branch_name = get_release_branch_name(parsed_new_version)
    release_branch = find_ref(synapse_repo, release_branch_name)
    if release_branch:
        if release_branch.is_remote():
            # If the release branch only exists on the remote we check it out
            # locally.
            synapse_repo.git.checkout(release_branch_name)
    else:
        # If a branch doesn't exist we create one. We ask which one branch it
        # should be based off, defaulting to sensible values depending on the
        # release type.
        if current_version.is_prerelease:
            default = release_branch_name
        elif release_type == "minor":
            default = "develop"
        else:
            default = "master"

        branch_name = click.prompt(
            "Which branch should the release be based on?", default=default
        )

        for repo_name, repo in {
            "synapse": synapse_repo,
            "sytest": sytest_repo,
            "complement": complement_repo,
        }.items():
            # Special case for Complement: `develop` maps to `main`
            if repo_name == "complement" and branch_name == "develop":
                branch_name = "main"

            base_branch = find_ref(repo, branch_name)
            if not base_branch:
                print(f"Could not find base branch {branch_name} for {repo_name}!")
                click.get_current_context().abort()

            # Check out the base branch and ensure it's up to date
            repo.head.set_reference(
                base_branch, f"check out the base branch for {repo_name}"
            )
            repo.head.reset(index=True, working_tree=True)
            if not base_branch.is_remote():
                update_branch(repo)

            # Create the new release branch
            repo.create_head(release_branch_name, commit=base_branch)

        # Special-case SyTest: we don't actually prepare any files so we may
        # as well push it now (and only when we create a release branch;
        # not on subsequent RCs or full releases).
        if click.confirm("Push new SyTest branch?", default=True):
            sytest_repo.git.push("-u", sytest_repo.remote().name, release_branch_name)

        # Same for Complement
        if click.confirm("Push new Complement branch?", default=True):
            complement_repo.git.push(
                "-u", complement_repo.remote().name, release_branch_name
            )

    # Switch to the release branch and ensure it's up to date.
    synapse_repo.git.checkout(release_branch_name)
    update_branch(synapse_repo)

    # Update the version specified in pyproject.toml.
    subprocess.check_output(["poetry", "version", new_version])

    # Generate changelogs.
    generate_and_write_changelog(synapse_repo, current_version, new_version)

    # Generate debian changelogs
    if parsed_new_version.pre is not None:
        # If this is an RC then we need to coerce the version string to match
        # Debian norms, e.g. 1.39.0rc2 gets converted to 1.39.0~rc2.
        base_ver = parsed_new_version.base_version
        pre_type, pre_num = parsed_new_version.pre
        debian_version = f"{base_ver}~{pre_type}{pre_num}"
    else:
        debian_version = new_version

    if sys.platform == "darwin":
        run_until_successful(
            f"docker run --rm -v .:/synapse ubuntu:latest /synapse/scripts-dev/docker_update_debian_changelog.sh {new_version}",
            shell=True,
        )
    else:
        run_until_successful(
            f'dch -M -v {debian_version} "New Synapse release {new_version}."',
            shell=True,
        )
        run_until_successful('dch -M -r -D stable ""', shell=True)

    # Show the user the changes and ask if they want to edit the change log.
    synapse_repo.git.add("-u")
    subprocess.run("git diff --cached", shell=True)

    if click.confirm("Edit changelog?", default=False):
        click.edit(filename="CHANGES.md")

    # Commit the changes.
    synapse_repo.git.add("-u")
    synapse_repo.git.commit("-m", new_version)

    # We give the option to bail here in case the user wants to make sure things
    # are OK before pushing.
    if not click.confirm("Push branch to github?", default=True):
        print("")
        print("Run when ready to push:")
        print("")
        print(
            f"\tgit push -u {synapse_repo.remote().name} {synapse_repo.active_branch.name}"
        )
        print("")
        sys.exit(0)

    # Otherwise, push and open the changelog in the browser.
    synapse_repo.git.push(
        "-u", synapse_repo.remote().name, synapse_repo.active_branch.name
    )

    print("Opening the changelog in your browser...")
    print("Please ask #synapse-dev to give it a check.")
    click.launch(
        f"https://github.com/element-hq/synapse/blob/{synapse_repo.active_branch.name}/CHANGES.md"
    )


@cli.command()
@click.option("--gh-token", envvar=["GH_TOKEN", "GITHUB_TOKEN"])
def tag(gh_token: Optional[str]) -> None:
    _tag(gh_token)


def _tag(gh_token: Optional[str]) -> None:
    """Tags the release and generates a draft GitHub release"""

    # Test that the GH Token is valid before continuing.
    check_valid_gh_token(gh_token)

    # Make sure we're in a git repo.
    repo = get_repo_and_check_clean_checkout()

    click.secho("Updating git repo...")
    repo.remote().fetch()

    # Find out the version and tag name.
    current_version = get_package_version()
    tag_name = f"v{current_version}"

    # Check we haven't released this version.
    if tag_name in repo.tags:
        raise click.ClickException(f"Tag {tag_name} already exists!\n")

    # Check we're on the right release branch
    release_branch = get_release_branch_name(current_version)
    if repo.active_branch.name != release_branch:
        click.echo(
            f"Need to be on the release branch ({release_branch}) before tagging. "
            f"Currently on ({repo.active_branch.name})."
        )
        click.get_current_context().abort()

    # Get the appropriate changelogs and tag.
    changes = get_changes_for_version(current_version)

    click.echo_via_pager(changes)
    if click.confirm("Edit text?", default=False):
        edited_changes = click.edit(changes, require_save=False)
        # This assert is for mypy's benefit. click's docs are a little unclear, but
        # when `require_save=False`, not saving the temp file in the editor returns
        # the original string.
        assert edited_changes is not None
        changes = edited_changes

    repo.create_tag(tag_name, message=changes, sign=True)

    if not click.confirm("Push tag to GitHub?", default=True):
        print("")
        print("Run when ready to push:")
        print("")
        print(f"\tgit push {repo.remote().name} tag {current_version}")
        print("")
        return

    repo.git.push(repo.remote().name, "tag", tag_name)

    # If no token was given, we bail here
    if not gh_token:
        print("Launching the GitHub release page in your browser.")
        print("Please correct the title and create a draft.")
        if current_version.is_prerelease:
            print("As this is an RC, remember to mark it as a pre-release!")
        print("(by the way, this step can be automated by passing --gh-token,")
        print("or one of the GH_TOKEN or GITHUB_TOKEN env vars.)")
        click.launch(f"https://github.com/element-hq/synapse/releases/edit/{tag_name}")

        print("Once done, you need to wait for the release assets to build.")
        if click.confirm("Launch the release assets actions page?", default=True):
            click.launch(
                f"https://github.com/element-hq/synapse/actions?query=branch%3A{tag_name}"
            )
        return

    # Create a new draft release
    gh = Github(gh_token)
    gh_repo = gh.get_repo("element-hq/synapse")
    release = gh_repo.create_git_release(
        tag=tag_name,
        name=tag_name,
        message=changes,
        draft=True,
        prerelease=current_version.is_prerelease,
    )

    # Open the release and the actions where we are building the assets.
    print("Launching the release page and the actions page.")
    click.launch(release.html_url)
    click.launch(
        f"https://github.com/element-hq/synapse/actions?query=branch%3A{tag_name}"
    )

    click.echo("Wait for release assets to be built")


@cli.command()
@click.option("--gh-token", envvar=["GH_TOKEN", "GITHUB_TOKEN"], required=True)
def publish(gh_token: str) -> None:
    _publish(gh_token)


def _publish(gh_token: str) -> None:
    """Publish release on GitHub."""

    if gh_token:
        # Test that the GH Token is valid before continuing.
        gh = Github(gh_token)
        gh.get_user()

    # Make sure we're in a git repo.
    get_repo_and_check_clean_checkout()

    current_version = get_package_version()
    tag_name = f"v{current_version}"

    if not click.confirm(f"Publish release {tag_name} on GitHub?", default=True):
        return

    # Publish the draft release
    gh = Github(gh_token)
    gh_repo = gh.get_repo("element-hq/synapse")
    for release in gh_repo.get_releases():
        if release.title == tag_name:
            break
    else:
        raise ClickException(f"Failed to find GitHub release for {tag_name}")

    assert release.title == tag_name

    if not release.draft:
        click.echo("Release already published.")
        return

    release = release.update_release(
        name=release.title,
        message=release.body,
        tag_name=release.tag_name,
        prerelease=release.prerelease,
        draft=False,
    )


@cli.command()
@click.option("--gh-token", envvar=["GH_TOKEN", "GITHUB_TOKEN"], required=False)
def upload(gh_token: Optional[str]) -> None:
    _upload(gh_token)


def _upload(gh_token: Optional[str]) -> None:
    """Upload release to pypi."""

    # Test that the GH Token is valid before continuing.
    check_valid_gh_token(gh_token)

    current_version = get_package_version()
    tag_name = f"v{current_version}"

    # Check we have the right tag checked out.
    repo = get_repo_and_check_clean_checkout()
    tag = repo.tag(f"refs/tags/{tag_name}")
    if repo.head.commit != tag.commit:
        click.echo(f"Tag {tag_name} ({tag.commit}) is not currently checked out!")
        click.get_current_context().abort()

    # Query all the assets corresponding to this release.
    gh = Github(gh_token)
    gh_repo = gh.get_repo("element-hq/synapse")
    gh_release = gh_repo.get_release(tag_name)

    all_assets = set(gh_release.get_assets())

    # Only accept the wheels and sdist.
    # Notably: we don't care about debs.tar.xz.
    asset_names_and_urls = sorted(
        (asset.name, asset.browser_download_url)
        for asset in all_assets
        if asset.name.endswith((".whl", ".tar.gz"))
    )

    # Print out what we've determined.
    print("Found relevant assets:")
    for asset_name, _ in asset_names_and_urls:
        print(f" - {asset_name}")

    ignored_asset_names = sorted(
        {asset.name for asset in all_assets}
        - {asset_name for asset_name, _ in asset_names_and_urls}
    )
    print("\nIgnoring irrelevant assets:")
    for asset_name in ignored_asset_names:
        print(f" - {asset_name}")

    with TemporaryDirectory(prefix=f"synapse_upload_{tag_name}_") as tmpdir:
        for name, asset_download_url in asset_names_and_urls:
            filename = path.join(tmpdir, name)

            click.echo(f"Downloading {name} into {filename}")
            urllib.request.urlretrieve(asset_download_url, filename=filename)

        if click.confirm("Upload to PyPI?", default=True):
            subprocess.run("twine upload *", shell=True, cwd=tmpdir)

    click.echo(
        f"Done! Remember to merge the tag {tag_name} into the appropriate branches"
    )


def _merge_into(repo: Repo, source: str, target: str) -> None:
    """
    Merges branch `source` into branch `target`.
    Pulls both before merging and pushes the result.
    """

    # Update our branches and switch to the target branch
    for branch in [source, target]:
        click.echo(f"Switching to {branch} and pulling...")
        repo.heads[branch].checkout()
        # Pull so we're up to date
        repo.remote().pull()

    assert repo.active_branch.name == target

    try:
        # TODO This seemed easier than using GitPython directly
        click.echo(f"Merging {source}...")
        repo.git.merge(source)
    except GitCommandError as exc:
        # If a merge conflict occurs, give some context and try to
        # make it easy to abort if necessary.
        click.echo(exc)
        if not click.confirm(
            f"Likely merge conflict whilst merging ({source} → {target}). "
            f"Have you resolved it?"
        ):
            repo.git.merge("--abort")
            return

    # Push result.
    click.echo("Pushing...")
    repo.remote().push()


@cli.command()
@click.option("--gh-token", envvar=["GH_TOKEN", "GITHUB_TOKEN"], required=False)
def wait_for_actions(gh_token: Optional[str]) -> None:
    _wait_for_actions(gh_token)


def _wait_for_actions(gh_token: Optional[str]) -> None:
    # Test that the GH Token is valid before continuing.
    check_valid_gh_token(gh_token)

    # Find out the version and tag name.
    current_version = get_package_version()
    tag_name = f"v{current_version}"

    # Authentication is optional on this endpoint,
    # but use a token if we have one to reduce the chance of being rate-limited.
    url = f"https://api.github.com/repos/element-hq/synapse/actions/runs?branch={tag_name}"
    headers = {"Accept": "application/vnd.github+json"}
    if gh_token is not None:
        headers["authorization"] = f"token {gh_token}"
    req = urllib.request.Request(url, headers=headers)

    time.sleep(10 * 60)
    while True:
        time.sleep(5 * 60)
        response = urllib.request.urlopen(req)
        resp = json.loads(response.read())

        if len(resp["workflow_runs"]) == 0:
            continue

        if all(
            workflow["status"] != "in_progress" for workflow in resp["workflow_runs"]
        ):
            success = (
                workflow["status"] == "completed" for workflow in resp["workflow_runs"]
            )
            if success:
                _notify("Workflows successful. You can now continue the release.")
            else:
                _notify("Workflows failed.")
                click.confirm("Continue anyway?", abort=True)

            break


def _notify(message: str) -> None:
    # Send a bell character. Most terminals will play a sound or show a notification
    # for this.
    click.echo(f"\a{message}")

    app_name = "Synapse Release Script"

    # Try and run notify-send, but don't raise an Exception if this fails
    # (This is best-effort)
    if sys.platform == "darwin":
        # See https://developer.apple.com/library/archive/documentation/AppleScript/Conceptual/AppleScriptLangGuide/reference/ASLR_cmds.html#//apple_ref/doc/uid/TP40000983-CH216-SW224
        subprocess.run(
            f"""osascript -e 'display notification "{message}" with title "{app_name}"'""",
            shell=True,
        )
    else:
        subprocess.run(
            [
                "notify-send",
                "--app-name",
                app_name,
                "--expire-time",
                "3600000",
                message,
            ]
        )


@cli.command()
def merge_back() -> None:
    _merge_back()


def _merge_back() -> None:
    """Merge the release branch back into the appropriate branches.
    All branches will be automatically pulled from the remote and the results
    will be pushed to the remote."""

    synapse_repo = get_repo_and_check_clean_checkout()
    branch_name = synapse_repo.active_branch.name

    if not branch_name.startswith("release-v"):
        raise RuntimeError("Not on a release branch. This does not seem sensible.")

    # Pull so we're up to date
    synapse_repo.remote().pull()

    current_version = get_package_version()

    if current_version.is_prerelease:
        # Release candidate
        if click.confirm(f"Merge {branch_name} → develop?", default=True):
            _merge_into(synapse_repo, branch_name, "develop")
    else:
        # Full release
        sytest_repo = get_repo_and_check_clean_checkout("../sytest", "sytest")
        complement_repo = get_repo_and_check_clean_checkout(
            "../complement", "complement"
        )

        if click.confirm(f"Merge {branch_name} → master?", default=True):
            _merge_into(synapse_repo, branch_name, "master")

        if click.confirm("Merge master → develop?", default=True):
            _merge_into(synapse_repo, "master", "develop")

        if click.confirm(f"On SyTest, merge {branch_name} → master?", default=True):
            _merge_into(sytest_repo, branch_name, "master")

        if click.confirm("On SyTest, merge master → develop?", default=True):
            _merge_into(sytest_repo, "master", "develop")

        if click.confirm(f"On Complement, merge {branch_name} → main?", default=True):
            _merge_into(complement_repo, branch_name, "main")


@cli.command()
def announce() -> None:
    _announce()


def _announce() -> None:
    """Generate markdown to announce the release."""

    current_version = get_package_version()
    tag_name = f"v{current_version}"

    click.echo(
        f"""
Hi everyone. Synapse {current_version} has just been released.

[notes](https://github.com/element-hq/synapse/releases/tag/{tag_name}) | \
[docker](https://hub.docker.com/r/matrixdotorg/synapse/tags?name={tag_name}) | \
[debs](https://packages.matrix.org/debian/) | \
[pypi](https://pypi.org/project/matrix-synapse/{current_version}/)"""
    )

    if "rc" in tag_name:
        click.echo(
            """
Announce the RC in
- #homeowners:matrix.org (Synapse Announcements)
- #synapse-dev:matrix.org"""
        )
    else:
        click.echo(
            """
Announce the release in
- #homeowners:matrix.org (Synapse Announcements), bumping the version in the topic
- #synapse:matrix.org (Synapse Admins), bumping the version in the topic
- #synapse-dev:matrix.org
- #synapse-package-maintainers:matrix.org

Ask the designated people to do the blog and tweets."""
        )


@cli.command()
@click.option("--gh-token", envvar=["GH_TOKEN", "GITHUB_TOKEN"], required=True)
def full(gh_token: str) -> None:
    if gh_token:
        # Test that the GH Token is valid before continuing.
        gh = Github(gh_token)
        gh.get_user()

    click.echo("1. If this is a security release, read the security wiki page.")
    click.echo("2. Check for any release blockers before proceeding.")
    click.echo("    https://github.com/element-hq/synapse/labels/X-Release-Blocker")
    click.echo(
        "3. Check for any other special release notes, including announcements to add to the changelog or special deployment instructions."
    )
    click.echo("    See the 'Synapse Maintainer Report'.")

    click.confirm("Ready?", abort=True)

    click.echo("\n*** prepare ***")
    _prepare()

    click.echo("Deploy to matrix.org and ensure that it hasn't fallen over.")
    click.echo("Remember to silence the alerts to prevent alert spam.")
    click.confirm("Deployed?", abort=True)

    click.echo("\n*** tag ***")
    _tag(gh_token)

    click.echo("\n*** wait for actions ***")
    _wait_for_actions(gh_token)

    click.echo("\n*** publish ***")
    _publish(gh_token)

    click.echo("\n*** upload ***")
    _upload(gh_token)

    click.echo("\n*** merge back ***")
    _merge_back()

    click.echo("\nUpdate the Debian repository")
    click.confirm("Started updating Debian repository?", abort=True)

    click.echo("\nWait for all release methods to be ready.")
    # Docker should be ready because it was done by the workflows earlier
    # PyPI should be ready because we just ran upload().
    # TODO Automatically poll until the Debs have made it to packages.matrix.org
    click.confirm("Debs ready?", abort=True)

    click.echo("\n*** announce ***")
    _announce()


def get_package_version() -> version.Version:
    version_string = subprocess.check_output(["poetry", "version", "--short"]).decode(
        "utf-8"
    )
    return version.Version(version_string)


def get_release_branch_name(version_number: version.Version) -> str:
    return f"release-v{version_number.major}.{version_number.minor}"


def get_repo_and_check_clean_checkout(
    path: str = ".", name: str = "synapse"
) -> git.Repo:
    """Get the project repo and check it's not got any uncommitted changes."""
    try:
        repo = git.Repo(path=path)
    except git.InvalidGitRepositoryError:
        raise click.ClickException(
            f"{path} is not a git repository (expecting a {name} repository)."
        )
    if repo.is_dirty():
        raise click.ClickException(f"Uncommitted changes exist in {path}.")
    return repo


def check_valid_gh_token(gh_token: Optional[str]) -> None:
    """Check that a github token is valid, if supplied"""

    if not gh_token:
        # No github token supplied, so nothing to do.
        return

    try:
        gh = Github(gh_token)

        # We need to lookup name to trigger a request.
        _name = gh.get_user().name
    except BadCredentialsException as e:
        raise click.ClickException(f"Github credentials are bad: {e}")


def find_ref(repo: git.Repo, ref_name: str) -> Optional[git.HEAD]:
    """Find the branch/ref, looking first locally then in the remote."""
    if ref_name in repo.references:
        return repo.references[ref_name]
    elif ref_name in repo.remote().refs:
        return repo.remote().refs[ref_name]
    else:
        return None


def update_branch(repo: git.Repo) -> None:
    """Ensure branch is up to date if it has a remote"""
    tracking_branch = repo.active_branch.tracking_branch()
    if tracking_branch:
        repo.git.merge(tracking_branch.name)


def get_changes_for_version(wanted_version: version.Version) -> str:
    """Get the changelogs for the given version.

    If an RC then will only get the changelog for that RC version, otherwise if
    its a full release will get the changelog for the release and all its RCs.
    """

    with open("CHANGES.md") as f:
        changes = f.read()

    # First we parse the changelog so that we can split it into sections based
    # on the release headings.
    ast = commonmark.Parser().parse(changes)

    @attr.s(auto_attribs=True)
    class VersionSection:
        title: str

        # These are 0-based.
        start_line: int
        end_line: Optional[int] = None  # Is none if its the last entry

    headings: List[VersionSection] = []
    for node, _ in ast.walker():
        # We look for all text nodes that are in a level 1 heading.
        if node.t != "text":
            continue

        if node.parent.t != "heading" or node.parent.level != 1:
            continue

        # If we have a previous heading then we update its `end_line`.
        if headings:
            headings[-1].end_line = node.parent.sourcepos[0][0] - 1

        headings.append(VersionSection(node.literal, node.parent.sourcepos[0][0] - 1))

    changes_by_line = changes.split("\n")

    version_changelog = []  # The lines we want to include in the changelog

    # Go through each section and find any that match the requested version.
    regex = re.compile(r"^Synapse v?(\S+)")
    for section in headings:
        groups = regex.match(section.title)
        if not groups:
            continue

        heading_version = version.parse(groups.group(1))
        heading_base_version = version.parse(heading_version.base_version)

        # Check if heading version matches the requested version, or if its an
        # RC of the requested version.
        if wanted_version not in (heading_version, heading_base_version):
            continue

        version_changelog.extend(changes_by_line[section.start_line : section.end_line])

    return "\n".join(version_changelog)


def generate_and_write_changelog(
    repo: Repo, current_version: version.Version, new_version: str
) -> None:
    # We do this by getting a draft so that we can edit it before writing to the
    # changelog.
    result = run_until_successful(
        f"python3 -m towncrier build --draft --version {new_version}",
        shell=True,
        capture_output=True,
    )
    new_changes = result.stdout.decode("utf-8")
    new_changes = new_changes.replace(
        "No significant changes.", f"No significant changes since {current_version}."
    )
    new_changes += build_dependabot_changelog(
        repo,
        current_version,
    )

    # Prepend changes to changelog
    with open("CHANGES.md", "r+") as f:
        existing_content = f.read()
        f.seek(0, 0)
        f.write(new_changes)
        f.write("\n")
        f.write(existing_content)

    # Remove all the news fragments
    for filename in glob.iglob("changelog.d/*.*"):
        os.remove(filename)


def build_dependabot_changelog(repo: Repo, current_version: version.Version) -> str:
    """Summarise dependabot commits between `current_version` and `release_branch`.

    Returns an empty string if there have been no such commits; otherwise outputs a
    third-level markdown header followed by an unordered list."""
    last_release_commit = repo.tag("v" + str(current_version)).commit
    rev_spec = f"{last_release_commit.hexsha}.."
    commits = list(git.objects.Commit.iter_items(repo, rev_spec))
    messages = []
    for commit in reversed(commits):
        if commit.author.name == "dependabot[bot]":
            message: Union[str, bytes] = commit.message
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            messages.append(message.split("\n", maxsplit=1)[0])

    if not messages:
        print(f"No dependabot commits in range {rev_spec}", file=sys.stderr)
        return ""

    messages.sort()

    def replacer(match: Match[str]) -> str:
        desc = match.group(1)
        number = match.group(2)
        return f"* {desc}. ([\\#{number}](https://github.com/element-hq/synapse/issues/{number}))"

    for i, message in enumerate(messages):
        messages[i] = re.sub(r"(.*) \(#(\d+)\)$", replacer, message)
    messages.insert(0, "### Updates to locked dependencies\n")
    # Add an extra blank line to the bottom of the section
    messages.append("")
    return "\n".join(messages)


@cli.command()
@click.argument("since")
def test_dependabot_changelog(since: str) -> None:
    """Test building the dependabot changelog.

    Summarises all dependabot commits between the SINCE tag and the current git HEAD."""
    print(build_dependabot_changelog(git.Repo("."), version.Version(since)))


if __name__ == "__main__":
    cli()
