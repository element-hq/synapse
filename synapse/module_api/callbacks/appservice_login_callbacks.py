from typing import TYPE_CHECKING, Union


if TYPE_CHECKING:
    from synapse.server import HomeServer
    from synapse.appservice import ApplicationService

class AppserviceLoginModuleApiCallbacks:
    def __init__(self, hs: "HomeServer") -> None:
        self.scheduler = hs.get_application_service_scheduler()

    async def reset_recoverer_backoff(self, appservice: "ApplicationService"):
        print(appservice, "resetting backoff, retrying transactions immediately")

        appservice_id = appservice.id

        if appservice_id not in self.scheduler.txn_ctrl.recoverers:
            print(appservice, "no recoverer found to reset")
            return

        self.scheduler.txn_ctrl.recoverers[appservice_id].reset()
