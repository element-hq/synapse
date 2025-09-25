#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
#  Copyright 2021 The Matrix.org Foundation C.I.C.
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
import html
import logging
import urllib.parse
from typing import TYPE_CHECKING, List, Optional, cast

import attr

from synapse.media.preview_html import parse_html_description
from synapse.types import JsonDict
from synapse.util.json import json_decoder

if TYPE_CHECKING:
    from lxml import etree

    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class OEmbedResult:
    # The Open Graph result (converted from the oEmbed result).
    open_graph_result: JsonDict
    # The author_name of the oEmbed result
    author_name: Optional[str]
    # Number of milliseconds to cache the content, according to the oEmbed response.
    #
    # This will be None if no cache-age is provided in the oEmbed response (or
    # if the oEmbed response cannot be turned into an Open Graph response).
    cache_age: Optional[int]


class OEmbedProvider:
    """
    A helper for accessing oEmbed content.

    It can be used to check if a URL should be accessed via oEmbed and for
    requesting/parsing oEmbed content.
    """

    def __init__(self, hs: "HomeServer"):
        self._oembed_patterns = {}
        for oembed_endpoint in hs.config.oembed.oembed_patterns:
            api_endpoint = oembed_endpoint.api_endpoint

            # Only JSON is supported at the moment. This could be declared in
            # the formats field. Otherwise, if the endpoint ends in .xml assume
            # it doesn't support JSON.
            if (
                oembed_endpoint.formats is not None
                and "json" not in oembed_endpoint.formats
            ) or api_endpoint.endswith(".xml"):
                logger.info(
                    "Ignoring oEmbed endpoint due to not supporting JSON: %s",
                    api_endpoint,
                )
                continue

            # Iterate through each URL pattern and point it to the endpoint.
            for pattern in oembed_endpoint.url_patterns:
                self._oembed_patterns[pattern] = api_endpoint

    def get_oembed_url(self, url: str) -> Optional[str]:
        """
        Check whether the URL should be downloaded as oEmbed content instead.

        Args:
            url: The URL to check.

        Returns:
            A URL to use instead or None if the original URL should be used.
        """
        for url_pattern, endpoint in self._oembed_patterns.items():
            if url_pattern.fullmatch(url):
                # TODO Specify max height / width.

                # Note that only the JSON format is supported, some endpoints want
                # this in the URL, others want it as an argument.
                endpoint = endpoint.replace("{format}", "json")

                args = {"url": url, "format": "json"}
                query_str = urllib.parse.urlencode(args, True)
                return f"{endpoint}?{query_str}"

        # No match.
        return None

    def autodiscover_from_html(self, tree: "etree._Element") -> Optional[str]:
        """
        Search an HTML document for oEmbed autodiscovery information.

        Args:
            tree: The parsed HTML body.

        Returns:
            The URL to use for oEmbed information, or None if no URL was found.
        """
        # Search for link elements with the proper rel and type attributes.
        # Cast: the type returned by xpath depends on the xpath expression: mypy can't deduce this.
        for tag in cast(
            List["etree._Element"],
            tree.xpath("//link[@rel='alternate'][@type='application/json+oembed']"),
        ):
            if "href" in tag.attrib:
                return cast(str, tag.attrib["href"])

        # Some providers (e.g. Flickr) use alternative instead of alternate.
        # Cast: the type returned by xpath depends on the xpath expression: mypy can't deduce this.
        for tag in cast(
            List["etree._Element"],
            tree.xpath("//link[@rel='alternative'][@type='application/json+oembed']"),
        ):
            if "href" in tag.attrib:
                return cast(str, tag.attrib["href"])

        return None

    def parse_oembed_response(self, url: str, raw_body: bytes) -> OEmbedResult:
        """
        Parse the oEmbed response into an Open Graph response.

        Args:
            url: The URL which is being previewed (not the one which was
                requested).
            raw_body: The oEmbed response as JSON encoded as bytes.

        Returns:
            json-encoded Open Graph data
        """

        try:
            # oEmbed responses *must* be UTF-8 according to the spec.
            oembed = json_decoder.decode(raw_body.decode("utf-8"))
        except ValueError:
            return OEmbedResult({}, None, None)

        # The version is a required string field, but not always provided,
        # or sometimes provided as a float. Be lenient.
        oembed_version = oembed.get("version", "1.0")
        if oembed_version != "1.0" and oembed_version != 1:
            return OEmbedResult({}, None, None)

        # Attempt to parse the cache age, if possible.
        try:
            cache_age = int(oembed.get("cache_age")) * 1000
        except (TypeError, ValueError):
            # If the cache age cannot be parsed (e.g. wrong type or invalid
            # string), ignore it.
            cache_age = None

        # The oEmbed response converted to Open Graph.
        open_graph_response: JsonDict = {"og:url": url}

        title = oembed.get("title")
        if title and isinstance(title, str):
            # A common WordPress plug-in seems to incorrectly escape entities
            # in the oEmbed response.
            open_graph_response["og:title"] = html.unescape(title)

        author_name = oembed.get("author_name")
        if not isinstance(author_name, str):
            author_name = None

        # Use the provider name and as the site.
        provider_name = oembed.get("provider_name")
        if provider_name and isinstance(provider_name, str):
            open_graph_response["og:site_name"] = provider_name

        # If a thumbnail exists, use it. Note that dimensions will be calculated later.
        thumbnail_url = oembed.get("thumbnail_url")
        if thumbnail_url and isinstance(thumbnail_url, str):
            open_graph_response["og:image"] = thumbnail_url

        # Process each type separately.
        oembed_type = oembed.get("type")
        if oembed_type == "rich":
            html_str = oembed.get("html")
            if isinstance(html_str, str):
                calc_description_and_urls(open_graph_response, html_str)

        elif oembed_type == "photo":
            # If this is a photo, use the full image, not the thumbnail.
            url = oembed.get("url")
            if url and isinstance(url, str):
                open_graph_response["og:image"] = url

        elif oembed_type == "video":
            open_graph_response["og:type"] = "video.other"
            html_str = oembed.get("html")
            if html_str and isinstance(html_str, str):
                calc_description_and_urls(open_graph_response, oembed["html"])
            for size in ("width", "height"):
                val = oembed.get(size)
                if type(val) is int:  # noqa: E721
                    open_graph_response[f"og:video:{size}"] = val

        elif oembed_type == "link":
            open_graph_response["og:type"] = "website"

        else:
            logger.warning("Unknown oEmbed type: %s", oembed_type)

        return OEmbedResult(open_graph_response, author_name, cache_age)


def _fetch_urls(tree: "etree._Element", tag_name: str) -> List[str]:
    results = []
    # Cast: the type returned by xpath depends on the xpath expression: mypy can't deduce this.
    for tag in cast(List["etree._Element"], tree.xpath("//*/" + tag_name)):
        if "src" in tag.attrib:
            results.append(cast(str, tag.attrib["src"]))
    return results


def calc_description_and_urls(open_graph_response: JsonDict, html_body: str) -> None:
    """
    Calculate description for an HTML document.

    This uses lxml to convert the HTML document into plaintext. If errors
    occur during processing of the document, an empty response is returned.

    Args:
        open_graph_response: The current Open Graph summary. This is updated with additional fields.
        html_body: The HTML document, as bytes.

    Returns:
        The summary
    """
    # If there's no body, nothing useful is going to be found.
    if not html_body:
        return

    from lxml import etree

    # Create an HTML parser. If this fails, log and return no metadata.
    parser = etree.HTMLParser(recover=True, encoding="utf-8")

    # Attempt to parse the body. If this fails, log and return no metadata.
    tree = etree.fromstring(html_body, parser)

    # The data was successfully parsed, but no tree was found.
    if tree is None:
        return

    # Attempt to find interesting URLs (images, videos, embeds).
    if "og:image" not in open_graph_response:
        image_urls = _fetch_urls(tree, "img")
        if image_urls:
            open_graph_response["og:image"] = image_urls[0]

    video_urls = _fetch_urls(tree, "video") + _fetch_urls(tree, "embed")
    if video_urls:
        open_graph_response["og:video"] = video_urls[0]

    description = parse_html_description(tree)
    if description:
        open_graph_response["og:description"] = description
