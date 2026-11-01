"""Plugin interface for future script intelligence - INACTIVE in this release.

Per the approved scope, SubTalk does not import, match or substitute any
prepared speech script. Main Speech Mode only enables interpreter-aware
processing. A future provider can implement ``ScriptContextProvider`` and be
passed to the pipeline; the translator request already carries an optional
``script_context`` field. Substitution of prepared wording must additionally
pass a deterministic matching gate before it may be enabled.
"""
from __future__ import annotations

from typing import Protocol


class ScriptContextProvider(Protocol):
    name: str

    def is_active(self) -> bool: ...

    def context_for(self, text: str, lang: str) -> object | None: ...


class NullScriptContext:
    name = "none"

    def is_active(self) -> bool:
        return False

    def context_for(self, text: str, lang: str) -> None:
        return None
