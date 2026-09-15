#!/bin/bash
#
# Fetches a version of complement which best matches the current build.
#
# The tarball is unpacked into `./complement`.

set -e
mkdir -p complement

# Pick an appropriate version of complement. Depending on whether this is a PR or release,
# etc. we need to use different fallbacks:
#
# 1. First check if there's a similarly named branch (GITHUB_HEAD_REF
#    for pull requests, otherwise GITHUB_REF).
# 2. Attempt to use the base branch, e.g. when merging into release-vX.Y
#    (GITHUB_BASE_REF for pull requests).
# 3. Use the default complement branch ("HEAD").
for BRANCH_NAME in "$GITHUB_HEAD_REF" "$GITHUB_BASE_REF" "${GITHUB_REF#refs/heads/}" "HEAD"; do
  # Skip empty branch names and merge commits.
  if [[ -z "$BRANCH_NAME" || $BRANCH_NAME =~ ^refs/pull/.* ]]; then
    continue
  fi

  # TEMPORARY, DO NOT MERGE: try the barodeur/complement fork first so this PR
  # can be tested against the matching Complement branch before it is merged
  # into matrix-org/complement. Revert this commit once that has happened.
  for REPO in barodeur/complement matrix-org/complement; do
    (wget -O - "https://github.com/$REPO/archive/$BRANCH_NAME.tar.gz" | tar -xz --strip-components=1 -C complement) && break 2
  done
done
