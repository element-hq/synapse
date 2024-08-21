#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2018-2021 The Matrix.org Foundation C.I.C.
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

import abc
import logging
import os
import shutil
from typing import TYPE_CHECKING, Callable, Optional

from synapse.config._base import Config
from synapse.logging.context import defer_to_thread, run_in_background
from synapse.logging.opentracing import start_active_span, trace_with_opname
from synapse.util.async_helpers import maybe_awaitable

from ._base import FileInfo, Responder
from .media_storage import FileResponder

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from synapse.server import HomeServer


class StorageProvider(metaclass=abc.ABCMeta):
    """A storage provider is a service that can store uploaded media and
    retrieve them.
    """

    @abc.abstractmethod
    async def store_file(self, path: str, file_info: FileInfo) -> None:
        """Store the file described by file_info. The actual contents can be
        retrieved by reading the file in file_info.upload_path.

        Args:
            path: Relative path of file in local cache
            file_info: The metadata of the file.
        """

    @abc.abstractmethod
    async def fetch(self, path: str, file_info: FileInfo) -> Optional[Responder]:
        """Attempt to fetch the file described by file_info and stream it
        into writer.

        Args:
            path: Relative path of file in local cache
            file_info: The metadata of the file.

        Returns:
            Returns a Responder if the provider has the file, otherwise returns None.
        """


class StorageProviderWrapper(StorageProvider):
    """Wraps a storage provider and provides various config options

    Args:
        backend: The storage provider to wrap.
        store_local: Whether to store new local files or not.
        store_synchronous: Whether to wait for file to be successfully
            uploaded, or todo the upload in the background.
        store_remote: Whether remote media should be uploaded
    """

    def __init__(
        self,
        backend: StorageProvider,
        store_local: bool,
        store_synchronous: bool,
        store_remote: bool,
    ):
        self.backend = backend
        self.store_local = store_local
        self.store_synchronous = store_synchronous
        self.store_remote = store_remote

    def __str__(self) -> str:
        return "StorageProviderWrapper[%s]" % (self.backend,)

    @trace_with_opname("StorageProviderWrapper.store_file")
    async def store_file(self, path: str, file_info: FileInfo) -> None:
        if not file_info.server_name and not self.store_local:
            return None

        if file_info.server_name and not self.store_remote:
            return None

        if file_info.url_cache:
            # The URL preview cache is short lived and not worth offloading or
            # backing up.
            return None

        if self.store_synchronous:
            # store_file is supposed to return an Awaitable, but guard
            # against improper implementations.
            await maybe_awaitable(self.backend.store_file(path, file_info))  # type: ignore
        else:
            # TODO: Handle errors.
            async def store() -> None:
                try:
                    return await maybe_awaitable(
                        self.backend.store_file(path, file_info)
                    )
                except Exception:
                    logger.exception("Error storing file")

            run_in_background(store)

    @trace_with_opname("StorageProviderWrapper.fetch")
    async def fetch(self, path: str, file_info: FileInfo) -> Optional[Responder]:
        if file_info.url_cache:
            # Files in the URL preview cache definitely aren't stored here,
            # so avoid any potentially slow I/O or network access.
            return None

        # store_file is supposed to return an Awaitable, but guard
        # against improper implementations.
        return await maybe_awaitable(self.backend.fetch(path, file_info))


class FileStorageProviderBackend(StorageProvider):
    """A storage provider that stores files in a directory on a filesystem.

    Args:
        hs
        config: The config returned by `parse_config`.
    """

    def __init__(self, hs: "HomeServer", config: str):
        self.hs = hs
        self.reactor = hs.get_reactor()
        self.cache_directory = hs.config.media.media_store_path
        self.base_directory = config

    def __str__(self) -> str:
        return "FileStorageProviderBackend[%s]" % (self.base_directory,)

    @trace_with_opname("FileStorageProviderBackend.store_file")
    async def store_file(self, path: str, file_info: FileInfo) -> None:
        """See StorageProvider.store_file"""

        primary_fname = os.path.join(self.cache_directory, path)
        backup_fname = os.path.join(self.base_directory, path)

        dirname = os.path.dirname(backup_fname)
        os.makedirs(dirname, exist_ok=True)

        # mypy needs help inferring the type of the second parameter, which is generic
        shutil_copyfile: Callable[[str, str], str] = shutil.copyfile
        with start_active_span("shutil_copyfile"):
            await defer_to_thread(
                self.reactor,
                shutil_copyfile,
                primary_fname,
                backup_fname,
            )

    @trace_with_opname("FileStorageProviderBackend.fetch")
    async def fetch(self, path: str, file_info: FileInfo) -> Optional[Responder]:
        """See StorageProvider.fetch"""

        backup_fname = os.path.join(self.base_directory, path)
        if os.path.isfile(backup_fname):
            return FileResponder(self.hs, open(backup_fname, "rb"))

        return None

    @staticmethod
    def parse_config(config: dict) -> str:
        """Called on startup to parse config supplied. This should parse
        the config and raise if there is a problem.

        The returned value is passed into the constructor.

        In this case we only care about a single param, the directory, so let's
        just pull that out.
        """
        return Config.ensure_directory(config["directory"])
