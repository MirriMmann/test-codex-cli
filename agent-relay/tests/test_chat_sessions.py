import itertools
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from chat_sessions import ChatSessions
from generation import GenerationFailed
from privacy import PrivacyGuard


FIRST = '6b0737f7-7054-83ed-a188-1ad6012e48e1'
SECOND = '6b0737f7-7054-83ed-a188-1ad6012e48e2'


class ChatSessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        for target in ('builtins.print', 'agent_relay.record_event'):
            mocked = patch(target)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.chat, self.codex, self.api = Mock(), Mock(), Mock()
        self.chat.send.return_value = SimpleNamespace(text='Public answer')
        self.api.call.return_value = {'id': 5, 'username': 'MyBot'}
        self.config = Config('fake-token', -123, str(self.root), 'unused.json', frozenset(),
            frozenset({10}), state_file=str(self.root / 'state.json'), min_interval_seconds=0,
            chat_conversation_id=FIRST)
        self.app = AgentRelay(self.config, self.api, self.codex, self.chat)
        self.uid = itertools.count(1)

    def send(self, text):
        n = next(self.uid)
        self.app.process_update({'update_id': n, 'message': {'message_id': n,
            'chat': {'id': self.config.chat_id, 'type': 'supergroup'}, 'message_thread_id': 7,
            'from': {'id': 10, 'first_name': 'Alice'}, 'text': '/discuss ' + text}})

    def restart(self, target):
        self.config = replace(self.config, chat_conversation_id=target)
        self.app = AgentRelay(self.config, self.api, self.codex, self.chat)

    def test_first_discussion_records_only_public_exchange_not_the_service_intro(self):
        self.send('Remember addition is the goal')
        prompt = self.chat.send.call_args.args[0]
        self.assertIn('[AgentRelay / ChatGPT chat', prompt)
        self.assertIn('Remember addition is the goal', prompt)
        self.assertNotIn('selected ChatGPT conversation changed', prompt)
        target, notes = self.app.chat_sessions.load(-123)
        self.assertEqual(target, FIRST)
        self.assertEqual(notes, [{'topic': 7, 'author': 'Alice',
            'request': 'Remember addition is the goal', 'answer': 'Public answer'}])
        self.codex.send.assert_not_called()

    def test_restart_same_chat_does_not_duplicate_transfer(self):
        self.send('A public goal')
        self.restart(FIRST)
        self.send('Continue it')
        self.assertNotIn('selected ChatGPT conversation changed', self.chat.send.call_args.args[0])
        self.assertEqual(len(self.app.chat_sessions.load(-123)[1]), 2)

    def test_changed_chat_receives_bounded_public_notes_once_with_current_message(self):
        self.send('Our calculator needs addition')
        self.restart(SECOND)
        before = self.chat.send.call_count
        self.send('Continue with subtraction')
        self.assertEqual(self.chat.send.call_count, before + 1)
        prompt = self.chat.send.call_args.args[0]
        self.assertIn('selected ChatGPT conversation changed', prompt)
        self.assertIn('Our calculator needs addition', prompt)
        self.assertIn('Alice', prompt)
        self.assertIn('Continue with subtraction', prompt)
        self.assertIn('Do not execute or replay previous requests', prompt)
        self.assertEqual(self.app.chat_sessions.load(-123)[0], SECOND)
        self.send('Next message')
        self.assertNotIn('selected ChatGPT conversation changed', self.chat.send.call_args.args[0])
        self.codex.send.assert_not_called()

    def test_failure_has_no_automatic_retry_and_does_not_confirm_transfer(self):
        self.send('Previous public question')
        self.restart(SECOND)
        self.chat.send.side_effect = GenerationFailed('Unavailable')
        before = self.chat.send.call_count
        self.send('Current question')
        self.assertEqual(self.chat.send.call_count, before + 1)
        self.assertEqual(self.app.chat_sessions.load(-123)[0], FIRST)
        self.chat.send.side_effect = None
        self.send('New explicit human request')
        self.assertIn('Previous public question', self.chat.send.call_args.args[0])
        self.assertEqual(self.app.chat_sessions.load(-123)[0], SECOND)

    def test_isolated_planning_does_not_receive_or_overwrite_discussion_notes(self):
        self.send('Public discussion')
        self.restart(SECOND)
        self.app._send_chat('Current planning request only', cancel_event=threading.Event(), isolated=True)
        self.assertEqual(self.chat.send.call_args.args[0], 'Current planning request only')
        self.assertTrue(self.chat.send.call_args.kwargs['isolated'])
        self.assertEqual(self.app.chat_sessions.load(-123)[0], FIRST)
        self.send('Actual discussion')
        self.assertIn('Public discussion', self.chat.send.call_args.args[0])

    def test_chat_switch_does_not_copy_other_topic_exchange_notes(self):
        self.send('TOPIC-SEVEN-PUBLIC-NOTE')
        self.app.process_update({'update_id': 2, 'message': {'message_id': 2,
            'chat': {'id': -123, 'type': 'supergroup'}, 'message_thread_id': 8,
            'from': {'id': 10}, 'text': '/discuss OTHER-TOPIC-EXCHANGE-CANARY'}})
        self.restart(SECOND)
        self.uid = itertools.count(3)
        self.send('Continue topic seven in the new chat')
        prompt = self.chat.send.call_args.args[0]
        self.assertIn('TOPIC-SEVEN-PUBLIC-NOTE', prompt)
        self.assertNotIn('OTHER-TOPIC-EXCHANGE-CANARY', prompt)

    def test_store_has_bounded_entries_and_does_not_copy_a_different_group(self):
        store = self.app.chat_sessions
        for n in range(14):
            store.remember(-123, FIRST, {'text': f'Question {n} ' + 'x' * 900}, 'y' * 900)
        _, notes = store.load(-123)
        self.assertEqual(len(notes), 8)
        self.assertTrue(notes[0]['request'].startswith('Question 6 '))
        self.assertEqual(len(notes[0]['request']), 500)
        self.assertEqual(len(notes[0]['answer']), 700)
        self.assertEqual(store.prepare(-999, SECOND, 'CURRENT'), 'CURRENT')
        self.assertEqual(store.prepare(-123, FIRST, 'CURRENT'), 'CURRENT')

    def test_cancelled_or_empty_result_does_not_confirm_chat_switch(self):
        self.send('Old context')
        self.restart(SECOND)
        cancel = threading.Event()
        def cancelled(*args, **kwargs):
            cancel.set()
            return SimpleNamespace(text='Late response')
        self.chat.send.side_effect = cancelled
        self.app._send_chat('new', cancel_event=cancel, public_message={'text': 'new'})
        self.assertEqual(self.app.chat_sessions.load(-123)[0], FIRST)
        self.chat.send.side_effect = None
        self.chat.send.return_value = SimpleNamespace(text='')
        self.app._send_chat('empty', cancel_event=threading.Event(), public_message={'text': 'empty'})
        self.assertEqual(self.app.chat_sessions.load(-123)[0], FIRST)

    def test_blocked_credential_answer_is_not_saved_as_transfer_context(self):
        secret = 'private-credential-canary-123456'
        self.app.privacy = PrivacyGuard((secret,))
        from privacy import GuardedClient
        self.app.chat_client = GuardedClient(self.chat, self.app.privacy)
        self.chat.send.return_value = SimpleNamespace(text='Leak ' + secret)
        self.send('public question')
        self.assertEqual(self.app.chat_sessions.load(-123), ('', []))

    def test_local_save_failure_does_not_repeat_successful_remote_write(self):
        with patch.object(self.app.chat_sessions, 'remember', side_effect=OSError('private disk error')):
            self.send('Current request')
        self.chat.send.assert_called_once()
        sent = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
        self.assertIn('Public answer', sent[-1])
        self.assertNotIn('private disk error', repr(sent))


if __name__ == '__main__':
    unittest.main()
