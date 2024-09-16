#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C
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

from typing import TYPE_CHECKING, Dict, FrozenSet, List, Tuple, cast

from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main import CacheInvalidationWorkerStore
from synapse.util.caches.descriptors import cached

if TYPE_CHECKING:
    from synapse.rest.admin.experimental_features import ExperimentalFeature
    from synapse.server import HomeServer


class ExperimentalFeaturesStore(CacheInvalidationWorkerStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ) -> None:
        super().__init__(database, db_conn, hs)

    @cached()
    async def list_enabled_features(self, user_id: str) -> FrozenSet[str]:
        """
        Checks to see what features are enabled for a given user
        Args:
            user:
                the user to be queried on
        Returns:
            the features currently enabled for the user
        """
        enabled = cast(
            List[Tuple[str]],
            await self.db_pool.simple_select_list(
                table="per_user_experimental_features",
                keyvalues={"user_id": user_id, "enabled": True},
                retcols=("feature",),
            ),
        )

        return frozenset(feature[0] for feature in enabled)

    async def set_features_for_user(
        self,
        user: str,
        features: Dict["ExperimentalFeature", bool],
    ) -> None:
        """
        Enables or disables features for a given user
        Args:
            user:
                the user for whom to enable/disable the features
            features:
                pairs of features and True/False for whether the feature should be enabled
        """

        def set_features_for_user_txn(txn: LoggingTransaction) -> None:
            for feature, enabled in features.items():
                self.db_pool.simple_upsert_txn(
                    txn,
                    table="per_user_experimental_features",
                    keyvalues={"feature": feature, "user_id": user},
                    values={"enabled": enabled},
                    insertion_values={"user_id": user, "feature": feature},
                )

                self._invalidate_cache_and_stream(
                    txn, self.is_feature_enabled, (user, feature)
                )

            self._invalidate_cache_and_stream(txn, self.list_enabled_features, (user,))

        return await self.db_pool.runInteraction(
            "set_features_for_user", set_features_for_user_txn
        )

    @cached()
    async def is_feature_enabled(
        self, user_id: str, feature: "ExperimentalFeature"
    ) -> bool:
        """
        Checks to see if a given feature is enabled for the user
        Args:
            user_id: the user to be queried on
            feature: the feature in question
        Returns:
                True if the feature is enabled, False if it is not or if the feature was
                not found.
        """

        if feature.is_globally_enabled(self.hs.config):
            return True

        # if it's not enabled globally, check if it is enabled per-user
        res = await self.db_pool.simple_select_one_onecol(
            table="per_user_experimental_features",
            keyvalues={"user_id": user_id, "feature": feature},
            retcol="enabled",
            allow_none=True,
            desc="get_feature_enabled",
        )

        # None and false are treated the same
        db_enabled = bool(res)

        return db_enabled
