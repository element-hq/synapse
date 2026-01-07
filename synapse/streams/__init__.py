#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from synapse.types import StrCollection, UserID

# The key, this is either a stream token or int.
K = TypeVar("K")
# The return type.
R = TypeVar("R")


class EventSource(ABC, Generic[K, R]):
    @abstractmethod
    async def get_new_events(
        self,
        user: UserID,
        from_key: K,
        limit: int,
        room_ids: StrCollection,
        is_guest: bool,
        explicit_room_id: str | None = None,
    ) -> tuple[list[R], K]:
        raise NotImplementedError()
