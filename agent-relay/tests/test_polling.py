import _thread
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import agent_relay
from agent_relay import AgentRelay, Telegram, TelegramAPIError
from bot_instance import AlreadyRunningError, BotInstance


class TelegramTests(unittest.TestCase):
    def test_ctrl_c_interrupts_blocked_network_request_promptly(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_request(*args, **kwargs):
            entered.set()
            release.wait(5)
            return io.BytesIO(b'{"ok":true,"result":[]}')

        def interrupt():
            if entered.wait(2):
                _thread.interrupt_main()

        interrupter = threading.Thread(target=interrupt, daemon=True)
        with patch('urllib.request.urlopen', side_effect=blocked_request):
            started = time.monotonic()
            interrupter.start()
            try:
                with self.assertRaises(KeyboardInterrupt):
                    Telegram('test-token').call('getUpdates', timeout=45)
                self.assertLess(time.monotonic() - started, 1)
            finally:
                release.set()
                interrupter.join(timeout=2)

    def test_http_conflict_reports_code_without_token_or_body(self):
        secret = 'private-bot-token'
        payload = json.dumps({'error_code': 409, 'description': 'private response text'}).encode()
        error = HTTPError('https://api.telegram.org/bot' + secret + '/getUpdates',
                          409, 'Conflict', {}, io.BytesIO(payload))
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(TelegramAPIError) as caught:
                Telegram(secret).call('getUpdates')
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn('polling conflict', str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn('private response', str(caught.exception))

    def test_non_json_http_error_is_safe(self):
        error = HTTPError('https://example.test/secret', 502, 'Bad Gateway', {}, io.BytesIO(b'<html>private</html>'))
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(TelegramAPIError) as caught:
                Telegram('token').call('getUpdates')
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn('private', str(caught.exception))

    def test_rate_limit_parses_retry_after(self):
        body = json.dumps({'error_code': 429, 'parameters': {'retry_after': 9}}).encode()
        error = HTTPError('https://example.test', 429, 'Too Many Requests', {}, io.BytesIO(body))
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(TelegramAPIError) as caught:
                Telegram('token').call('getUpdates')
        self.assertEqual(caught.exception.retry_after, 9)

    def test_json_api_error_is_also_classified(self):
        response = io.BytesIO(b'{"ok":false,"error_code":401}')
        with patch('urllib.request.urlopen', return_value=response):
            with self.assertRaises(TelegramAPIError) as caught:
                Telegram('token').call('getMe')
        self.assertEqual(caught.exception.status_code, 401)

    def app(self, failure):
        app = object.__new__(AgentRelay)
        app.cursor = SimpleNamespace(offset=1)
        app.client = Mock()
        app._active = None
        app._pending = deque()
        app._chat_active = None
        app._chat_pending = deque()
        app.tasks = Mock()
        app.paused = False
        app.api = Mock()
        app.api.call.side_effect = [{}, failure, KeyboardInterrupt()]
        return app

    def test_conflict_and_auth_errors_stop_polling_without_retry(self):
        for code in (400, 401, 403, 404, 409):
            with self.subTest(code=code):
                app = self.app(TelegramAPIError('getUpdates', code))
                with patch('agent_relay.time.sleep') as sleep:
                    with self.assertRaises(TelegramAPIError):
                        app.serve()
                sleep.assert_not_called()
                self.assertEqual(app.api.call.call_count, 2)  # webhook check + one poll
                app.client.send.assert_not_called()

    def test_polling_rate_limit_honors_server_delay(self):
        app = self.app(TelegramAPIError('getUpdates', 429, {'parameters': {'retry_after': 9}}))
        with patch('agent_relay.time.sleep') as sleep, patch('builtins.print'):
            app.serve()
        sleep.assert_called_once_with(9)
        self.assertEqual(app.api.call.call_count, 3)
        app.client.send.assert_not_called()


class InstanceTests(unittest.TestCase):
    def test_other_bots_can_run_and_lock_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with self.assertRaisesRegex(ValueError, 'test failure'):
                with BotInstance('one', directory), BotInstance('two', directory):
                    with self.assertRaises(AlreadyRunningError):
                        with BotInstance('one', directory):
                            self.fail('duplicate lock acquired')
                    raise ValueError('test failure')
            with BotInstance('one', directory):
                pass

    def test_lock_blocks_a_second_process_and_can_be_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            script = (
                'import sys; from pathlib import Path; '
                'from bot_instance import BotInstance, AlreadyRunningError\n'
                'try:\n'
                ' with BotInstance("test-token", Path(sys.argv[1])): pass\n'
                'except AlreadyRunningError: sys.exit(42)\n'
            )
            command = [sys.executable, '-c', script, folder]
            with BotInstance('test-token', Path(folder)):
                result = subprocess.run(command, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 42, result.stderr.decode())
            result = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_duplicate_startup_exits_before_telegram_or_codex_turn(self):
        with patch.object(agent_relay.Config, 'from_env'), \
             patch.object(agent_relay, 'validate_workspace'), \
             patch.object(agent_relay, 'resolve_codex_binary'), \
             patch.object(agent_relay, 'check_login'), \
             patch.object(agent_relay, 'BotInstance', side_effect=AlreadyRunningError('already running')), \
             patch.object(agent_relay, 'Telegram') as telegram, \
             patch('generation.ProcessClient') as cwa, \
             patch('builtins.print'):
            with self.assertRaises(SystemExit) as caught:
                agent_relay.main()
        self.assertEqual(caught.exception.code, 1)
        telegram.assert_not_called()
        cwa.assert_not_called()


if __name__ == '__main__':
    unittest.main()
