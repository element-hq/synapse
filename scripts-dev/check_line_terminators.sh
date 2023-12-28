#!/bin/bash
##
## This file is licensed under the Affero General Public License (AGPL) version 3.
##
## Copyright (C) 2023 New Vector, Ltd
##
## This program is free software: you can redistribute it and/or modify
## it under the terms of the GNU Affero General Public License as
## published by the Free Software Foundation, either version 3 of the
## License, or (at your option) any later version.
##
## See the GNU Affero General Public License for more details:
## <https://www.gnu.org/licenses/agpl-3.0.html>.
##
## Originally licensed under the Apache License, Version 2.0:
## <http://www.apache.org/licenses/LICENSE-2.0>.
##
## [This file includes modifications made by New Vector Limited]
##
##

# cd to the root of the repository
cd "$(dirname "$0")/.." || exit

# Find and print files with non-unix line terminators
if find . -path './.git/*' -prune -o -type f -print0 | xargs -0 grep -I -l $'\r$'; then
    echo -e '\e[31mERROR: found files with CRLF line endings. See above.\e[39m'
    exit 1
fi
