"""Choose one backend from explicit names or the engine of a replied message."""
from __future__ import annotations

import re


def choose_backend(text: str, codex_aliases: tuple[str, ...], chat_aliases: tuple[str, ...],
                   *, default: str = 'codex', reply_backend: str | None = None) -> str:
    matches = []
    for backend, aliases in (('codex', codex_aliases), ('chat', chat_aliases)):
        for alias in aliases:
            found = re.search(r'(?<![\w@])' + re.escape(alias) + r'(?!\w)', text, re.I)
            if found:
                matches.append((found.start(), backend))
    if matches:
        return min(matches, key=lambda value: value[0])[1]
    return reply_backend if reply_backend in ('codex', 'chat') else default
