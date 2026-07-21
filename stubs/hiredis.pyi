from __future__ import annotations

from typing import Any

class Reader:
    def __init__(
        self,
        encoding: str | None = ...,
        errors: str = ...,
        notEnoughData: Any = ...,
        protocolError: Any = ...,
        replyError: Any = ...,
    ) -> None: ...
    def feed(self, data: bytes) -> None: ...
    def gets(self) -> object: ...
