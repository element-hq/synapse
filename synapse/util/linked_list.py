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

"""A circular doubly linked list implementation.

Free-threading safe: each list has its own lock (stored on the root node)
rather than a single global lock shared by all lists. This eliminates
cross-cache contention under free-threaded Python.
"""

import threading
from typing import Generic, TypeVar

P = TypeVar("P")
LN = TypeVar("LN", bound="ListNode")


class ListNode(Generic[P]):
    """A node in a circular doubly linked list, with an (optional) reference to
    a cache entry.

    The reference should only be `None` for the root node or if the node has
    been removed from the list.
    """

    # We don't use attrs here as in py3.6 you can't have `attr.s(slots=True)`
    # and inherit from `Generic` for some reason
    __slots__ = [
        "cache_entry",
        "prev_node",
        "next_node",
        "_list_lock",
    ]

    def __init__(self, cache_entry: P | None = None) -> None:
        self.cache_entry = cache_entry
        self.prev_node: ListNode[P] | None = None
        self.next_node: ListNode[P] | None = None
        # Per-list lock. Set on root nodes at creation; propagated to
        # non-root nodes when they are inserted into the list.
        self._list_lock: threading.Lock | None = None

    @classmethod
    def create_root_node(cls: type["ListNode[P]"]) -> "ListNode[P]":
        """Create a new linked list by creating a "root" node, which is a node
        that has prev_node/next_node pointing to itself and no associated cache
        entry. The root node owns the per-list lock.
        """
        root = cls()
        root.prev_node = root
        root.next_node = root
        root._list_lock = threading.Lock()
        return root

    @classmethod
    def insert_after(
        cls: type[LN],
        cache_entry: P,
        node: "ListNode[P]",
    ) -> LN:
        """Create a new list node that is placed after the given node.

        Args:
            cache_entry: The associated cache entry.
            node: The existing node in the list to insert the new entry after.
        """
        new_node = cls(cache_entry)
        # Propagate the list lock from the target node.
        lock = node._list_lock
        new_node._list_lock = lock
        if lock is not None:
            with lock:
                new_node._refs_insert_after(node)
        else:
            new_node._refs_insert_after(node)
        return new_node

    def remove_from_list(self) -> None:
        """Remove this node from the list."""
        lock = self._list_lock
        if lock is not None:
            with lock:
                self._refs_remove_node_from_list()
        else:
            self._refs_remove_node_from_list()

        # We drop the reference to the cache entry to break the reference cycle
        # between the list node and cache entry, allowing the two to be dropped
        # immediately rather than at the next GC.
        self.cache_entry = None

    def move_after(self, node: "ListNode[P]") -> None:
        """Move this node from its current location in the list to after the
        given node.
        """
        lock = node._list_lock
        if lock is not None:
            with lock:
                self._move_after_inner(node)
        else:
            self._move_after_inner(node)

    def _move_after_inner(self, node: "ListNode[P]") -> None:
        # We assert that both this node and the target node is still "alive".
        assert self.prev_node
        assert self.next_node
        assert node.prev_node
        assert node.next_node

        assert self is not node

        # Remove self from the list
        self._refs_remove_node_from_list()

        # Insert self back into the list, after target node
        self._refs_insert_after(node)

    def _refs_remove_node_from_list(self) -> None:
        """Internal method to *just* remove the node from the list, without
        e.g. clearing out the cache entry.
        """
        if self.prev_node is None or self.next_node is None:
            # We've already been removed from the list.
            return

        prev_node = self.prev_node
        next_node = self.next_node

        prev_node.next_node = next_node
        next_node.prev_node = prev_node

        # We set these to None so that we don't get circular references,
        # allowing us to be dropped without having to go via the GC.
        self.prev_node = None
        self.next_node = None

    def _refs_insert_after(self, node: "ListNode[P]") -> None:
        """Internal method to insert the node after the given node."""

        # This method should only be called when we're not already in the list.
        assert self.prev_node is None
        assert self.next_node is None

        # We expect the given node to be in the list and thus have valid
        # prev/next refs.
        assert node.next_node
        assert node.prev_node

        prev_node = node
        next_node = node.next_node

        self.prev_node = prev_node
        self.next_node = next_node

        prev_node.next_node = self
        next_node.prev_node = self

    def get_cache_entry(self) -> P | None:
        """Get the cache entry, returns None if this is the root node (i.e.
        cache_entry is None) or if the entry has been dropped.
        """
        return self.cache_entry
