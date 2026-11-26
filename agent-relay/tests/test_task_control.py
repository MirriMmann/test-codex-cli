import itertools
import tempfile
import threading
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from generation import GenerationCancelled, GenerationFailed
from relay_fakes import DirectTaskRunner


class TaskControlTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for target in ('builtins.print', 'agent_relay.record_event'):
            mocked = patch(target)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.api, self.codex, self.chat = Mock(), Mock(), Mock()
        self.codex.send.return_value = SimpleNamespace(text='code result')
        self.chat.send.return_value = SimpleNamespace(text='chat result')
        self.uid, self.mid = itertools.count(1), itertools.count(1001)
        self.api.call.side_effect = self.api_call
        self.config = Config('test', -123, '.', 'unused.json', frozenset({'peerbot'}),
                             frozenset({10, 11}), frozenset({20}), min_interval_seconds=0,
                             state_file=str(Path(self.folder.name) / 'cursor.json'),
                             codex_aliases=('Сударь',), chat_aliases=('чувак',),
                             default_backend='codex', chat_conversation_id='existing-chat')
        self.app = AgentRelay(self.config, self.api, self.codex, self.chat, orchestrator=DirectTaskRunner(self.codex))

    def api_call(self, method, data=None, **kwargs):
        if method == 'getMe':
            return {'id': 5, 'username': 'MyBot'}
        if method == 'sendMessage':
            return {'message_id': next(self.mid)}
        if method == 'answerCallbackQuery':
            return True
        raise AssertionError('Unexpected method: ' + method)

    def event(self, text, *, user=10, topic=7, reply=None, bot=False, group=-123):
        uid = next(self.uid)
        message = {'message_id': uid, 'chat': {'id': group, 'type': 'supergroup'},
                   'from': {'id': user, 'is_bot': bot, 'username': 'PeerBot' if bot else 'Human'},
                   'text': text}
        if topic:
            message['message_thread_id'] = topic
        if reply:
            message['reply_to_message'] = {'message_id': reply, 'from': {'id': 5}}
        return {'update_id': uid, 'message': message}

    def send(self, text, **kwargs):
        self.app.process_update(self.event(text, **kwargs))

    def texts(self):
        return [call.args[1]['text'] for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']

    def queued(self):
        self.app._executor, self.app._chat_executor = Mock(), Mock()

    def test_mode_commands_are_personal_per_topic_and_persisted(self):
        self.send('/task')
        self.assertEqual(self.app.tasks.mode(-123, 7, 10, default='chat'), 'codex')
        self.send('@MyBot hello', user=11)
        self.send('@MyBot hello', topic=8)
        self.assertEqual(self.chat.send.call_count, 2)
        self.app = AgentRelay(self.config, self.api, self.codex, self.chat, orchestrator=DirectTaskRunner(self.codex))
        self.send('@MyBot make change')
        self.codex.send.assert_called_once()
        self.send('/mode')
        self.assertIn('Задача', self.texts()[-1])

    def test_command_body_overrides_names_and_does_not_require_another_mention(self):
        self.send('/task@MyBot чувак, fix project')
        self.codex.send.assert_called_once()
        self.send('/discuss Сударь, explain this')
        self.chat.send.assert_called_once()
        self.assertEqual(self.app.tasks.mode(-123, 7, 10, default='codex'), 'chat')

    def test_aliases_and_replies_do_not_change_saved_mode(self):
        self.send('/discuss')
        self.send('Сударь, change code')
        code_id = list(self.app.cursor.reply_routes)[-1]
        self.send('continue', reply=int(code_id))
        self.send('@MyBot explain')
        self.assertEqual(self.codex.send.call_count, 2)
        self.chat.send.assert_called_once()
        self.assertEqual(self.app.tasks.mode(-123, 7, 10, default='codex'), 'chat')

    def test_mode_selection_does_not_consume_ambient_group_messages(self):
        self.send('/task')
        self.send('ordinary group discussion')
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()
        self.assertEqual(self.app.tasks.owned(-123, 7, 10), [])

    def test_callbacks_change_clicker_not_original_author(self):
        self.send('/mode')
        query = {'id': 'callback', 'from': {'id': 11, 'is_bot': False}, 'data': 'relay-mode:codex',
                 'message': {'message_id': 1001, 'message_thread_id': 7,
                             'chat': {'id': -123, 'type': 'supergroup'}, 'from': {'id': 5}}}
        self.app.process_update({'update_id': next(self.uid), 'callback_query': query})
        self.assertEqual(self.app.tasks.mode(-123, 7, 11, default='chat'), 'codex')
        self.assertEqual(self.app.tasks.mode(-123, 7, 10, default='chat'), 'chat')
        self.codex.send.assert_not_called()
        self.assertEqual(self.api.call.call_args.args[0], 'answerCallbackQuery')

    def test_other_people_bots_and_other_bots_commands_cannot_change_mode(self):
        for kwargs in ({'user': 99}, {'user': 20, 'bot': True}, {'group': -999}):
            self.send('/task', **kwargs)
        self.send('/task@OtherBot')
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()
        self.assertEqual(self.app.tasks.mode(-123, 7, 10, default='chat'), 'chat')

    def test_untrusted_callback_cannot_change_a_mode(self):
        query = {'id': 'callback', 'from': {'id': 99, 'is_bot': False}, 'data': 'relay-mode:codex',
                 'message': {'message_id': 1001, 'message_thread_id': 7,
                             'chat': {'id': -123, 'type': 'supergroup'}, 'from': {'id': 5}}}
        self.api.call.reset_mock()
        self.app.process_update({'update_id': next(self.uid), 'callback_query': query})
        self.assertEqual(self.app.tasks.mode(-123, 7, 99, default='chat'), 'chat')
        self.api.call.assert_not_called()
        query['from']['id'] = 10
        query['message']['from']['id'] = 99
        self.app.process_update({'update_id': next(self.uid), 'callback_query': query})
        self.assertEqual(self.app.tasks.mode(-123, 7, 10, default='chat'), 'chat')
        self.api.call.assert_not_called()

    def test_disabled_chat_mode_is_reported_without_falling_back_to_codex(self):
        self.app.config = replace(self.config, chat_conversation_id='', chat_aliases=())
        self.send('/discuss question')
        self.assertIn('ChatGPT не настроен', self.texts()[-1])
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()

    def test_status_lists_ambiguous_tasks_without_disclosing_request_or_path(self):
        self.queued()
        self.send('/task private-task-text')
        self.send('/task second-private-text')
        self.send('/status')
        self.assertIn('#1', self.texts()[-1])
        self.assertIn('#2', self.texts()[-1])
        self.send('/status 1')
        self.assertIn('В очереди', self.texts()[-1])
        self.assertNotIn('private-task-text', self.texts()[-1])
        self.assertNotIn(str(Path('.').resolve()), self.texts()[-1])
        self.assertEqual(len(self.app._pending), 2)

    def test_cancel_removes_only_selected_queued_job_without_pausing_bot(self):
        self.queued()
        self.send('/task first')
        first = self.app._pending[0]
        self.send('/task second')
        self.send('/cancel 1')
        self.assertTrue(first.cancelled.is_set())
        self.assertEqual([task.work_id for task in self.app._pending], [2])
        self.assertEqual(self.app.tasks.get(1)['status'], 'cancelled')
        self.assertFalse(self.app.paused)
        self.app._pump_generation()
        self.assertEqual(self.app._active.work_id, 2)

    def test_cancel_cannot_target_another_person_or_topic(self):
        self.queued()
        self.send('/task first')
        self.send('/cancel 1', user=11)
        self.send('/cancel 1', topic=8)
        self.assertEqual(self.app.tasks.get(1)['status'], 'queued')
        self.assertFalse(self.app._pending[0].cancelled.is_set())

    def test_running_cancellation_suppresses_late_result_and_allows_next_task(self):
        self.queued()
        future = Future()
        self.app._executor.submit.return_value = future
        self.send('/task first')
        self.app._pump_generation()
        active = self.app._active
        self.send('/task second')
        self.send('/cancel 1')
        self.assertTrue(active.cancelled.is_set())
        self.assertTrue(self.app.tasks.get(1)['cancel_requested'])
        self.app._pump_generation()
        self.assertIs(self.app._active, active)  # Wait for actual worker exit before next job.
        future.set_result(SimpleNamespace(text='late output must not appear'))
        self.app._executor.submit.return_value = Future()
        self.app._pump_generation()
        self.assertEqual(self.app.tasks.get(1)['status'], 'cancelled')
        self.assertEqual(self.app._active.work_id, 2)
        self.assertNotIn('late output', '\n'.join(self.texts()))

    def test_status_and_mode_work_during_global_pause_without_accepting_task(self):
        self.queued()
        self.send('/task first')
        self.send('/stop')
        self.send('/status 1')
        self.assertIn('Отменена', self.texts()[-1])
        self.send('/task second')
        self.assertIn('Задание не принято', self.texts()[-1])
        self.send('/mode')
        self.assertIn('Задача', self.texts()[-1])
        self.send('/resume')
        self.assertFalse(self.app._pending)
        self.assertEqual(len(self.app.tasks.owned(-123, 7, 10)), 1)

    def test_failure_is_persisted_without_claiming_checks_passed_or_retrying(self):
        self.codex.send.side_effect = GenerationFailed('safe failure')
        self.send('/task first')
        self.send('/status 1')
        self.assertIn('Ошибка', self.texts()[-1])
        self.assertIn('не подтверждены', self.texts()[-1])
        self.codex.send.assert_called_once()

    def test_restart_marks_queue_interrupted_and_keeps_ids(self):
        self.queued()
        self.send('/task first')
        self.app = AgentRelay(self.config, self.api, self.codex, self.chat, orchestrator=DirectTaskRunner(self.codex))
        self.assertEqual(self.app.tasks.get(1)['status'], 'interrupted')
        self.assertFalse(self.app._pending)
        self.send('/task second')
        self.assertEqual(self.app.tasks.get(2)['status'], 'completed')
        self.assertEqual(self.app.tasks.get(2)['verification'], 'not_run')

    def test_discussion_status_and_cancellation_work_while_codex_is_blocked(self):
        started, cancelled, chat_started = threading.Event(), threading.Event(), threading.Event()
        def code(prompt, *, cancel_event, **kwargs):
            started.set()
            if not cancel_event.wait(3):
                raise AssertionError('Cancellation never arrived')
            cancelled.set()
            raise GenerationCancelled()
        def chat(*args, **kwargs):
            chat_started.set()
            return SimpleNamespace(text='parallel chat result')
        self.codex.send.side_effect, self.chat.send.side_effect = code, chat
        self.app.cursor.advance(1)
        polls = 0
        failures = []
        batch = [self.event('/task work'), self.event('чувак, explain')]
        def api(method, data=None, **kwargs):
            nonlocal polls
            if method == 'getWebhookInfo':
                return {}
            if method != 'getUpdates':
                return self.api_call(method, data, **kwargs)
            polls += 1
            try:
                if polls == 1:
                    return batch
                if polls == 2:
                    self.assertTrue(started.wait(2))
                    self.assertTrue(chat_started.wait(2))
                    # Backend completion precedes the durable handover-note
                    # commit; wait for the whole chat future before delivery.
                    if self.app._chat_active is not None:
                        self.app._chat_active.future.result(timeout=2)
                    self.assertFalse(cancelled.is_set())
                    return []
                if polls == 3:
                    self.assertIn('parallel chat result', '\n'.join(self.texts()))
                    return [self.event('/status 1'), self.event('/cancel 1')]
                self.assertTrue(cancelled.wait(2))
            except AssertionError as exc:
                failures.append(exc)
            raise KeyboardInterrupt()
        self.api.call.side_effect = api
        self.app.serve()
        self.assertEqual(failures, [])
        self.assertTrue(cancelled.is_set())
        self.assertIn('Выполняется', '\n'.join(self.texts()))
        self.assertEqual(self.app.tasks.get(1)['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
