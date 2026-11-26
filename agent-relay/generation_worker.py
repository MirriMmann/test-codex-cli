"""Private worker protocol; backend credentials and contexts stay separate."""
from __future__ import annotations

import contextlib
import json
import sys


def main() -> None:
    # Read stdin before starting the CLI; the parent can cancel startup as well.
    payload = json.load(sys.stdin)
    from codex_client import CodexCLIClient, CodexError
    from chat_client import ChatClient, ChatError
    from relay_events import record_event
    backend = payload.get('backend', 'codex')
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if backend == 'codex':
                client = CodexCLIClient(**payload['codex'])
            elif backend == 'chat':
                client = ChatClient(**payload['chat'])
                record_event('chat_started', model=payload['params'].get('model') or 'adapter default')
            else:
                raise ChatError('Unknown generation backend')
            result = client.send(payload['prompt'], **payload['params'])
        if backend == 'chat':
            record_event('chat_completed', characters=len(str(result.text or '')))
        response = {'text': str(result.text or '')}
        if backend == 'codex':
            response.update(commands=result.commands, tools_used=result.tools_used)
    except Exception as exc:
        response = {'error': str(exc) if isinstance(exc, (CodexError, ChatError)) else f'{backend} worker failed: {type(exc).__name__}'}
    sys.stdout.write(json.dumps(response, ensure_ascii=True))
    sys.stdout.flush()


if __name__ == '__main__':
    main()
