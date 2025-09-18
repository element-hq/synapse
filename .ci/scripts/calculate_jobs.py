#!/usr/bin/env python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

# Calculate the trial jobs to run based on if we're in a PR or not.

import json
import os


def set_output(key: str, value: str):
    # See https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions#setting-an-output-parameter
    with open(os.environ["GITHUB_OUTPUT"], "at") as f:
        print(f"{key}={value}", file=f)


IS_PR = os.environ["GITHUB_REF"].startswith("refs/pull/")

# First calculate the various trial jobs.
#
# For PRs, we only run each type of test with the oldest Python version supported (which
# is Python 3.9 right now)

trial_sqlite_tests = [
    {
        "python-version": "3.9",
        "database": "sqlite",
        "extras": "all",
    }
]

if not IS_PR:
    trial_sqlite_tests.extend(
        {
            "python-version": version,
            "database": "sqlite",
            "extras": "all",
        }
        for version in ("3.10", "3.11", "3.12", "3.13")
    )

trial_postgres_tests = [
    {
        "python-version": "3.9",
        "database": "postgres",
        "postgres-version": "13",
        "extras": "all",
    }
]

if not IS_PR:
    trial_postgres_tests.append(
        {
            "python-version": "3.13",
            "database": "postgres",
            "postgres-version": "17",
            "extras": "all",
        }
    )

trial_no_extra_tests = [
    {
        "python-version": "3.9",
        "database": "sqlite",
        "extras": "",
    }
]

print("::group::Calculated trial jobs")
print(
    json.dumps(
        trial_sqlite_tests + trial_postgres_tests + trial_no_extra_tests, indent=4
    )
)
print("::endgroup::")

test_matrix = json.dumps(
    trial_sqlite_tests + trial_postgres_tests + trial_no_extra_tests
)
set_output("trial_test_matrix", test_matrix)


# First calculate the various sytest jobs.
#
# For each type of test we only run on bullseye on PRs


sytest_tests = [
    {
        "sytest-tag": "bullseye",
    },
    {
        "sytest-tag": "bullseye",
        "postgres": "postgres",
    },
    {
        "sytest-tag": "bullseye",
        "postgres": "multi-postgres",
        "workers": "workers",
    },
    {
        "sytest-tag": "bullseye",
        "postgres": "multi-postgres",
        "workers": "workers",
        "reactor": "asyncio",
    },
]

if not IS_PR:
    sytest_tests.extend(
        [
            {
                "sytest-tag": "bullseye",
                "reactor": "asyncio",
            },
            {
                "sytest-tag": "bullseye",
                "postgres": "postgres",
                "reactor": "asyncio",
            },
            {
                "sytest-tag": "testing",
                "postgres": "postgres",
            },
        ]
    )


print("::group::Calculated sytest jobs")
print(json.dumps(sytest_tests, indent=4))
print("::endgroup::")

test_matrix = json.dumps(sytest_tests)
set_output("sytest_test_matrix", test_matrix)
