#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019-2021 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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

import logging
import os
import urllib
from abc import ABC, abstractmethod
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Awaitable,
    BinaryIO,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
    Type,
)

import attr
from zope.interface import implementer

from twisted.internet import interfaces
from twisted.internet.defer import Deferred
from twisted.internet.interfaces import IConsumer
from twisted.python.failure import Failure
from twisted.web.server import Request

from synapse.api.errors import Codes, cs_error
from synapse.http.server import finish_request, respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import (
    defer_to_threadpool,
    make_deferred_yieldable,
    run_in_background,
)
from synapse.util import Clock
from synapse.util.async_helpers import DeferredEvent
from synapse.util.stringutils import is_ascii

if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.storage.databases.main.media_repository import LocalMedia


logger = logging.getLogger(__name__)

# list all text content types that will have the charset default to UTF-8 when
# none is given
TEXT_CONTENT_TYPES = [
    "text/css",
    "text/csv",
    "text/html",
    "text/calendar",
    "text/plain",
    "text/javascript",
    "application/json",
    "application/ld+json",
    "application/rtf",
    "image/svg+xml",
    "text/xml",
]

# A list of all content types that are "safe" to be rendered inline in a browser.
INLINE_CONTENT_TYPES = [
    "text/css",
    "text/plain",
    "text/csv",
    "application/json",
    "application/ld+json",
    # We allow some media files deemed as safe, which comes from the matrix-react-sdk.
    # https://github.com/matrix-org/matrix-react-sdk/blob/a70fcfd0bcf7f8c85986da18001ea11597989a7c/src/utils/blobs.ts#L51
    # SVGs are *intentionally* omitted.
    "image/jpeg",
    "image/gif",
    "image/png",
    "image/apng",
    "image/webp",
    "image/avif",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "video/quicktime",
    "audio/mp4",
    "audio/webm",
    "audio/aac",
    "audio/mpeg",
    "audio/ogg",
    "audio/wave",
    "audio/wav",
    "audio/x-wav",
    "audio/x-pn-wav",
    "audio/flac",
    "audio/x-flac",
]

# Default timeout_ms for download and thumbnail requests
DEFAULT_MAX_TIMEOUT_MS = 20_000

# Maximum allowed timeout_ms for download and thumbnail requests
MAXIMUM_ALLOWED_MAX_TIMEOUT_MS = 60_000


def respond_404(request: SynapseRequest) -> None:
    assert request.path is not None
    respond_with_json(
        request,
        404,
        cs_error("Not found '%s'" % (request.path.decode(),), code=Codes.NOT_FOUND),
        send_cors=True,
    )


async def respond_with_file(
    hs: "HomeServer",
    request: SynapseRequest,
    media_type: str,
    file_path: str,
    file_size: Optional[int] = None,
    upload_name: Optional[str] = None,
) -> None:
    logger.debug("Responding with %r", file_path)

    if os.path.isfile(file_path):
        if file_size is None:
            stat = os.stat(file_path)
            file_size = stat.st_size

        add_file_headers(request, media_type, file_size, upload_name)

        with open(file_path, "rb") as f:
            await ThreadedFileSender(hs).beginFileTransfer(f, request)

        finish_request(request)
    else:
        respond_404(request)


def add_file_headers(
    request: Request,
    media_type: str,
    file_size: Optional[int],
    upload_name: Optional[str],
) -> None:
    """Adds the correct response headers in preparation for responding with the
    media.

    Args:
        request
        media_type: The media/content type.
        file_size: Size in bytes of the media, if known.
        upload_name: The name of the requested file, if any.
    """

    def _quote(x: str) -> str:
        return urllib.parse.quote(x.encode("utf-8"))

    # Default to a UTF-8 charset for text content types.
    # ex, uses UTF-8 for 'text/css' but not 'text/css; charset=UTF-16'
    if media_type.lower() in TEXT_CONTENT_TYPES:
        content_type = media_type + "; charset=UTF-8"
    else:
        content_type = media_type

    request.setHeader(b"Content-Type", content_type.encode("UTF-8"))

    # A strict subset of content types is allowed to be inlined  so that they may
    # be viewed directly in a browser. Other file types are forced to be downloads.
    #
    # Only the type & subtype are important, parameters can be ignored.
    if media_type.lower().split(";", 1)[0] in INLINE_CONTENT_TYPES:
        disposition = "inline"
    else:
        disposition = "attachment"

    if upload_name:
        # RFC6266 section 4.1 [1] defines both `filename` and `filename*`.
        #
        # `filename` is defined to be a `value`, which is defined by RFC2616
        # section 3.6 [2] to be a `token` or a `quoted-string`, where a `token`
        # is (essentially) a single US-ASCII word, and a `quoted-string` is a
        # US-ASCII string surrounded by double-quotes, using backslash as an
        # escape character. Note that %-encoding is *not* permitted.
        #
        # `filename*` is defined to be an `ext-value`, which is defined in
        # RFC5987 section 3.2.1 [3] to be `charset "'" [ language ] "'" value-chars`,
        # where `value-chars` is essentially a %-encoded string in the given charset.
        #
        # [1]: https://tools.ietf.org/html/rfc6266#section-4.1
        # [2]: https://tools.ietf.org/html/rfc2616#section-3.6
        # [3]: https://tools.ietf.org/html/rfc5987#section-3.2.1

        # We avoid the quoted-string version of `filename`, because (a) synapse didn't
        # correctly interpret those as of 0.99.2 and (b) they are a bit of a pain and we
        # may as well just do the filename* version.
        if _can_encode_filename_as_token(upload_name):
            disposition = "%s; filename=%s" % (
                disposition,
                upload_name,
            )
        else:
            disposition = "%s; filename*=utf-8''%s" % (
                disposition,
                _quote(upload_name),
            )

    request.setHeader(b"Content-Disposition", disposition.encode("ascii"))

    # cache for at least a day.
    # XXX: we might want to turn this off for data we don't want to
    # recommend caching as it's sensitive or private - or at least
    # select private. don't bother setting Expires as all our
    # clients are smart enough to be happy with Cache-Control
    request.setHeader(b"Cache-Control", b"public,max-age=86400,s-maxage=86400")

    if file_size is not None:
        request.setHeader(b"Content-Length", b"%d" % (file_size,))

    # Tell web crawlers to not index, archive, or follow links in media. This
    # should help to prevent things in the media repo from showing up in web
    # search results.
    request.setHeader(b"X-Robots-Tag", "noindex, nofollow, noarchive, noimageindex")


# separators as defined in RFC2616. SP and HT are handled separately.
# see _can_encode_filename_as_token.
_FILENAME_SEPARATOR_CHARS = {
    "(",
    ")",
    "<",
    ">",
    "@",
    ",",
    ";",
    ":",
    "\\",
    '"',
    "/",
    "[",
    "]",
    "?",
    "=",
    "{",
    "}",
}


def _can_encode_filename_as_token(x: str) -> bool:
    for c in x:
        # from RFC2616:
        #
        #        token          = 1*<any CHAR except CTLs or separators>
        #
        #        separators     = "(" | ")" | "<" | ">" | "@"
        #                       | "," | ";" | ":" | "\" | <">
        #                       | "/" | "[" | "]" | "?" | "="
        #                       | "{" | "}" | SP | HT
        #
        #        CHAR           = <any US-ASCII character (octets 0 - 127)>
        #
        #        CTL            = <any US-ASCII control character
        #                         (octets 0 - 31) and DEL (127)>
        #
        if ord(c) >= 127 or ord(c) <= 32 or c in _FILENAME_SEPARATOR_CHARS:
            return False
    return True


async def respond_with_multipart_responder(
    clock: Clock,
    request: SynapseRequest,
    responder: "Optional[Responder]",
    media_info: "LocalMedia",
) -> None:
    """
    Responds to requests originating from the federation media `/download` endpoint by
    streaming a multipart/mixed response

    Args:
        clock:
        request: the federation request to respond to
        responder: the responder which will send the response
        media_info: metadata about the media item
    """
    if not responder:
        respond_404(request)
        return

    # If we have a responder we *must* use it as a context manager.
    with responder:
        if request._disconnected:
            logger.warning(
                "Not sending response to request %s, already disconnected.", request
            )
            return

        if media_info.media_type.lower().split(";", 1)[0] in INLINE_CONTENT_TYPES:
            disposition = "inline"
        else:
            disposition = "attachment"

        def _quote(x: str) -> str:
            return urllib.parse.quote(x.encode("utf-8"))

        if media_info.upload_name:
            if _can_encode_filename_as_token(media_info.upload_name):
                disposition = "%s; filename=%s" % (
                    disposition,
                    media_info.upload_name,
                )
            else:
                disposition = "%s; filename*=utf-8''%s" % (
                    disposition,
                    _quote(media_info.upload_name),
                )

        from synapse.media.media_storage import MultipartFileConsumer

        # note that currently the json_object is just {}, this will change when linked media
        # is implemented
        multipart_consumer = MultipartFileConsumer(
            clock,
            request,
            media_info.media_type,
            {},
            disposition,
            media_info.media_length,
        )

        logger.debug("Responding to media request with responder %s", responder)
        if media_info.media_length is not None:
            content_length = multipart_consumer.content_length()
            assert content_length is not None
            request.setHeader(b"Content-Length", b"%d" % (content_length,))

        request.setHeader(
            b"Content-Type",
            b"multipart/mixed; boundary=%s" % multipart_consumer.boundary,
        )

        try:
            await responder.write_to_consumer(multipart_consumer)
        except Exception as e:
            # The majority of the time this will be due to the client having gone
            # away. Unfortunately, Twisted simply throws a generic exception at us
            # in that case.
            logger.warning("Failed to write to consumer: %s %s", type(e), e)

            # Unregister the producer, if it has one, so Twisted doesn't complain
            if request.producer:
                request.unregisterProducer()

    finish_request(request)


async def respond_with_responder(
    request: SynapseRequest,
    responder: "Optional[Responder]",
    media_type: str,
    file_size: Optional[int],
    upload_name: Optional[str] = None,
) -> None:
    """Responds to the request with given responder. If responder is None then
    returns 404.

    Args:
        request
        responder
        media_type: The media/content type.
        file_size: Size in bytes of the media. If not known it should be None
        upload_name: The name of the requested file, if any.
    """
    if not responder:
        respond_404(request)
        return

    # If we have a responder we *must* use it as a context manager.
    with responder:
        if request._disconnected:
            logger.warning(
                "Not sending response to request %s, already disconnected.", request
            )
            return

        logger.debug("Responding to media request with responder %s", responder)
        add_file_headers(request, media_type, file_size, upload_name)
        try:
            await responder.write_to_consumer(request)
        except Exception as e:
            # The majority of the time this will be due to the client having gone
            # away. Unfortunately, Twisted simply throws a generic exception at us
            # in that case.
            logger.warning("Failed to write to consumer: %s %s", type(e), e)

            # Unregister the producer, if it has one, so Twisted doesn't complain
            if request.producer:
                request.unregisterProducer()

    finish_request(request)


class Responder(ABC):
    """Represents a response that can be streamed to the requester.

    Responder is a context manager which *must* be used, so that any resources
    held can be cleaned up.
    """

    @abstractmethod
    def write_to_consumer(self, consumer: IConsumer) -> Awaitable:
        """Stream response into consumer

        Args:
            consumer: The consumer to stream into.

        Returns:
            Resolves once the response has finished being written
        """
        raise NotImplementedError()

    def __enter__(self) -> None:  # noqa: B027
        pass

    def __exit__(  # noqa: B027
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        pass


@attr.s(slots=True, frozen=True, auto_attribs=True)
class ThumbnailInfo:
    """Details about a generated thumbnail."""

    width: int
    height: int
    method: str
    # Content type of thumbnail, e.g. image/png
    type: str
    # The size of the media file, in bytes.
    length: int


@attr.s(slots=True, frozen=True, auto_attribs=True)
class FileInfo:
    """Details about a requested/uploaded file."""

    # The server name where the media originated from, or None if local.
    server_name: Optional[str]
    # The local ID of the file. For local files this is the same as the media_id
    file_id: str
    # If the file is for the url preview cache
    url_cache: bool = False
    # Whether the file is a thumbnail or not.
    thumbnail: Optional[ThumbnailInfo] = None

    # The below properties exist to maintain compatibility with third-party modules.
    @property
    def thumbnail_width(self) -> Optional[int]:
        if not self.thumbnail:
            return None
        return self.thumbnail.width

    @property
    def thumbnail_height(self) -> Optional[int]:
        if not self.thumbnail:
            return None
        return self.thumbnail.height

    @property
    def thumbnail_method(self) -> Optional[str]:
        if not self.thumbnail:
            return None
        return self.thumbnail.method

    @property
    def thumbnail_type(self) -> Optional[str]:
        if not self.thumbnail:
            return None
        return self.thumbnail.type

    @property
    def thumbnail_length(self) -> Optional[int]:
        if not self.thumbnail:
            return None
        return self.thumbnail.length


def get_filename_from_headers(headers: Dict[bytes, List[bytes]]) -> Optional[str]:
    """
    Get the filename of the downloaded file by inspecting the
    Content-Disposition HTTP header.

    Args:
        headers: The HTTP request headers.

    Returns:
        The filename, or None.
    """
    content_disposition = headers.get(b"Content-Disposition", [b""])

    # No header, bail out.
    if not content_disposition[0]:
        return None

    _, params = _parse_header(content_disposition[0])

    upload_name = None

    # First check if there is a valid UTF-8 filename
    upload_name_utf8 = params.get(b"filename*", None)
    if upload_name_utf8:
        if upload_name_utf8.lower().startswith(b"utf-8''"):
            upload_name_utf8 = upload_name_utf8[7:]
            # We have a filename*= section. This MUST be ASCII, and any UTF-8
            # bytes are %-quoted.
            try:
                # Once it is decoded, we can then unquote the %-encoded
                # parts strictly into a unicode string.
                upload_name = urllib.parse.unquote(
                    upload_name_utf8.decode("ascii"), errors="strict"
                )
            except UnicodeDecodeError:
                # Incorrect UTF-8.
                pass

    # If there isn't check for an ascii name.
    if not upload_name:
        upload_name_ascii = params.get(b"filename", None)
        if upload_name_ascii and is_ascii(upload_name_ascii):
            upload_name = upload_name_ascii.decode("ascii")

    # This may be None here, indicating we did not find a matching name.
    return upload_name


def _parse_header(line: bytes) -> Tuple[bytes, Dict[bytes, bytes]]:
    """Parse a Content-type like header.

    Cargo-culted from `cgi`, but works on bytes rather than strings.

    Args:
        line: header to be parsed

    Returns:
        The main content-type, followed by the parameter dictionary
    """
    parts = _parseparam(b";" + line)
    key = next(parts)
    pdict = {}
    for p in parts:
        i = p.find(b"=")
        if i >= 0:
            name = p[:i].strip().lower()
            value = p[i + 1 :].strip()

            # strip double-quotes
            if len(value) >= 2 and value[0:1] == value[-1:] == b'"':
                value = value[1:-1]
                value = value.replace(b"\\\\", b"\\").replace(b'\\"', b'"')
            pdict[name] = value

    return key, pdict


def _parseparam(s: bytes) -> Generator[bytes, None, None]:
    """Generator which splits the input on ;, respecting double-quoted sequences

    Cargo-culted from `cgi`, but works on bytes rather than strings.

    Args:
        s: header to be parsed

    Returns:
        The split input
    """
    while s[:1] == b";":
        s = s[1:]

        # look for the next ;
        end = s.find(b";")

        # if there is an odd number of " marks between here and the next ;, skip to the
        # next ; instead
        while end > 0 and (s.count(b'"', 0, end) - s.count(b'\\"', 0, end)) % 2:
            end = s.find(b";", end + 1)

        if end < 0:
            end = len(s)
        f = s[:end]
        yield f.strip()
        s = s[end:]


@implementer(interfaces.IPushProducer)
class ThreadedFileSender:
    """
    A producer that sends the contents of a file to a consumer, reading from the
    file on a thread.

    This works by having a loop in a threadpool repeatedly reading from the
    file, until the consumer pauses the producer. There is then a loop in the
    main thread that waits until the consumer resumes the producer and then
    starts reading in the threadpool again.

    This is done to ensure that we're never waiting in the threadpool, as
    otherwise its easy to starve it of threads.
    """

    # How much data to read in one go.
    CHUNK_SIZE = 2**14

    # How long we wait for the consumer to be ready again before aborting the
    # read.
    TIMEOUT_SECONDS = 90.0

    def __init__(self, hs: "HomeServer") -> None:
        self.reactor = hs.get_reactor()
        self.thread_pool = hs.get_media_sender_thread_pool()

        self.file: Optional[BinaryIO] = None
        self.deferred: "Deferred[None]" = Deferred()
        self.consumer: Optional[interfaces.IConsumer] = None

        # Signals if the thread should keep reading/sending data. Set means
        # continue, clear means pause.
        self.wakeup_event = DeferredEvent(self.reactor)

        # Signals if the thread should terminate, e.g. because the consumer has
        # gone away.
        self.stop_writing = False

    def beginFileTransfer(
        self, file: BinaryIO, consumer: interfaces.IConsumer
    ) -> "Deferred[None]":
        """
        Begin transferring a file
        """
        self.file = file
        self.consumer = consumer

        self.consumer.registerProducer(self, True)

        # We set the wakeup signal as we should start producing immediately.
        self.wakeup_event.set()
        run_in_background(self.start_read_loop)

        return make_deferred_yieldable(self.deferred)

    def resumeProducing(self) -> None:
        """interfaces.IPushProducer"""
        self.wakeup_event.set()

    def pauseProducing(self) -> None:
        """interfaces.IPushProducer"""
        self.wakeup_event.clear()

    def stopProducing(self) -> None:
        """interfaces.IPushProducer"""

        # Unregister the consumer so we don't try and interact with it again.
        if self.consumer:
            self.consumer.unregisterProducer()

        self.consumer = None

        # Terminate the loop.
        self.stop_writing = True
        self.wakeup_event.set()

        if not self.deferred.called:
            self.deferred.errback(Exception("Consumer asked us to stop producing"))

    async def start_read_loop(self) -> None:
        """This is the loop that drives reading/writing"""
        try:
            while not self.stop_writing:
                # Start the loop in the threadpool to read data.
                more_data = await defer_to_threadpool(
                    self.reactor, self.thread_pool, self._on_thread_read_loop
                )
                if not more_data:
                    # Reached EOF, we can just return.
                    return

                if not self.wakeup_event.is_set():
                    ret = await self.wakeup_event.wait(self.TIMEOUT_SECONDS)
                    if not ret:
                        raise Exception("Timed out waiting to resume")
        except Exception:
            self._error(Failure())
        finally:
            self._finish()

    def _on_thread_read_loop(self) -> bool:
        """This is the loop that happens on a thread.

        Returns:
            Whether there is more data to send.
        """

        while not self.stop_writing and self.wakeup_event.is_set():
            # The file should always have been set before we get here.
            assert self.file is not None

            chunk = self.file.read(self.CHUNK_SIZE)
            if not chunk:
                return False

            self.reactor.callFromThread(self._write, chunk)

        return True

    def _write(self, chunk: bytes) -> None:
        """Called from the thread to write a chunk of data"""
        if self.consumer:
            self.consumer.write(chunk)

    def _error(self, failure: Failure) -> None:
        """Called when there was a fatal error"""
        if self.consumer:
            self.consumer.unregisterProducer()
            self.consumer = None

        if not self.deferred.called:
            self.deferred.errback(failure)

    def _finish(self) -> None:
        """Called when we have finished writing (either on success or
        failure)."""
        if self.file:
            self.file.close()
            self.file = None

        if self.consumer:
            self.consumer.unregisterProducer()
            self.consumer = None

        if not self.deferred.called:
            self.deferred.callback(None)
