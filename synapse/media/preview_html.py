#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
import itertools
import logging
import re
from typing import (
    TYPE_CHECKING,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Optional,
    cast,
)

if TYPE_CHECKING:
    from bs4 import BeautifulSoup
    from bs4.element import PageElement, Tag

logger = logging.getLogger(__name__)

_content_type_match = re.compile(r'.*; *charset="?(.*?)"?(;|$)', flags=re.I)

# Certain elements aren't meant for display.
ARIA_ROLES_TO_IGNORE = {"directory", "menu", "menubar", "toolbar"}

NON_BLANK = re.compile(".+")


def decode_body(body: bytes | str, uri: str) -> Optional["BeautifulSoup"]:
    """
    This uses BeautifulSoup to parse the HTML document.

    Args:
        body: The HTML document, as bytes.
        uri: The URI used to download the body.
        content_type: The Content-Type header.

    Returns:
        The parsed HTML body, or None if an error occurred during processing.
    """
    # If there's no body, nothing useful is going to be found.
    if not body:
        return None

    from bs4 import BeautifulSoup
    from bs4.builder import ParserRejectedMarkup

    try:
        soup = BeautifulSoup(body, "html.parser")
        # If an empty document is returned, convert to None.
        if not len(soup):
            return None
        return soup
    except ParserRejectedMarkup:
        logger.warning("Unable to decode HTML body for %s", uri)
        return None


def _get_meta_tags(
    soup: "BeautifulSoup",
    property: str,
    prefix: str,
    property_mapper: Callable[[str], str | None] | None = None,
) -> dict[str, str | None]:
    """
    Search for meta tags prefixed with a particular string.

    Args:
        soup: The parsed HTML document.
        property: The name of the property which contains the tag name, e.g.
            "property" for Open Graph.
        prefix: The prefix on the property to search for, e.g. "og" for Open Graph.
        property_mapper: An optional callable to map the property to the Open Graph
            form. Can return None for a key to ignore that key.

    Returns:
        A map of tag name to value.
    """
    results: dict[str, str | None] = {}
    # Cast: the type returned by xpath depends on the xpath expression: mypy can't deduce this.
    for tag in soup.find_all(
        "meta", attrs={property: re.compile(rf"^{prefix}:")}, content=NON_BLANK
    ):
        # if we've got more than 50 tags, someone is taking the piss
        if len(results) >= 50:
            logger.warning(
                "Skipping parsing of Open Graph for page with too many '%s:' tags",
                prefix,
            )
            return {}

        key = cast(str, tag[property])
        if property_mapper:
            new_key = property_mapper(key)
            # None is a special value used to ignore a value.
            if new_key is None:
                continue
            key = new_key

        results[key] = cast(str, tag["content"])

    return results


def _map_twitter_to_open_graph(key: str) -> str | None:
    """
    Map a Twitter card property to the analogous Open Graph property.

    Args:
        key: The Twitter card property (starts with "twitter:").

    Returns:
        The Open Graph property (starts with "og:") or None to have this property
        be ignored.
    """
    # Twitter card properties with no analogous Open Graph property.
    if key == "twitter:card" or key == "twitter:creator":
        return None
    if key == "twitter:site":
        return "og:site_name"
    # Otherwise, swap twitter to og.
    return "og" + key[7:]


def parse_html_to_open_graph(soup: "BeautifulSoup") -> dict[str, str | None]:
    """
    Calculate metadata for an HTML document.

    This uses BeautifulSoup to search the HTML document for Open Graph data.

    Args:
        soup: The parsed HTML document.

    Returns:
        The Open Graph response as a dictionary.
    """

    # Search for Open Graph (og:) meta tags, e.g.:
    #
    # "og:type"         : "video",
    # "og:url"          : "https://www.youtube.com/watch?v=LXDBoHyjmtw",
    # "og:site_name"    : "YouTube",
    # "og:video:type"   : "application/x-shockwave-flash",
    # "og:description"  : "Fun stuff happening here",
    # "og:title"        : "RemoteJam - Matrix team hack for Disrupt Europe Hackathon",
    # "og:image"        : "https://i.ytimg.com/vi/LXDBoHyjmtw/maxresdefault.jpg",
    # "og:video:url"    : "http://www.youtube.com/v/LXDBoHyjmtw?version=3&autohide=1",
    # "og:video:width"  : "1280"
    # "og:video:height" : "720",
    # "og:video:secure_url": "https://www.youtube.com/v/LXDBoHyjmtw?version=3",

    # TODO: grab article: meta tags too, e.g.:
    og = _get_meta_tags(soup, "property", "og")

    # TODO: Search for properties specific to the different Open Graph types,
    # such as article: meta tags, e.g.:
    #
    # "article:publisher" : "https://www.facebook.com/thethudonline" />
    # "article:author" content="https://www.facebook.com/thethudonline" />
    # "article:tag" content="baby" />
    # "article:section" content="Breaking News" />
    # "article:published_time" content="2016-03-31T19:58:24+00:00" />
    # "article:modified_time" content="2016-04-01T18:31:53+00:00" />

    # Search for Twitter Card (twitter:) meta tags, e.g.:
    #
    # "twitter:site"    : "@matrixdotorg"
    # "twitter:creator" : "@matrixdotorg"
    #
    # Twitter cards tags also duplicate Open Graph tags.
    #
    # See https://developer.twitter.com/en/docs/twitter-for-websites/cards/guides/getting-started
    twitter = _get_meta_tags(soup, "name", "twitter", _map_twitter_to_open_graph)
    # Merge the Twitter values with the Open Graph values, but do not overwrite
    # information from Open Graph tags.
    for key, value in twitter.items():
        if key not in og:
            og[key] = value

    if "og:title" not in og:
        # Attempt to find a title from the title tag, or the biggest header on the page.
        #
        # mypy doesn't like passing both name and string, but it is used to ignore
        # empty elements.
        title = soup.find(("title", "h1", "h2", "h3"), string=True)  # type: ignore[call-overload]
        if title and title.string:
            og["og:title"] = title.string.strip()
        else:
            og["og:title"] = None

    if "og:image" not in og:
        # Check microdata for an image.
        meta_image = soup.find(
            "meta", itemprop=re.compile("image", re.I), content=NON_BLANK
        )
        # If a meta image is found, use it.
        if meta_image:
            og["og:image"] = cast(str, meta_image["content"])
        else:
            # Try to find images which are larger than 10px by 10px.
            #
            # TODO: consider inlined CSS styles as well as width & height attribs
            images = cast(
                list["Tag"],
                soup.find_all("img", src=NON_BLANK, width=NON_BLANK, height=NON_BLANK),
            )
            images = sorted(
                filter(
                    lambda tag: int(cast(str, tag["width"])) > 10
                    and int(cast(str, tag["height"])) > 10,
                    images,
                ),
                key=lambda i: (
                    -1 * float(cast(str, i["width"])) * float(cast(str, i["height"]))
                ),
            )
            # If no images were found, try to find *any* images.
            if not images:
                images = soup.find_all("img", src=NON_BLANK, limit=1)
            if images:
                og["og:image"] = cast(str, images[0]["src"])

            # Finally, fallback to the favicon if nothing else.
            else:
                favicon = cast("Tag", soup.find("link", href=NON_BLANK, rel="icon"))
                if favicon:
                    og["og:image"] = cast(str, favicon["href"])

    if "og:description" not in og:
        # Check the first meta description tag for content.
        meta_description = soup.find(
            "meta",
            attrs={"name": re.compile("description", re.I)},
            content=NON_BLANK,
        )

        # If a meta description is found with content, use it.
        if meta_description:
            og["og:description"] = cast(str, meta_description["content"])
        else:
            og["og:description"] = parse_html_description(soup)
    elif og["og:description"]:
        # This must be a non-empty string at this point.
        assert isinstance(og["og:description"], str)
        og["og:description"] = summarize_paragraphs([og["og:description"]])

    # TODO: delete the url downloads to stop diskfilling,
    # as we only ever cared about its OG
    return og


def parse_html_description(soup: "BeautifulSoup") -> str | None:
    """
    Calculate a text description based on an HTML document.

    Grabs any text nodes which are inside the <body/> tag, unless they are within
    an HTML5 semantic markup tag (<header/>, <nav/>, <aside/>, <footer/>), or
    if they are within a <script/>, <svg/> or <style/> tag, or if they are within
    a tag whose content is usually only shown to old browsers
    (<iframe/>, <video/>, <canvas/>, <picture/>).

    This is a very very very coarse approximation to a plain text render of the page.

    Args:
        soup: The parsed HTML document.

    Returns:
        The plain text description, or None if one cannot be generated.
    """

    TAGS_TO_REMOVE = {
        "head",
        "header",
        "nav",
        "aside",
        "footer",
        "script",
        "noscript",
        "style",
        "svg",
        "iframe",
        "video",
        "canvas",
        "img",
        "picture",
    }

    # Split all the text nodes into paragraphs (by splitting on new
    # lines)
    text_nodes = (
        re.sub(r"\s+", "\n", el).strip()
        for el in _iterate_over_text(soup, TAGS_TO_REMOVE)
    )
    return summarize_paragraphs(text_nodes)


def _iterate_over_text(
    soup: "BeautifulSoup",
    tags_to_ignore: Iterable[str],
    stack_limit: int = 1024,
) -> Generator[str, None, None]:
    """Iterate over the document returning text nodes in a depth first fashion,
    skipping text nodes inside certain tags.

    Args:
        soup: The parent element to iterate. Can be None if there isn't one.
        tags_to_ignore: Set of tags to ignore
        stack_limit: Maximum stack size limit for depth-first traversal.
            Nodes will be dropped if this limit is hit, which may truncate the
            textual result.
            Intended to limit the maximum working memory when generating a preview.
    """

    from bs4.element import NavigableString, Tag

    # This is basically a stack that we extend using itertools.chain.
    # This will either consist of an element to iterate over *or* a string
    # to be returned.
    elements: Iterator["PageElement"] = iter([soup])
    while True:
        el = next(elements, None)
        if el is None:
            return

        # Do not consider sub-classes of NavigableString since those represent
        # comments, etc.
        if type(el) == NavigableString:  # noqa: E721
            yield str(el)
        elif isinstance(el, Tag) and el.name not in tags_to_ignore:
            # We add to the stack all the element's children.
            elements = itertools.chain(el.contents, elements)


def summarize_paragraphs(
    text_nodes: Iterable[str], min_size: int = 200, max_size: int = 500
) -> str | None:
    """
    Try to get a summary respecting first paragraph and then word boundaries.

    Args:
        text_nodes: The paragraphs to summarize.
        min_size: The minimum number of words to include.
        max_size: The maximum number of words to include.

    Returns:
        A summary of the text nodes, or None if that was not possible.
    """

    # TODO: Respect sentences?

    description = ""

    # Keep adding paragraphs until we get to the MIN_SIZE.
    for text_node in text_nodes:
        if len(description) < min_size:
            text_node = re.sub(r"[\t \r\n]+", " ", text_node)
            description += text_node + "\n\n"
        else:
            break

    description = description.strip()
    description = re.sub(r"[\t ]+", " ", description)
    description = re.sub(r"[\t \r\n]*[\r\n]+", "\n\n", description)

    # If the concatenation of paragraphs to get above MIN_SIZE
    # took us over MAX_SIZE, then we need to truncate mid paragraph
    if len(description) > max_size:
        new_desc = ""

        # This splits the paragraph into words, but keeping the
        # (preceding) whitespace intact so we can easily concat
        # words back together.
        for match in re.finditer(r"\s*\S+", description):
            word = match.group()

            # Keep adding words while the total length is less than
            # MAX_SIZE.
            if len(word) + len(new_desc) < max_size:
                new_desc += word
            else:
                # At this point the next word *will* take us over
                # MAX_SIZE, but we also want to ensure that its not
                # a huge word. If it is add it anyway and we'll
                # truncate later.
                if len(new_desc) < min_size:
                    new_desc += word
                break

        # Double check that we're not over the limit
        if len(new_desc) > max_size:
            new_desc = new_desc[:max_size]

        # We always add an ellipsis because at the very least
        # we chopped mid paragraph.
        description = new_desc.strip() + "…"
    return description if description else None
