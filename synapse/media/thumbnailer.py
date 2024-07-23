#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020-2021 The Matrix.org Foundation C.I.C.
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
from io import BytesIO
from types import TracebackType
from typing import TYPE_CHECKING, List, Optional, Tuple, Type

from PIL import Image

from synapse.api.errors import Codes, NotFoundError, SynapseError, cs_error
from synapse.config.repository import THUMBNAIL_SUPPORTED_MEDIA_FORMAT_MAP
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.opentracing import trace
from synapse.media._base import (
    FileInfo,
    ThumbnailInfo,
    respond_404,
    respond_with_file,
    respond_with_multipart_responder,
    respond_with_responder,
)
from synapse.media.media_storage import FileResponder, MediaStorage
from synapse.storage.databases.main.media_repository import LocalMedia

if TYPE_CHECKING:
    from synapse.media.media_repository import MediaRepository
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

EXIF_ORIENTATION_TAG = 0x0112
EXIF_TRANSPOSE_MAPPINGS = {
    2: Image.FLIP_LEFT_RIGHT,
    3: Image.ROTATE_180,
    4: Image.FLIP_TOP_BOTTOM,
    5: Image.TRANSPOSE,
    6: Image.ROTATE_270,
    7: Image.TRANSVERSE,
    8: Image.ROTATE_90,
}


class ThumbnailError(Exception):
    """An error occurred generating a thumbnail."""


class Thumbnailer:
    FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG"}

    @staticmethod
    def set_limits(max_image_pixels: int) -> None:
        Image.MAX_IMAGE_PIXELS = max_image_pixels

    def __init__(self, input_path: str):
        # Have we closed the image?
        self._closed = False

        try:
            self.image = Image.open(input_path)
        except OSError as e:
            # If an error occurs opening the image, a thumbnail won't be able to
            # be generated.
            raise ThumbnailError from e
        except Image.DecompressionBombError as e:
            # If an image decompression bomb error occurs opening the image,
            # then the image exceeds the pixel limit and a thumbnail won't
            # be able to be generated.
            raise ThumbnailError from e

        self.width, self.height = self.image.size
        self.transpose_method = None
        try:
            # We don't use ImageOps.exif_transpose since it crashes with big EXIF
            #
            # Ignore safety: Pillow seems to acknowledge that this method is
            # "private, experimental, but generally widely used". Pillow 6
            # includes a public getexif() method (no underscore) that we might
            # consider using instead when we can bump that dependency.
            #
            # At the time of writing, Debian buster (currently oldstable)
            # provides version 5.4.1. It's expected to EOL in mid-2022, see
            # https://wiki.debian.org/DebianReleases#Production_Releases
            image_exif = self.image._getexif()  # type: ignore
            if image_exif is not None:
                image_orientation = image_exif.get(EXIF_ORIENTATION_TAG)
                assert type(image_orientation) is int  # noqa: E721
                self.transpose_method = EXIF_TRANSPOSE_MAPPINGS.get(image_orientation)
        except Exception as e:
            # A lot of parsing errors can happen when parsing EXIF
            logger.info("Error parsing image EXIF information: %s", e)

    @trace
    def transpose(self) -> Tuple[int, int]:
        """Transpose the image using its EXIF Orientation tag

        Returns:
            A tuple containing the new image size in pixels as (width, height).
        """
        if self.transpose_method is not None:
            # Safety: `transpose` takes an int rather than e.g. an IntEnum.
            # self.transpose_method is set above to be a value in
            # EXIF_TRANSPOSE_MAPPINGS, and that only contains correct values.
            with self.image:
                self.image = self.image.transpose(self.transpose_method)  # type: ignore[arg-type]
            self.width, self.height = self.image.size
            self.transpose_method = None
            # We don't need EXIF any more
            self.image.info["exif"] = None
        return self.image.size

    def aspect(self, max_width: int, max_height: int) -> Tuple[int, int]:
        """Calculate the largest size that preserves aspect ratio which
        fits within the given rectangle::

            (w_in / h_in) = (w_out / h_out)
            w_out = max(min(w_max, h_max * (w_in / h_in)), 1)
            h_out = max(min(h_max, w_max * (h_in / w_in)), 1)

        Args:
            max_width: The largest possible width.
            max_height: The largest possible height.
        """

        if max_width * self.height < max_height * self.width:
            return max_width, max((max_width * self.height) // self.width, 1)
        else:
            return max((max_height * self.width) // self.height, 1), max_height

    def _resize(self, width: int, height: int) -> Image.Image:
        # 1-bit or 8-bit color palette images need converting to RGB
        # otherwise they will be scaled using nearest neighbour which
        # looks awful.
        #
        # If the image has transparency, use RGBA instead.
        if self.image.mode in ["1", "L", "P"]:
            if self.image.info.get("transparency", None) is not None:
                with self.image:
                    self.image = self.image.convert("RGBA")
            else:
                with self.image:
                    self.image = self.image.convert("RGB")
        return self.image.resize((width, height), Image.LANCZOS)

    @trace
    def scale(self, width: int, height: int, output_type: str) -> BytesIO:
        """Rescales the image to the given dimensions.

        Returns:
            The bytes of the encoded image ready to be written to disk
        """
        with self._resize(width, height) as scaled:
            return self._encode_image(scaled, output_type)

    @trace
    def crop(self, width: int, height: int, output_type: str) -> BytesIO:
        """Rescales and crops the image to the given dimensions preserving
        aspect::
            (w_in / h_in) = (w_scaled / h_scaled)
            w_scaled = max(w_out, h_out * (w_in / h_in))
            h_scaled = max(h_out, w_out * (h_in / w_in))

        Args:
            max_width: The largest possible width.
            max_height: The largest possible height.

        Returns:
            The bytes of the encoded image ready to be written to disk
        """
        if width * self.height > height * self.width:
            scaled_width = width
            scaled_height = (width * self.height) // self.width
            crop_top = (scaled_height - height) // 2
            crop_bottom = height + crop_top
            crop = (0, crop_top, width, crop_bottom)
        else:
            scaled_width = (height * self.width) // self.height
            scaled_height = height
            crop_left = (scaled_width - width) // 2
            crop_right = width + crop_left
            crop = (crop_left, 0, crop_right, height)

        with self._resize(scaled_width, scaled_height) as scaled_image:
            with scaled_image.crop(crop) as cropped:
                return self._encode_image(cropped, output_type)

    def _encode_image(self, output_image: Image.Image, output_type: str) -> BytesIO:
        output_bytes_io = BytesIO()
        fmt = self.FORMATS[output_type]
        if fmt == "JPEG":
            output_image = output_image.convert("RGB")
        output_image.save(output_bytes_io, fmt, quality=80)
        return output_bytes_io

    def close(self) -> None:
        """Closes the underlying image file.

        Once closed no other functions can be called.

        Can be called multiple times.
        """

        if self._closed:
            return

        self._closed = True

        # Since we run this on the finalizer then we need to handle `__init__`
        # raising an exception before it can define `self.image`.
        image = getattr(self, "image", None)
        if image is None:
            return

        image.close()

    def __enter__(self) -> "Thumbnailer":
        """Make `Thumbnailer` a context manager that calls `close` on
        `__exit__`.
        """
        return self

    def __exit__(
        self,
        type: Optional[Type[BaseException]],
        value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()

    def __del__(self) -> None:
        # Make sure we actually do close the image, rather than leak data.
        self.close()


class ThumbnailProvider:
    def __init__(
        self,
        hs: "HomeServer",
        media_repo: "MediaRepository",
        media_storage: MediaStorage,
    ):
        self.hs = hs
        self.media_repo = media_repo
        self.media_storage = media_storage
        self.store = hs.get_datastores().main
        self.dynamic_thumbnails = hs.config.media.dynamic_thumbnails

    async def respond_local_thumbnail(
        self,
        request: SynapseRequest,
        media_id: str,
        width: int,
        height: int,
        method: str,
        m_type: str,
        max_timeout_ms: int,
        for_federation: bool,
        allow_authenticated: bool = True,
    ) -> None:
        media_info = await self.media_repo.get_local_media_info(
            request, media_id, max_timeout_ms
        )
        if not media_info:
            return

        # if the media the thumbnail is generated from is authenticated, don't serve the
        # thumbnail over an unauthenticated endpoint
        if self.hs.config.media.enable_authenticated_media and not allow_authenticated:
            if media_info.authenticated:
                raise NotFoundError()

        thumbnail_infos = await self.store.get_local_media_thumbnails(media_id)
        await self._select_and_respond_with_thumbnail(
            request,
            width,
            height,
            method,
            m_type,
            thumbnail_infos,
            media_id,
            media_id,
            url_cache=bool(media_info.url_cache),
            server_name=None,
            for_federation=for_federation,
            media_info=media_info,
        )

    async def select_or_generate_local_thumbnail(
        self,
        request: SynapseRequest,
        media_id: str,
        desired_width: int,
        desired_height: int,
        desired_method: str,
        desired_type: str,
        max_timeout_ms: int,
        for_federation: bool,
        allow_authenticated: bool = True,
    ) -> None:
        media_info = await self.media_repo.get_local_media_info(
            request, media_id, max_timeout_ms
        )
        if not media_info:
            return

        # if the media the thumbnail is generated from is authenticated, don't serve the
        # thumbnail over an unauthenticated endpoint
        if self.hs.config.media.enable_authenticated_media and not allow_authenticated:
            if media_info.authenticated:
                raise NotFoundError()

        thumbnail_infos = await self.store.get_local_media_thumbnails(media_id)
        for info in thumbnail_infos:
            t_w = info.width == desired_width
            t_h = info.height == desired_height
            t_method = info.method == desired_method
            t_type = info.type == desired_type

            if t_w and t_h and t_method and t_type:
                file_info = FileInfo(
                    server_name=None,
                    file_id=media_id,
                    url_cache=bool(media_info.url_cache),
                    thumbnail=info,
                )

                responder = await self.media_storage.fetch_media(file_info)
                if responder:
                    if for_federation:
                        await respond_with_multipart_responder(
                            self.hs.get_clock(), request, responder, media_info
                        )
                        return
                    else:
                        await respond_with_responder(
                            request, responder, info.type, info.length
                        )
                        return

        logger.debug("We don't have a thumbnail of that size. Generating")

        # Okay, so we generate one.
        file_path = await self.media_repo.generate_local_exact_thumbnail(
            media_id,
            desired_width,
            desired_height,
            desired_method,
            desired_type,
            url_cache=bool(media_info.url_cache),
        )

        if file_path:
            if for_federation:
                await respond_with_multipart_responder(
                    self.hs.get_clock(),
                    request,
                    FileResponder(open(file_path, "rb")),
                    media_info,
                )
            else:
                await respond_with_file(request, desired_type, file_path)
        else:
            logger.warning("Failed to generate thumbnail")
            raise SynapseError(400, "Failed to generate thumbnail.")

    async def select_or_generate_remote_thumbnail(
        self,
        request: SynapseRequest,
        server_name: str,
        media_id: str,
        desired_width: int,
        desired_height: int,
        desired_method: str,
        desired_type: str,
        max_timeout_ms: int,
        ip_address: str,
        use_federation: bool,
        allow_authenticated: bool = True,
    ) -> None:
        media_info = await self.media_repo.get_remote_media_info(
            server_name,
            media_id,
            max_timeout_ms,
            ip_address,
            use_federation,
            allow_authenticated,
        )
        if not media_info:
            respond_404(request)
            return

        # if the media the thumbnail is generated from is authenticated, don't serve the
        # thumbnail over an unauthenticated endpoint
        if self.hs.config.media.enable_authenticated_media and not allow_authenticated:
            if media_info.authenticated:
                respond_404(request)
                return

        thumbnail_infos = await self.store.get_remote_media_thumbnails(
            server_name, media_id
        )

        file_id = media_info.filesystem_id

        for info in thumbnail_infos:
            t_w = info.width == desired_width
            t_h = info.height == desired_height
            t_method = info.method == desired_method
            t_type = info.type == desired_type

            if t_w and t_h and t_method and t_type:
                file_info = FileInfo(
                    server_name=server_name,
                    file_id=file_id,
                    thumbnail=info,
                )

                responder = await self.media_storage.fetch_media(file_info)
                if responder:
                    await respond_with_responder(
                        request, responder, info.type, info.length
                    )
                    return

        logger.debug("We don't have a thumbnail of that size. Generating")

        # Okay, so we generate one.
        file_path = await self.media_repo.generate_remote_exact_thumbnail(
            server_name,
            file_id,
            media_id,
            desired_width,
            desired_height,
            desired_method,
            desired_type,
        )

        if file_path:
            await respond_with_file(request, desired_type, file_path)
        else:
            logger.warning("Failed to generate thumbnail")
            raise SynapseError(400, "Failed to generate thumbnail.")

    async def respond_remote_thumbnail(
        self,
        request: SynapseRequest,
        server_name: str,
        media_id: str,
        width: int,
        height: int,
        method: str,
        m_type: str,
        max_timeout_ms: int,
        ip_address: str,
        use_federation: bool,
        allow_authenticated: bool = True,
    ) -> None:
        # TODO: Don't download the whole remote file
        # We should proxy the thumbnail from the remote server instead of
        # downloading the remote file and generating our own thumbnails.
        media_info = await self.media_repo.get_remote_media_info(
            server_name,
            media_id,
            max_timeout_ms,
            ip_address,
            use_federation,
            allow_authenticated,
        )
        if not media_info:
            return

        # if the media the thumbnail is generated from is authenticated, don't serve the
        # thumbnail over an unauthenticated endpoint
        if self.hs.config.media.enable_authenticated_media and not allow_authenticated:
            if media_info.authenticated:
                raise NotFoundError()

        thumbnail_infos = await self.store.get_remote_media_thumbnails(
            server_name, media_id
        )
        await self._select_and_respond_with_thumbnail(
            request,
            width,
            height,
            method,
            m_type,
            thumbnail_infos,
            media_id,
            media_info.filesystem_id,
            url_cache=False,
            server_name=server_name,
            for_federation=False,
        )

    async def _select_and_respond_with_thumbnail(
        self,
        request: SynapseRequest,
        desired_width: int,
        desired_height: int,
        desired_method: str,
        desired_type: str,
        thumbnail_infos: List[ThumbnailInfo],
        media_id: str,
        file_id: str,
        url_cache: bool,
        for_federation: bool,
        media_info: Optional[LocalMedia] = None,
        server_name: Optional[str] = None,
    ) -> None:
        """
        Respond to a request with an appropriate thumbnail from the previously generated thumbnails.

        Args:
            request: The incoming request.
            desired_width: The desired width, the returned thumbnail may be larger than this.
            desired_height: The desired height, the returned thumbnail may be larger than this.
            desired_method: The desired method used to generate the thumbnail.
            desired_type: The desired content-type of the thumbnail.
            thumbnail_infos: A list of thumbnail info of candidate thumbnails.
            file_id: The ID of the media that a thumbnail is being requested for.
            url_cache: True if this is from a URL cache.
            server_name: The server name, if this is a remote thumbnail.
            for_federation: whether the request is from the federation /thumbnail request
            media_info: metadata about the media being requested.
        """
        logger.debug(
            "_select_and_respond_with_thumbnail: media_id=%s desired=%sx%s (%s) thumbnail_infos=%s",
            media_id,
            desired_width,
            desired_height,
            desired_method,
            thumbnail_infos,
        )

        # If `dynamic_thumbnails` is enabled, we expect Synapse to go down a
        # different code path to handle it.
        assert not self.dynamic_thumbnails

        if thumbnail_infos:
            file_info = self._select_thumbnail(
                desired_width,
                desired_height,
                desired_method,
                desired_type,
                thumbnail_infos,
                file_id,
                url_cache,
                server_name,
            )
            if not file_info:
                logger.info("Couldn't find a thumbnail matching the desired inputs")
                respond_404(request)
                return

            # The thumbnail property must exist.
            assert file_info.thumbnail is not None

            responder = await self.media_storage.fetch_media(file_info)
            if responder:
                if for_federation:
                    assert media_info is not None
                    await respond_with_multipart_responder(
                        self.hs.get_clock(), request, responder, media_info
                    )
                    return
                else:
                    await respond_with_responder(
                        request,
                        responder,
                        file_info.thumbnail.type,
                        file_info.thumbnail.length,
                    )
                    return

            # If we can't find the thumbnail we regenerate it. This can happen
            # if e.g. we've deleted the thumbnails but still have the original
            # image somewhere.
            #
            # Since we have an entry for the thumbnail in the DB we a) know we
            # have have successfully generated the thumbnail in the past (so we
            # don't need to worry about repeatedly failing to generate
            # thumbnails), and b) have already calculated that appropriate
            # width/height/method so we can just call the "generate exact"
            # methods.

            # First let's check that we do actually have the original image
            # still. This will throw a 404 if we don't.
            # TODO: We should refetch the thumbnails for remote media.
            await self.media_storage.ensure_media_is_in_local_cache(
                FileInfo(server_name, file_id, url_cache=url_cache)
            )

            if server_name:
                await self.media_repo.generate_remote_exact_thumbnail(
                    server_name,
                    file_id=file_id,
                    media_id=media_id,
                    t_width=file_info.thumbnail.width,
                    t_height=file_info.thumbnail.height,
                    t_method=file_info.thumbnail.method,
                    t_type=file_info.thumbnail.type,
                )
            else:
                await self.media_repo.generate_local_exact_thumbnail(
                    media_id=media_id,
                    t_width=file_info.thumbnail.width,
                    t_height=file_info.thumbnail.height,
                    t_method=file_info.thumbnail.method,
                    t_type=file_info.thumbnail.type,
                    url_cache=url_cache,
                )

            responder = await self.media_storage.fetch_media(file_info)
            if for_federation:
                assert media_info is not None
                await respond_with_multipart_responder(
                    self.hs.get_clock(), request, responder, media_info
                )
            else:
                await respond_with_responder(
                    request,
                    responder,
                    file_info.thumbnail.type,
                    file_info.thumbnail.length,
                )
        else:
            # This might be because:
            # 1. We can't create thumbnails for the given media (corrupted or
            #    unsupported file type), or
            # 2. The thumbnailing process never ran or errored out initially
            #    when the media was first uploaded (these bugs should be
            #    reported and fixed).
            # Note that we don't attempt to generate a thumbnail now because
            # `dynamic_thumbnails` is disabled.
            logger.info("Failed to find any generated thumbnails")

            assert request.path is not None
            respond_with_json(
                request,
                400,
                cs_error(
                    "Cannot find any thumbnails for the requested media ('%s'). This might mean the media is not a supported_media_format=(%s) or that thumbnailing failed for some other reason. (Dynamic thumbnails are disabled on this server.)"
                    % (
                        request.path.decode(),
                        ", ".join(THUMBNAIL_SUPPORTED_MEDIA_FORMAT_MAP.keys()),
                    ),
                    code=Codes.UNKNOWN,
                ),
                send_cors=True,
            )

    def _select_thumbnail(
        self,
        desired_width: int,
        desired_height: int,
        desired_method: str,
        desired_type: str,
        thumbnail_infos: List[ThumbnailInfo],
        file_id: str,
        url_cache: bool,
        server_name: Optional[str],
    ) -> Optional[FileInfo]:
        """
        Choose an appropriate thumbnail from the previously generated thumbnails.

        Args:
            desired_width: The desired width, the returned thumbnail may be larger than this.
            desired_height: The desired height, the returned thumbnail may be larger than this.
            desired_method: The desired method used to generate the thumbnail.
            desired_type: The desired content-type of the thumbnail.
            thumbnail_infos: A list of thumbnail infos of candidate thumbnails.
            file_id: The ID of the media that a thumbnail is being requested for.
            url_cache: True if this is from a URL cache.
            server_name: The server name, if this is a remote thumbnail.

        Returns:
             The thumbnail which best matches the desired parameters.
        """
        desired_method = desired_method.lower()

        # The chosen thumbnail.
        thumbnail_info = None

        d_w = desired_width
        d_h = desired_height

        if desired_method == "crop":
            # Thumbnails that match equal or larger sizes of desired width/height.
            crop_info_list: List[
                Tuple[int, int, int, bool, Optional[int], ThumbnailInfo]
            ] = []
            # Other thumbnails.
            crop_info_list2: List[
                Tuple[int, int, int, bool, Optional[int], ThumbnailInfo]
            ] = []
            for info in thumbnail_infos:
                # Skip thumbnails generated with different methods.
                if info.method != "crop":
                    continue

                t_w = info.width
                t_h = info.height
                aspect_quality = abs(d_w * t_h - d_h * t_w)
                min_quality = 0 if d_w <= t_w and d_h <= t_h else 1
                size_quality = abs((d_w - t_w) * (d_h - t_h))
                type_quality = desired_type != info.type
                length_quality = info.length
                if t_w >= d_w or t_h >= d_h:
                    crop_info_list.append(
                        (
                            aspect_quality,
                            min_quality,
                            size_quality,
                            type_quality,
                            length_quality,
                            info,
                        )
                    )
                else:
                    crop_info_list2.append(
                        (
                            aspect_quality,
                            min_quality,
                            size_quality,
                            type_quality,
                            length_quality,
                            info,
                        )
                    )
            # Pick the most appropriate thumbnail. Some values of `desired_width` and
            # `desired_height` may result in a tie, in which case we avoid comparing on
            # the thumbnail info and pick the thumbnail that appears earlier
            # in the list of candidates.
            if crop_info_list:
                thumbnail_info = min(crop_info_list, key=lambda t: t[:-1])[-1]
            elif crop_info_list2:
                thumbnail_info = min(crop_info_list2, key=lambda t: t[:-1])[-1]
        elif desired_method == "scale":
            # Thumbnails that match equal or larger sizes of desired width/height.
            info_list: List[Tuple[int, bool, int, ThumbnailInfo]] = []
            # Other thumbnails.
            info_list2: List[Tuple[int, bool, int, ThumbnailInfo]] = []

            for info in thumbnail_infos:
                # Skip thumbnails generated with different methods.
                if info.method != "scale":
                    continue

                t_w = info.width
                t_h = info.height
                size_quality = abs((d_w - t_w) * (d_h - t_h))
                type_quality = desired_type != info.type
                length_quality = info.length
                if t_w >= d_w or t_h >= d_h:
                    info_list.append((size_quality, type_quality, length_quality, info))
                else:
                    info_list2.append(
                        (size_quality, type_quality, length_quality, info)
                    )
            # Pick the most appropriate thumbnail. Some values of `desired_width` and
            # `desired_height` may result in a tie, in which case we avoid comparing on
            # the thumbnail info and pick the thumbnail that appears earlier
            # in the list of candidates.
            if info_list:
                thumbnail_info = min(info_list, key=lambda t: t[:-1])[-1]
            elif info_list2:
                thumbnail_info = min(info_list2, key=lambda t: t[:-1])[-1]

        if thumbnail_info:
            return FileInfo(
                file_id=file_id,
                url_cache=url_cache,
                server_name=server_name,
                thumbnail=thumbnail_info,
            )

        # No matching thumbnail was found.
        return None
