from typing import Any

from synapse.types import JsonDict

from ._base import Config


class CUSTOMJWTConfig(Config):
    section = "custom_jwt"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        custom_jwt_config = config.get("custom_jwt", None)
        if custom_jwt_config:
            self.custom_jwt_enabled = custom_jwt_config.get("enabled", False)
            self.custom_jwt_algorithm = custom_jwt_config["algorithm"]
            # The issuer and audiences are optional, if provided, it is asserted
            # that the claims exist on the JWT.
            self.custom_jwt_issuer = custom_jwt_config.get("issuer")
            self.custom_jwt_audiences = custom_jwt_config.get("audiences")
            # check_requirements("jwt")
        else:
            self.custom_jwt_enabled = False
            self.custom_jwt_algorithm = None
            self.custom_jwt_issuer = None
            self.custom_jwt_audiences = None
