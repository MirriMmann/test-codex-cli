"""One fresh relay process for acceptance tests; all external transports are fake."""
import contextlib
import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_relay import AgentRelay, Config
from chat_client import ChatClient
from generation import GenerationCancelled
from privacy import PrivacyGuard
import relay_events


def main():
    payload = json.load(sys.stdin)
    relay_events.EVENT_FILE = Path('events.jsonl')
    calls, posts, pending = [], [], list(payload.get('catchup', []))
    counters = {'codex': 0, 'publication': 0}

    class TelegramFixture:
        def call(self, method, data=None, **kwargs):
            if method == 'getMe':
                return {'id': 5, 'username': 'MyBot'}
            if method == 'sendMessage':
                posts.append(data)
                return {'message_id': 10000 + len(posts)}
            if method == 'getUpdates':
                batch, pending[:] = pending[:100], pending[100:]
                return batch
            raise AssertionError('Unexpected Telegram method')

    class ChatFixture:
        def send(self, prompt, **params):
            calls.append({'prompt': prompt, 'params': params})
            if payload.get('fail_chat'):
                raise RuntimeError('synthetic upstream failure')
            return SimpleNamespace(text='ACCEPTANCE_PUBLIC_ANSWER')

    class CodexFixture:
        def send(self, *args, **kwargs):
            counters['codex'] += 1
            raise AssertionError('Discussion acceptance must not execute Codex')

    class PublisherFixture:
        def send(self, *args, **kwargs):
            counters['publication'] += 1
            raise AssertionError('Historical messages must not publish')

    class ChatTransportFixture:
        def send(self, prompt, *, cancel_event=None, **params):
            # The production process boundary consumes this cancellation event;
            # only ChatClient's supported request parameters reach the adapter.
            if cancel_event is not None and cancel_event.is_set():
                raise GenerationCancelled()
            return chat.send(prompt, **params)

    # Suppress only synthetic operational console output; emit one JSON result.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        config = Config.from_env()  # reads only the temporary cwd/.env
        chat = ChatClient(auth_file=config.chat_auth_file, conversation_id=config.chat_conversation_id)
        with patch('relay_cwa.RelayHTTPClient', return_value=ChatFixture()):
            app = AgentRelay(config, TelegramFixture(), CodexFixture(), ChatTransportFixture(),
                             publisher=PublisherFixture(),
                             privacy=PrivacyGuard.configured(config.telegram_token, config.chat_auth_file))
            startup_calls, startup_posts = len(calls), len(posts)
            restored = app.group_context.recent_text(topic_id=7)
            if pending:
                app._catch_up_context()
            for operation in payload.get('operations', []):
                if operation.get('planner'):
                    app._send_chat('EXPLICIT_TASK_ONLY', cancel_event=threading.Event(), isolated=True)
                else:
                    app.process_update(operation)
            result = {'calls': calls, 'posts': posts, 'counters': counters,
                      'startup_calls': startup_calls, 'startup_posts': startup_posts,
                      'restored': restored, 'offset': app.cursor.offset,
                      'intro_pending': app._chat_intro_pending,
                      'tasks': len(app.tasks.owned(-123, 7, 10)),
                      'mode7': app.tasks.mode(-123, 7, 10, default='chat'),
                      'other_person_mode7': app.tasks.mode(-123, 7, 11, default='chat'),
                      'mode8': app.tasks.mode(-123, 8, 10, default='chat')}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
