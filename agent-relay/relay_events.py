"""Local operational events: no prompts, answers, commands or credentials."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


EVENT_FILE = Path(__file__).with_name('.agent-relay-events.jsonl')


def record_event(event: str, **fields) -> None:
    try:
        # Keep a bounded local history. Only one relay worker runs at a time.
        if EVENT_FILE.exists() and EVENT_FILE.stat().st_size > 2 * 1024 * 1024:
            EVENT_FILE.replace(EVENT_FILE.with_suffix('.jsonl.1'))
        entry = {'time': datetime.now(timezone.utc).isoformat(), 'event': event, **fields}
        with EVENT_FILE.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=True) + '\n')
    except OSError:
        pass  # Diagnostics must not prevent delivery.
