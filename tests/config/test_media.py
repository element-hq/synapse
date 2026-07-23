#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.


from synapse.config.homeserver import HomeServerConfig

from tests import unittest


class MediaUploadLimits(unittest.HomeserverTestCase):
    """
    This test case simulates a homeserver with media upload limits configured.
    """

    def test_fallback_template_loaded_without_media_repo(self) -> None:
        """The fallback page template must be loaded even on a worker that does
        not run the media repo, since `build_synapse_client_resource_tree`
        mounts the fallback resource on every worker exposing a C-S API and its
        constructor reads `media_upload_limit_exceeded_template`."""
        config_dict = self.default_config()

        config = HomeServerConfig()
        config.parse_config_dict(
            {
                # A generic worker with the media repo disabled: this takes the
                # early return in `ContentRepositoryConfig.read_config`.
                "worker_app": "synapse.app.generic_worker",
                "enable_media_repo": False,
                **config_dict,
            },
            "",
            "",
        )

        # The media repo isn't loaded on this worker...
        self.assertFalse(config.media.can_load_media_repo)
        # ...but the fallback template must still be available so the resource
        # can be constructed and served.
        self.assertIsNotNone(config.media.media_upload_limit_exceeded_template)
