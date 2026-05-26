#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl_3.0.html>.
#
from pydantic import model_validator
from typing_extensions import Self

from synapse.util.pydantic_models import StrictRootModel


class PolicySignResponse(StrictRootModel[dict[str, str]]):
    """
    Response to `POST /_matrix/policy/v1/sign`
    Spec: https://spec.matrix.org/v1.18/server-server-api/#post_matrixpolicyv1sign

    Example:
        {
          "policy.example.org": {
            "ed25519:policy_server": "zLFxllD0pbBuBpfHh8NuHNaICpReF/PAOpUQTsw+bFGKiGfDNAsnhcP7pbrmhhpfbOAxIdLraQLeeiXBryLmBw"
          }
        }
    """

    @model_validator(mode="after")
    def check_rules(self) -> Self:
        """
        > The Policy Server has signed the event, indicating that it recommends the event for inclusion in the room.
        > Only the Policy Server’s signature is returned.
        > This signature is to be added to the event before sending or processing the event further.
        >
        > `ed25519:policy_server` is always used for Ed25519 signatures.
        > — https://spec.matrix.org/v1.18/server-server-api/#validating-policy-server-signatures
        """

        for key_id in self.root.keys():
            if key_id.startswith("ed25519:") and key_id != "ed25519:policy_server":
                # > `ed25519:policy_server` is always used for Ed25519 signatures.
                raise ValueError(
                    "policy servers must only use ed25519:policy_server for Ed25519 signatures"
                )

        return self
