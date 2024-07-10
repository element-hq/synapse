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
import contextlib
import json
import logging
import os
import shutil
from contextlib import closing
from io import BytesIO
from types import TracebackType
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    BinaryIO,
    Callable,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)
from uuid import uuid4

import attr
from zope.interface import implementer

from twisted.internet import interfaces
from twisted.internet.defer import Deferred
from twisted.internet.interfaces import IConsumer
from twisted.protocols.basic import FileSender

from synapse.api.errors import NotFoundError
from synapse.logging.context import (
    defer_to_thread,
    make_deferred_yieldable,
    run_in_background,
)
from synapse.logging.opentracing import start_active_span, trace, trace_with_opname
from synapse.util import Clock
from synapse.util.file_consumer import BackgroundFileConsumer

from ..types import JsonDict
from ._base import FileInfo, Responder
from .filepath import MediaFilePaths

if TYPE_CHECKING:
    from synapse.media.storage_provider import StorageProvider
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

CRLF = b"\r\n"


class MediaStorage:
    """Responsible for storing/fetching files from local sources.

    Args:
        hs
        local_media_directory: Base path where we store media on disk
        filepaths
        storage_providers: List of StorageProvider that are used to fetch and store files.
    """

    def __init__(
        self,
        hs: "HomeServer",
        local_media_directory: str,
        filepaths: MediaFilePaths,
        storage_providers: Sequence["StorageProvider"],
    ):
        self.hs = hs
        self.reactor = hs.get_reactor()
        self.local_media_directory = local_media_directory
        self.filepaths = filepaths
        self.storage_providers = storage_providers
        self._spam_checker_module_callbacks = hs.get_module_api_callbacks().spam_checker
        self.clock = hs.get_clock()

    @trace_with_opname("MediaStorage.store_file")
    async def store_file(self, source: IO, file_info: FileInfo) -> str:
        """Write `source` to the on disk media store, and also any other
        configured storage providers

        Args:
            source: A file like object that should be written
            file_info: Info about the file to store

        Returns:
            the file path written to in the primary media store
        """

        async with self.store_into_file(file_info) as (f, fname):
            # Write to the main media repository
            await self.write_to_file(source, f)

        return fname

    @trace_with_opname("MediaStorage.write_to_file")
    async def write_to_file(self, source: IO, output: IO) -> None:
        """Asynchronously write the `source` to `output`."""
        await defer_to_thread(self.reactor, _write_file_synchronously, source, output)

    @trace_with_opname("MediaStorage.store_into_file")
    @contextlib.asynccontextmanager
    async def store_into_file(
        self, file_info: FileInfo
    ) -> AsyncIterator[Tuple[BinaryIO, str]]:
        """Async Context manager used to get a file like object to write into, as
        described by file_info.

        Actually yields a 2-tuple (file, fname,), where file is a file
        like object that can be written to and fname is the absolute path of file
        on disk.

        fname can be used to read the contents from after upload, e.g. to
        generate thumbnails.

        Args:
            file_info: Info about the file to store

        Example:

            async with media_storage.store_into_file(info) as (f, fname,):
                # .. write into f ...
        """

        path = self._file_info_to_path(file_info)
        fname = os.path.join(self.local_media_directory, path)

        dirname = os.path.dirname(fname)
        os.makedirs(dirname, exist_ok=True)

        try:
            with start_active_span("writing to main media repo"):
                with open(fname, "wb") as f:
                    yield f, fname

            with start_active_span("writing to other storage providers"):
                spam_check = (
                    await self._spam_checker_module_callbacks.check_media_file_for_spam(
                        ReadableFileWrapper(self.clock, fname), file_info
                    )
                )
                if spam_check != self._spam_checker_module_callbacks.NOT_SPAM:
                    logger.info("Blocking media due to spam checker")
                    # Note that we'll delete the stored media, due to the
                    # try/except below. The media also won't be stored in
                    # the DB.
                    # We currently ignore any additional field returned by
                    # the spam-check API.
                    raise SpamMediaException(errcode=spam_check[0])

                for provider in self.storage_providers:
                    with start_active_span(str(provider)):
                        await provider.store_file(path, file_info)

        except Exception as e:
            try:
                os.remove(fname)
            except Exception:
                pass

            raise e from None

    async def fetch_media(self, file_info: FileInfo) -> Optional[Responder]:
        """Attempts to fetch media described by file_info from the local cache
        and configured storage providers.

        Args:
            file_info: Metadata about the media file

        Returns:
            Returns a Responder if the file was found, otherwise None.
        """
        paths = [self._file_info_to_path(file_info)]

        # fallback for remote thumbnails with no method in the filename
        if file_info.thumbnail and file_info.server_name:
            paths.append(
                self.filepaths.remote_media_thumbnail_rel_legacy(
                    server_name=file_info.server_name,
                    file_id=file_info.file_id,
                    width=file_info.thumbnail.width,
                    height=file_info.thumbnail.height,
                    content_type=file_info.thumbnail.type,
                )
            )

        for path in paths:
            local_path = os.path.join(self.local_media_directory, path)
            if os.path.exists(local_path):
                logger.debug("responding with local file %s", local_path)
                return FileResponder(open(local_path, "rb"))
            logger.debug("local file %s did not exist", local_path)

        for provider in self.storage_providers:
            for path in paths:
                res: Any = await provider.fetch(path, file_info)
                if res:
                    logger.debug("Streaming %s from %s", path, provider)
                    return res
                logger.debug("%s not found on %s", path, provider)

        return None

    @trace
    async def ensure_media_is_in_local_cache(self, file_info: FileInfo) -> str:
        """Ensures that the given file is in the local cache. Attempts to
        download it from storage providers if it isn't.

        Args:
            file_info

        Returns:
            Full path to local file
        """
        path = self._file_info_to_path(file_info)
        local_path = os.path.join(self.local_media_directory, path)
        if os.path.exists(local_path):
            return local_path

        # Fallback for paths without method names
        # Should be removed in the future
        if file_info.thumbnail and file_info.server_name:
            legacy_path = self.filepaths.remote_media_thumbnail_rel_legacy(
                server_name=file_info.server_name,
                file_id=file_info.file_id,
                width=file_info.thumbnail.width,
                height=file_info.thumbnail.height,
                content_type=file_info.thumbnail.type,
            )
            legacy_local_path = os.path.join(self.local_media_directory, legacy_path)
            if os.path.exists(legacy_local_path):
                return legacy_local_path

        dirname = os.path.dirname(local_path)
        os.makedirs(dirname, exist_ok=True)

        for provider in self.storage_providers:
            res: Any = await provider.fetch(path, file_info)
            if res:
                with res:
                    consumer = BackgroundFileConsumer(
                        open(local_path, "wb"), self.reactor
                    )
                    await res.write_to_consumer(consumer)
                    await consumer.wait()
                return local_path

        raise NotFoundError()

    @trace
    def _file_info_to_path(self, file_info: FileInfo) -> str:
        """Converts file_info into a relative path.

        The path is suitable for storing files under a directory, e.g. used to
        store files on local FS under the base media repository directory.
        """
        if file_info.url_cache:
            if file_info.thumbnail:
                return self.filepaths.url_cache_thumbnail_rel(
                    media_id=file_info.file_id,
                    width=file_info.thumbnail.width,
                    height=file_info.thumbnail.height,
                    content_type=file_info.thumbnail.type,
                    method=file_info.thumbnail.method,
                )
            return self.filepaths.url_cache_filepath_rel(file_info.file_id)

        if file_info.server_name:
            if file_info.thumbnail:
                return self.filepaths.remote_media_thumbnail_rel(
                    server_name=file_info.server_name,
                    file_id=file_info.file_id,
                    width=file_info.thumbnail.width,
                    height=file_info.thumbnail.height,
                    content_type=file_info.thumbnail.type,
                    method=file_info.thumbnail.method,
                )
            return self.filepaths.remote_media_filepath_rel(
                file_info.server_name, file_info.file_id
            )

        if file_info.thumbnail:
            return self.filepaths.local_media_thumbnail_rel(
                media_id=file_info.file_id,
                width=file_info.thumbnail.width,
                height=file_info.thumbnail.height,
                content_type=file_info.thumbnail.type,
                method=file_info.thumbnail.method,
            )
        return self.filepaths.local_media_filepath_rel(file_info.file_id)


@trace
def _write_file_synchronously(source: IO, dest: IO) -> None:
    """Write `source` to the file like `dest` synchronously. Should be called
    from a thread.

    Args:
        source: A file like object that's to be written
        dest: A file like object to be written to
    """
    source.seek(0)  # Ensure we read from the start of the file
    shutil.copyfileobj(source, dest)


class FileResponder(Responder):
    """Wraps an open file that can be sent to a request.

    Args:
        open_file: A file like object to be streamed to the client,
            is closed when finished streaming.
    """

    def __init__(self, open_file: IO):
        self.open_file = open_file

    def write_to_consumer(self, consumer: IConsumer) -> Deferred:
        return make_deferred_yieldable(
            FileSender().beginFileTransfer(self.open_file, consumer)
        )

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.open_file.close()


class SpamMediaException(NotFoundError):
    """The media was blocked by a spam checker, so we simply 404 the request (in
    the same way as if it was quarantined).
    """


@attr.s(slots=True, auto_attribs=True)
class ReadableFileWrapper:
    """Wrapper that allows reading a file in chunks, yielding to the reactor,
    and writing to a callback.

    This is simplified `FileSender` that takes an IO object rather than an
    `IConsumer`.
    """

    CHUNK_SIZE = 2**14

    clock: Clock
    path: str

    async def write_chunks_to(self, callback: Callable[[bytes], object]) -> None:
        """Reads the file in chunks and calls the callback with each chunk."""

        with open(self.path, "rb") as file:
            while True:
                chunk = file.read(self.CHUNK_SIZE)
                if not chunk:
                    break

                callback(chunk)

                # We yield to the reactor by sleeping for 0 seconds.
                await self.clock.sleep(0)


@implementer(interfaces.IConsumer)
@implementer(interfaces.IPushProducer)
class MultipartFileConsumer:
    """Wraps a given consumer so that any data that gets written to it gets
    converted to a multipart format.
    """

    def __init__(
        self,
        clock: Clock,
        wrapped_consumer: interfaces.IConsumer,
        file_content_type: str,
        json_object: JsonDict,
        disposition: str,
        content_length: Optional[int],
    ) -> None:
        self.clock = clock
        self.wrapped_consumer = wrapped_consumer
        self.json_field = json_object
        self.json_field_written = False
        self.file_headers_written = False
        self.file_content_type = file_content_type
        self.boundary = uuid4().hex.encode("ascii")

        # The producer that registered with us, and if it's a push or pull
        # producer.
        self.producer: Optional["interfaces.IProducer"] = None
        self.streaming: Optional[bool] = None

        # Whether the wrapped consumer has asked us to pause.
        self.paused = False

        self.length = content_length
        self.disposition = disposition

    ### IConsumer APIs ###

    def registerProducer(
        self, producer: "interfaces.IProducer", streaming: bool
    ) -> None:
        """
        Register to receive data from a producer.

        This sets self to be a consumer for a producer.  When this object runs
        out of data (as when a send(2) call on a socket succeeds in moving the
        last data from a userspace buffer into a kernelspace buffer), it will
        ask the producer to resumeProducing().

        For L{IPullProducer} providers, C{resumeProducing} will be called once
        each time data is required.

        For L{IPushProducer} providers, C{pauseProducing} will be called
        whenever the write buffer fills up and C{resumeProducing} will only be
        called when it empties.  The consumer will only call C{resumeProducing}
        to balance a previous C{pauseProducing} call; the producer is assumed
        to start in an un-paused state.

        @param streaming: C{True} if C{producer} provides L{IPushProducer},
            C{False} if C{producer} provides L{IPullProducer}.

        @raise RuntimeError: If a producer is already registered.
        """
        self.producer = producer
        self.streaming = streaming

        self.wrapped_consumer.registerProducer(self, True)

        # kick off producing if `self.producer` is not a streaming producer
        if not streaming:
            self.resumeProducing()

    def unregisterProducer(self) -> None:
        """
        Stop consuming data from a producer, without disconnecting.
        """
        self.wrapped_consumer.write(CRLF + b"--" + self.boundary + b"--" + CRLF)
        self.wrapped_consumer.unregisterProducer()
        self.paused = True

    def write(self, data: bytes) -> None:
        """
        The producer will write data by calling this method.

        The implementation must be non-blocking and perform whatever
        buffering is necessary.  If the producer has provided enough data
        for now and it is a L{IPushProducer}, the consumer may call its
        C{pauseProducing} method.
        """
        if not self.json_field_written:
            self.wrapped_consumer.write(CRLF + b"--" + self.boundary + CRLF)

            content_type = Header(b"Content-Type", b"application/json")
            self.wrapped_consumer.write(bytes(content_type) + CRLF)

            json_field = json.dumps(self.json_field)
            json_bytes = json_field.encode("utf-8")
            self.wrapped_consumer.write(CRLF + json_bytes)
            self.wrapped_consumer.write(CRLF + b"--" + self.boundary + CRLF)

            self.json_field_written = True

        # if we haven't written the content type yet, do so
        if not self.file_headers_written:
            type = self.file_content_type.encode("utf-8")
            content_type = Header(b"Content-Type", type)
            self.wrapped_consumer.write(bytes(content_type) + CRLF)
            disp_header = Header(b"Content-Disposition", self.disposition)
            self.wrapped_consumer.write(bytes(disp_header) + CRLF + CRLF)
            self.file_headers_written = True

        self.wrapped_consumer.write(data)

    ### IPushProducer APIs ###

    def stopProducing(self) -> None:
        """
        Stop producing data.

        This tells a producer that its consumer has died, so it must stop
        producing data for good.
        """
        assert self.producer is not None
        self.paused = True
        self.producer.stopProducing()

    def pauseProducing(self) -> None:
        """
        Pause producing data.

        Tells a producer that it has produced too much data to process for
        the time being, and to stop until C{resumeProducing()} is called.
        """
        assert self.producer is not None
        self.paused = True

        if self.streaming:
            cast("interfaces.IPushProducer", self.producer).pauseProducing()
        else:
            self.paused = True

    def resumeProducing(self) -> None:
        """
        Resume producing data.

        This tells a producer to re-add itself to the main loop and produce
        more data for its consumer.
        """
        assert self.producer is not None

        if self.streaming:
            cast("interfaces.IPushProducer", self.producer).resumeProducing()
        else:
            # If the producer is not a streaming producer we need to start
            # repeatedly calling  `resumeProducing` in a loop.
            run_in_background(self._resumeProducingRepeatedly)

    def content_length(self) -> Optional[int]:
        """
        Calculate the content length of the multipart response
        in bytes.
        """
        if not self.length:
            return None
        # calculate length of json field and content-type, disposition headers
        json_field = json.dumps(self.json_field)
        json_bytes = json_field.encode("utf-8")
        json_length = len(json_bytes)

        type = self.file_content_type.encode("utf-8")
        content_type = Header(b"Content-Type", type)
        type_length = len(bytes(content_type))

        disp = self.disposition.encode("utf-8")
        disp_header = Header(b"Content-Disposition", disp)
        disp_length = len(bytes(disp_header))

        # 156 is the length of the elements that aren't variable, ie
        # CRLFs and boundary strings, etc
        self.length += json_length + type_length + disp_length + 156

        return self.length

    ### Internal APIs. ###

    async def _resumeProducingRepeatedly(self) -> None:
        assert self.producer is not None
        assert not self.streaming
        producer = cast("interfaces.IPullProducer", self.producer)

        self.paused = False
        while not self.paused:
            producer.resumeProducing()
            await self.clock.sleep(0)


class Header:
    """
    `Header` This class is a tiny wrapper that produces
    request headers. We can't use standard python header
    class because it encodes unicode fields using =? bla bla ?=
    encoding, which is correct, but no one in HTTP world expects
    that, everyone wants utf-8 raw bytes. (stolen from treq.multipart)

    """

    def __init__(
        self,
        name: bytes,
        value: Any,
        params: Optional[List[Tuple[Any, Any]]] = None,
    ):
        self.name = name
        self.value = value
        self.params = params or []

    def add_param(self, name: Any, value: Any) -> None:
        self.params.append((name, value))

    def __bytes__(self) -> bytes:
        with closing(BytesIO()) as h:
            h.write(self.name + b": " + escape(self.value).encode("us-ascii"))
            if self.params:
                for name, val in self.params:
                    h.write(b"; ")
                    h.write(escape(name).encode("us-ascii"))
                    h.write(b"=")
                    h.write(b'"' + escape(val).encode("utf-8") + b'"')
            h.seek(0)
            return h.read()


def escape(value: Union[str, bytes]) -> str:
    """
    This function prevents header values from corrupting the request,
    a newline in the file name parameter makes form-data request unreadable
    for a majority of parsers. (stolen from treq.multipart)
    """
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return value.replace("\r", "").replace("\n", "").replace('"', '\\"')
