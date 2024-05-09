from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests.rest.client.test_sendtodevice_base import SendToDeviceTestCaseBase
from tests.unittest import HomeserverTestCase


# Test To-Device messages working correctly with the `/sync/e2ee` endpoint (`to_device`)
class SlidingSyncE2eeSendToDeviceTestCase(SendToDeviceTestCaseBase, HomeserverTestCase):
    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Use the Sliding Sync `/sync/e2ee` endpoint
        self.sync_endpoint = "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee"

    # See SendToDeviceTestCaseBase for tests
