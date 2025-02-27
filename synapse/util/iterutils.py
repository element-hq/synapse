#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
import heapq
from itertools import islice
from typing import (
    Callable,
    Collection,
    Dict,
    Generator,
    Iterable,
    Iterator,
    List,
    Mapping,
    Set,
    Sized,
    Tuple,
    TypeVar,
)

from typing_extensions import Protocol

T = TypeVar("T")
S = TypeVar("S", bound="_SelfSlice")


class _SelfSlice(Sized, Protocol):
    """A helper protocol that matches types where taking a slice results in the
    same type being returned.

    This is more specific than `Sequence`, which allows another `Sequence` to be
    returned.
    """

    def __getitem__(self: S, i: slice) -> S: ...


def batch_iter(iterable: Iterable[T], size: int) -> Iterator[Tuple[T, ...]]:
    """batch an iterable up into tuples with a maximum size

    Args:
        iterable: the iterable to slice
        size: the maximum batch size

    Returns:
        an iterator over the chunks
    """
    # make sure we can deal with iterables like lists too
    sourceiter = iter(iterable)
    # call islice until it returns an empty tuple
    return iter(lambda: tuple(islice(sourceiter, size)), ())


def chunk_seq(iseq: S, maxlen: int) -> Iterator[S]:
    """Split the given sequence into chunks of the given size

    The last chunk may be shorter than the given size.

    If the input is empty, no chunks are returned.
    """
    return (iseq[i : i + maxlen] for i in range(0, len(iseq), maxlen))


def partition(
    iterable: Iterable[T], predicate: Callable[[T], bool]
) -> Tuple[List[T], List[T]]:
    """
    Separate a given iterable into two lists based on the result of a predicate function.

    Args:
        iterable: the iterable to partition (separate)
        predicate: a function that takes an item from the iterable and returns a boolean

    Returns:
        A tuple of two lists, the first containing all items for which the predicate
        returned True, the second containing all items for which the predicate returned
        False
    """
    true_results = []
    false_results = []
    for item in iterable:
        if predicate(item):
            true_results.append(item)
        else:
            false_results.append(item)
    return true_results, false_results


def sorted_topologically(
    nodes: Iterable[T],
    graph: Mapping[T, Collection[T]],
) -> Generator[T, None, None]:
    """Given a set of nodes and a graph, yield the nodes in toplogical order.

    For example `sorted_topologically([1, 2], {1: [2]})` will yield `2, 1`.
    """

    # This is implemented by Kahn's algorithm.

    degree_map = {node: 0 for node in nodes}
    reverse_graph: Dict[T, Set[T]] = {}

    for node, edges in graph.items():
        if node not in degree_map:
            continue

        for edge in set(edges):
            if edge in degree_map:
                degree_map[node] += 1

            reverse_graph.setdefault(edge, set()).add(node)
        reverse_graph.setdefault(node, set())

    zero_degree = [node for node, degree in degree_map.items() if degree == 0]
    heapq.heapify(zero_degree)

    while zero_degree:
        node = heapq.heappop(zero_degree)
        yield node

        for edge in reverse_graph.get(node, []):
            if edge in degree_map:
                degree_map[edge] -= 1
                if degree_map[edge] == 0:
                    heapq.heappush(zero_degree, edge)


def sorted_topologically_batched(
    nodes: Iterable[T],
    graph: Mapping[T, Collection[T]],
) -> Generator[Collection[T], None, None]:
    r"""Walk the graph topologically, returning batches of nodes where all nodes
    that references it have been previously returned.

    For example, given the following graph:

         A
        / \
       B   C
        \ /
         D

    This function will return: `[[A], [B, C], [D]]`.

    This function is useful for e.g. batch persisting events in an auth chain,
    where we can only persist an event if all its auth events have already been
    persisted.
    """

    degree_map = {node: 0 for node in nodes}
    reverse_graph: Dict[T, Set[T]] = {}

    for node, edges in graph.items():
        if node not in degree_map:
            continue

        for edge in set(edges):
            if edge in degree_map:
                degree_map[node] += 1

            reverse_graph.setdefault(edge, set()).add(node)
        reverse_graph.setdefault(node, set())

    zero_degree = [node for node, degree in degree_map.items() if degree == 0]

    while zero_degree:
        new_zero_degree = []
        for node in zero_degree:
            for edge in reverse_graph.get(node, []):
                if edge in degree_map:
                    degree_map[edge] -= 1
                    if degree_map[edge] == 0:
                        new_zero_degree.append(edge)

        yield zero_degree
        zero_degree = new_zero_degree
