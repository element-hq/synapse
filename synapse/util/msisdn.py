#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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

import phonenumbers

from synapse.api.errors import SynapseError


def phone_number_to_msisdn(country: str, number: str) -> str:
    """
    Takes an ISO-3166-1 2 letter country code and phone number and
    returns an msisdn representing the canonical version of that
    phone number.

    As an example, if `country` is "GB" and `number` is "7470674927", this
    function will return "447470674927".

    Args:
        country: ISO-3166-1 2 letter country code
        number: Phone number in a national or international format

    Returns:
        The canonical form of the phone number, as an msisdn.
    Raises:
        SynapseError if the number could not be parsed.
    """
    try:
        phoneNumber = phonenumbers.parse(number, country)
    except phonenumbers.NumberParseException:
        raise SynapseError(400, "Unable to parse phone number")
    return phonenumbers.format_number(phoneNumber, phonenumbers.PhoneNumberFormat.E164)[
        1:
    ]
