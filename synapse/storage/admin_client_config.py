import logging
from typing import Optional

from synapse.types import JsonMapping

logger = logging.getLogger(__name__)


class AdminClientConfig:
    """Class to track various Synapse-specific admin-only client-impacting config options."""

    def __init__(self, account_data: Optional[JsonMapping]):
        self.return_soft_failed_events: bool = False

        if account_data:
            self.return_soft_failed_events = account_data.get("return_soft_failed_events", False)
