#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 Openmarket
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

from typing import Any

from synapse.types import JsonDict
from synapse.util.module_loader import load_module

from ._base import Config

LDAP_PROVIDER = "ldap_auth_provider.LdapAuthProvider"


class PasswordAuthProviderConfig(Config):
    section = "authproviders"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        """Parses the old password auth providers config. The config format looks like this:

        password_providers:
           # Example config for an LDAP auth provider
           - module: "ldap_auth_provider.LdapAuthProvider"
             config:
               enabled: true
               uri: "ldap://ldap.example.com:389"
               start_tls: true
               base: "ou=users,dc=example,dc=com"
               attributes:
                  uid: "cn"
                  mail: "email"
                  name: "givenName"
               #bind_dn:
               #bind_password:
               #filter: "(objectClass=posixAccount)"

        We expect admins to use modules for this feature (which is why it doesn't appear
        in the sample config file), but we want to keep support for it around for a bit
        for backwards compatibility.
        """

        self.password_providers: list[tuple[type, Any]] = []
        providers = []

        # We want to be backwards compatible with the old `ldap_config`
        # param.
        ldap_config = config.get("ldap_config", {})
        if ldap_config.get("enabled", False):
            providers.append({"module": LDAP_PROVIDER, "config": ldap_config})

        providers.extend(config.get("password_providers") or [])
        for i, provider in enumerate(providers):
            mod_name = provider["module"]

            # This is for backwards compat when the ldap auth provider resided
            # in this package.
            if mod_name == "synapse.util.ldap_auth_provider.LdapAuthProvider":
                mod_name = LDAP_PROVIDER

            (provider_class, provider_config) = load_module(
                {"module": mod_name, "config": provider["config"]},
                ("password_providers", "<item %i>" % i),
            )

            self.password_providers.append((provider_class, provider_config))
