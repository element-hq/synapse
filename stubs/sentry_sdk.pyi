from __future__ import annotations

from typing import Any

def init(*args: Any, **kwargs: Any) -> None: ...

class Scope:
    @staticmethod
    def get_global_scope() -> Scope: ...
    def set_tag(self, key: str, value: Any) -> None: ...
