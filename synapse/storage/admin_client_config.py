import logging

from synapse.types import JsonMapping

logger = logging.getLogger(__name__)


class AdminClientConfig:
    """Class to track various Synapse-specific admin-only client-impacting config options."""

    def __init__(self, account_data: JsonMapping | None):
        # Allow soft-failed events to be returned down `/sync` and other
        # client APIs. `io.element.synapse.soft_failed: true` is added to the
        # `unsigned` portion of the event to inform clients that the event
        # is soft-failed.
        self.return_soft_failed_events: bool = False
        self.return_policy_server_spammy_events: bool = False

        if account_data:
            self.return_soft_failed_events = account_data.get(
                "return_soft_failed_events", False
            )
            self.return_policy_server_spammy_events = account_data.get(
                "return_policy_server_spammy_events", self.return_soft_failed_events
            )
