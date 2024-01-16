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

"""
Moves a list of remote media from one media store to another.

The input should be a list of media files to be moved, one per line. Each line
should be formatted::

    <origin server>|<file id>

This can be extracted from postgres with::

    psql --tuples-only -A -c "select media_origin, filesystem_id from
        matrix.remote_media_cache where ..."

To use, pipe the above into::

    PYTHON_PATH=. synapse/_scripts/move_remote_media_to_new_store.py <source repo> <dest repo>
"""

import argparse
import logging
import os
import shutil
import sys

from synapse.media.filepath import MediaFilePaths

logger = logging.getLogger()


def main(src_repo: str, dest_repo: str) -> None:
    src_paths = MediaFilePaths(src_repo)
    dest_paths = MediaFilePaths(dest_repo)
    for line in sys.stdin:
        line = line.strip()
        parts = line.split("|")
        if len(parts) != 2:
            print("Unable to parse input line %s" % line, file=sys.stderr)
            sys.exit(1)

        move_media(parts[0], parts[1], src_paths, dest_paths)


def move_media(
    origin_server: str,
    file_id: str,
    src_paths: MediaFilePaths,
    dest_paths: MediaFilePaths,
) -> None:
    """Move the given file, and any thumbnails, to the dest repo

    Args:
        origin_server:
        file_id:
        src_paths:
        dest_paths:
    """
    logger.info("%s/%s", origin_server, file_id)

    # check that the original exists
    original_file = src_paths.remote_media_filepath(origin_server, file_id)
    if not os.path.exists(original_file):
        logger.warning(
            "Original for %s/%s (%s) does not exist",
            origin_server,
            file_id,
            original_file,
        )
    else:
        mkdir_and_move(
            original_file, dest_paths.remote_media_filepath(origin_server, file_id)
        )

    # now look for thumbnails
    original_thumb_dir = src_paths.remote_media_thumbnail_dir(origin_server, file_id)
    if not os.path.exists(original_thumb_dir):
        return

    mkdir_and_move(
        original_thumb_dir,
        dest_paths.remote_media_thumbnail_dir(origin_server, file_id),
    )


def mkdir_and_move(original_file: str, dest_file: str) -> None:
    dirname = os.path.dirname(dest_file)
    if not os.path.exists(dirname):
        logger.debug("mkdir %s", dirname)
        os.makedirs(dirname)
    logger.debug("mv %s %s", original_file, dest_file)
    shutil.move(original_file, dest_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", action="store_true", help="enable debug logging")
    parser.add_argument("src_repo", help="Path to source content repo")
    parser.add_argument("dest_repo", help="Path to source content repo")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.v else logging.INFO,
        format="%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(message)s",
    )

    main(args.src_repo, args.dest_repo)
