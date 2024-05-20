from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests.rest.client.test_sendtodevice import NotTested as SendToDeviceNotTested
from tests.rest.client.test_sync import NotTested as SyncNotTested


class SlidingSyncE2eeSendToDeviceTestCase(
    SendToDeviceNotTested.SendToDeviceTestCaseBase
):
    """
    Test To-Device messages working correctly with the `/sync/e2ee` endpoint
    (`to_device`)
    """

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        # Use the Sliding Sync `/sync/e2ee` endpoint
        self.sync_endpoint = "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee"

    # See SendToDeviceTestCaseBase for tests


class SlidingSyncE2eeDeviceListSyncTestCase(SyncNotTested.DeviceListSyncTestCaseBase):
    """
    Test device lists working correctly with the `/sync/e2ee` endpoint (`device_lists`)
    """

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        # Use the Sliding Sync `/sync/e2ee` endpoint
        self.sync_endpoint = "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee"

    # See DeviceListSyncTestCaseBase for tests


class SlidingSyncE2eeDeviceOneTimeKeysSyncTestCase(
    SyncNotTested.DeviceOneTimeKeysSyncTestCaseBase
):
    """
    Test device one time keys working correctly with the `/sync/e2ee` endpoint
    (`device_one_time_keys_count`)
    """

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        # Use the Sliding Sync `/sync/e2ee` endpoint
        self.sync_endpoint = "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee"

    # See DeviceOneTimeKeysSyncTestCaseBase for tests


class SlidingSyncE2eeDeviceUnusedFallbackKeySyncTestCase(
    SyncNotTested.DeviceUnusedFallbackKeySyncTestCaseBase
):
    """
    Test device unused fallback key types working correctly with the `/sync/e2ee`
    endpoint (`device_unused_fallback_key_types`)
    """

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        # Use the Sliding Sync `/sync/e2ee` endpoint
        self.sync_endpoint = "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee"

    # See DeviceUnusedFallbackKeySyncTestCaseBase for tests
