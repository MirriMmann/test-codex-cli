import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config, GenerationTask
from generation import GenerationCancelled
from relay_fakes import DirectTaskRunner


class StopTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api, self.client = Mock(), Mock()
        self.api.call.return_value = {'id': 5, 'username': 'MyBot'}
        self.client.send.return_value = SimpleNamespace(text='answer')
        self.config = Config('test-token', -123, 'conversation', 'auth.json', frozenset({'peerbot'}),
                             frozenset({10}), frozenset({20}), min_interval_seconds=0,
                             state_file=str(Path(self.folder.name) / 'state.json'), bot_aliases=('Mir',))
        with patch('builtins.print'):
            self.app = AgentRelay(self.config, self.api, self.client, orchestrator=DirectTaskRunner(self.client))

    def event(self, text, uid=1, sender=None, group=-123):
        return {'update_id': uid, 'message': {
            'message_id': uid, 'chat': {'id': group, 'type': 'supergroup'}, 'text': text,
            'from': sender or {'id': 10, 'is_bot': False},
        }}

    def posted(self):
        return [call.args[1]['text'] for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']

    def test_stop_pauses_aliases_and_handoffs_until_resume(self):
        self.app.process_update(self.event('/stop@mybot'))
        self.app.process_update(self.event('Mir, answer', 2))
        self.app.process_update(self.event('/relay@MyBot continue', 3,
                                           {'id': 20, 'username': 'PeerBot', 'is_bot': True}))
        self.assertTrue(self.app.paused)
        self.client.send.assert_not_called()
        self.assertEqual(len(self.posted()), 1)
        self.app.process_update(self.event('/resume@MyBot', 4))
        with patch('builtins.print'):
            self.app.process_update(self.event('Mir, answer', 5))
        self.assertFalse(self.app.paused)
        self.client.send.assert_called_once()
        self.assertTrue(self.posted()[-1].startswith('codex\n\nЗадача #1'))
        self.assertTrue(self.posted()[-1].endswith('\n\nanswer'))

    def test_only_allowed_humans_in_this_group_can_control_bot(self):
        for sender, group, text in (
            ({'id': 99, 'is_bot': False}, -123, '/stop'),
            ({'id': 20, 'username': 'PeerBot', 'is_bot': True}, -123, '/stop'),
            ({'id': True, 'is_bot': False}, -123, '/stop'),
            ({'id': 10, 'is_bot': False}, -999, '/stop'),
            ({'id': 10, 'is_bot': False}, -123, '/stop@OtherBot Mir'),
        ):
            self.app.process_update(self.event(text, sender=sender, group=group, uid=self.app.cursor.offset))
        anonymous = self.event('/stop', uid=self.app.cursor.offset)
        anonymous['message']['sender_chat'] = {'id': -123}
        self.app.process_update(anonymous)
        self.assertFalse(self.app.paused)
        self.assertEqual(self.posted(), [])
        self.client.send.assert_not_called()

    def test_stop_discards_queue_and_late_result_even_after_resume(self):
        task = GenerationTask(1, self.event('Mir')['message'], 'prompt', {})
        task.future = Future()
        self.app._active = task
        self.app._pending.append(GenerationTask(2, {}, 'queued', {}))
        self.app.process_update(self.event('/stoprelay', 3))
        self.assertTrue(task.cancelled.is_set())
        self.assertFalse(self.app._pending)
        self.app.process_update(self.event('/resume', 4))
        task.future.set_result(SimpleNamespace(text='/relay@PeerBot late answer'))
        self.app._pump_generation()
        self.assertIsNone(self.app._active)
        self.assertEqual(len(self.posted()), 2)  # Only stop/resume confirmations.
        self.client.send.assert_not_called()

    def test_serve_receives_stop_while_generation_is_blocked(self):
        started, cancelled = threading.Event(), threading.Event()
        def blocked_send(prompt, *, cancel_event, **params):
            started.set()
            if not cancel_event.wait(5):
                raise AssertionError('Stop command did not reach the running generation')
            cancelled.set()
            raise GenerationCancelled()
        self.client.send.side_effect = blocked_send
        self.app.cursor.advance(1)
        polls = []
        def api_call(method, data=None, **kwargs):
            if method == 'getWebhookInfo':
                return {}
            if method == 'sendMessage':
                return {}
            polls.append(data)
            if len(polls) == 1:
                return [self.event('Mir, answer', 1)]
            if len(polls) == 2:
                self.assertTrue(started.wait(2))
                return [self.event('/stop', 2)]
            self.assertTrue(cancelled.wait(2))
            raise KeyboardInterrupt()
        self.api.call.side_effect = api_call
        with patch('builtins.print'):
            self.app.serve()
        self.assertTrue(self.app.paused)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(polls[1]['timeout'], 1)
        self.assertEqual(len(self.posted()), 2)
        self.assertIn('/resume', self.posted()[-1])

    def test_ctrl_c_cancels_active_generation_and_discards_queue_without_confirmation(self):
        started, cancelled = threading.Event(), threading.Event()

        def blocked_send(prompt, *, cancel_event, **params):
            started.set()
            if not cancel_event.wait(2):
                raise AssertionError('Ctrl+C did not cancel generation')
            cancelled.set()
            raise GenerationCancelled()

        self.client.send.side_effect = blocked_send
        self.app.cursor.advance(1)
        polls = []

        def api_call(method, data=None, **kwargs):
            if method == 'getWebhookInfo':
                return {}
            if method == 'sendMessage':
                return {}
            polls.append(data)
            if len(polls) == 1:
                return [self.event('Mir, answer', 1), self.event('Mir, queued', 2)]
            self.assertTrue(started.wait(2))
            raise KeyboardInterrupt()

        self.api.call.side_effect = api_call
        with patch('builtins.print') as output:
            self.app.serve()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.app._pending)
        self.assertIsNone(self.app._executor)
        self.assertEqual(len(self.posted()), 2)  # Queue acknowledgements, no late answers.
        self.assertNotIn('Stopped.', [call.args[0] for call in output.call_args_list])

    def test_queued_turn_waits_for_cooldown_instead_of_being_discarded(self):
        self.app.config = SimpleNamespace(min_interval_seconds=2)
        self.app._executor = Mock()
        self.app.last_send_time = 100
        task = GenerationTask(1, {}, 'prompt', {})
        self.app._pending.append(task)
        with patch('agent_relay.time.monotonic', return_value=101):
            self.app._pump_generation()
        self.app._executor.submit.assert_not_called()
        self.assertEqual(len(self.app._pending), 1)
        with patch('agent_relay.time.monotonic', return_value=102):
            self.app._pump_generation()
        self.app._executor.submit.assert_called_once()
        self.assertIs(self.app._active, task)

    def test_stop_interrupts_remaining_parts_of_a_long_reply(self):
        task = GenerationTask(1, self.event('Mir')['message'], 'prompt', {})
        task.future = Future()
        task.future.set_result(SimpleNamespace(text='x' * 9000))
        self.app._active = task
        self.app._pump_generation()
        self.assertEqual(len(self.posted()), 1)
        self.assertEqual(len(self.posted()[0]), 3900)
        self.assertIs(self.app._active, task)
        self.app.process_update(self.event('/stop', 2))
        self.app._pump_generation()
        self.assertIsNone(self.app._active)
        self.assertEqual(len(self.posted()), 2)  # First part + stop confirmation.

    def test_failed_generation_does_not_prevent_next_queued_turn(self):
        task = GenerationTask(1, self.event('Mir')['message'], 'first', {})
        task.future = Future()
        task.future.set_exception(RuntimeError('request failed'))
        self.app._active = task
        next_task = GenerationTask(2, self.event('Mir', 2)['message'], 'next', {})
        self.app._pending.append(next_task)
        self.app._executor = Mock()
        with patch('builtins.print'):
            self.app._pump_generation()
        self.assertIs(self.app._active, next_task)
        self.app._executor.submit.assert_called_once_with(self.app._send_generation, next_task)


if __name__ == '__main__':
    unittest.main()
