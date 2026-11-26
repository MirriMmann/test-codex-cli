import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import test_chat_sessions as sessions
from agent_relay import Config
from generation import GenerationCancelled, GenerationFailed


MARKER = '[AgentRelay / server startup context]'


class ChatStartupTests(unittest.TestCase):
    setUp = sessions.ChatSessionTests.setUp
    send = sessions.ChatSessionTests.send
    restart = sessions.ChatSessionTests.restart

    def test_first_human_message_has_briefing_and_question_in_one_call_then_no_repeat(self):
        self.chat.send.assert_not_called()  # no startup greeting request
        self.send('First human question')
        self.chat.send.assert_called_once()
        prompt = self.chat.send.call_args.args[0]
        self.assertEqual(prompt.count(MARKER), 1)
        self.assertIn('@MyBot', prompt)
        self.assertIn('First human question', prompt)
        self.send('Second human question')
        self.assertEqual(self.chat.send.call_count, 2)
        self.assertNotIn(MARKER, self.chat.send.call_args.args[0])
        notes = self.app.chat_sessions.load(-123)[1]
        self.assertNotIn(MARKER, repr(notes))

    def test_restart_reintroduces_same_chat_but_pause_resume_does_not(self):
        self.send('First')
        self.app.paused = True
        self.app._cancel_generation()
        self.app.paused = False
        self.send('After resume')
        self.assertNotIn(MARKER, self.chat.send.call_args.args[0])
        self.restart(sessions.FIRST)
        self.send('After real restart')
        self.assertIn(MARKER, self.chat.send.call_args.args[0])
        self.assertNotIn('selected ChatGPT conversation changed', self.chat.send.call_args.args[0])

    def test_planner_and_peer_turn_do_not_consume_first_human_briefing(self):
        self.app._send_chat('plan only', cancel_event=threading.Event(), isolated=True)
        self.assertEqual(self.chat.send.call_args.args[0], 'plan only')
        self.app._send_chat('Peer public turn', cancel_event=threading.Event(), public_message={
            'text': 'peer', 'from': {'id': 20, 'is_bot': True}})
        self.assertNotIn(MARKER, self.chat.send.call_args.args[0])
        self.send('First human discussion')
        self.assertIn(MARKER, self.chat.send.call_args.args[0])

    def test_intro_and_changed_chat_handover_share_the_same_current_request(self):
        self.send('Previous public context')
        self.restart(sessions.SECOND)
        before = self.chat.send.call_count
        self.send('New current question')
        self.assertEqual(self.chat.send.call_count, before + 1)
        prompt = self.chat.send.call_args.args[0]
        for text in (MARKER, 'selected ChatGPT conversation changed', 'Previous public context', 'New current question'):
            self.assertIn(text, prompt)
        self.send('Next question')
        self.assertNotIn(MARKER, self.chat.send.call_args.args[0])
        self.assertNotIn('selected ChatGPT conversation changed', self.chat.send.call_args.args[0])

    def test_failure_does_not_retry_but_next_explicit_human_request_can_receive_intro(self):
        self.chat.send.side_effect = GenerationFailed('Unavailable')
        self.send('First try')
        self.chat.send.assert_called_once()
        self.assertTrue(self.app._chat_intro_pending)
        self.chat.send.side_effect = None
        self.send('New explicit request')
        self.assertIn(MARKER, self.chat.send.call_args.args[0])
        self.assertFalse(self.app._chat_intro_pending)

    def test_pre_cancelled_and_late_cancelled_requests_do_not_confirm_intro(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(GenerationCancelled):
            self.app._send_chat('cancelled', cancel_event=event, public_message={'from': {'id': 10}})
        self.chat.send.assert_not_called()
        event.clear()
        def late(*args, **kwargs):
            event.set()
            return SimpleNamespace(text='late answer')
        self.chat.send.side_effect = late
        self.app._send_chat('late', cancel_event=event, public_message={'from': {'id': 10}})
        self.assertTrue(self.app._chat_intro_pending)

    def test_custom_reminder_and_names_are_in_intro_without_private_configuration(self):
        self.config = replace(self.config, chat_startup_prompt='Keep explaining calculator percentages.',
                              chat_aliases=('юки', 'чувак'), codex_aliases=('Сударь',))
        self.restart(sessions.FIRST)
        self.send('Question')
        prompt = self.chat.send.call_args.args[0]
        for text in ('Keep explaining calculator percentages.', 'юки', 'чувак', 'Сударь'):
            self.assertIn(text, prompt)
        self.assertNotIn(self.config.telegram_token, prompt)
        self.assertNotIn(self.config.codex_workdir, prompt)
        self.assertNotIn(self.config.chat_conversation_id, prompt)

    def test_confirmed_remote_reply_consumes_intro_even_if_local_notes_save_fails(self):
        with patch.object(self.app.chat_sessions, 'remember', side_effect=OSError()):
            self.send('First')
        self.send('Second')
        self.assertEqual(self.chat.send.call_count, 2)
        self.assertNotIn(MARKER, self.chat.send.call_args.args[0])

    def test_concurrent_human_requests_cannot_both_claim_first_intro(self):
        entered, release = threading.Event(), threading.Event()
        prompts = []
        def backend(prompt, **kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:
                entered.set()
                if not release.wait(3):
                    raise AssertionError('First chat was not released')
            return SimpleNamespace(text='ok')
        self.chat.send.side_effect = backend
        message = {'from': {'id': 10}, 'text': 'public question'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.app._send_chat, 'first', cancel_event=threading.Event(), public_message=message)
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.app._send_chat, 'second', cancel_event=threading.Event(), public_message=message)
            release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual(sum(MARKER in prompt for prompt in prompts), 1)

    def test_custom_reminder_is_loaded_and_bounded_without_echoing_invalid_value(self):
        env = {'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '-123',
               'ALLOWED_USER_IDS': '10', 'CODEX_WORKDIR': '.', 'CHAT_STARTUP_PROMPT': 'Brief operator reminder'}
        with patch.dict(os.environ, env, clear=True), patch('agent_relay.load_dotenv'):
            self.assertEqual(Config.from_env().chat_startup_prompt, 'Brief operator reminder')
        env['CHAT_STARTUP_PROMPT'] = 'private-invalid-' + 'x' * 2000
        with patch.dict(os.environ, env, clear=True), patch('agent_relay.load_dotenv'):
            with self.assertRaises(ValueError) as caught:
                Config.from_env()
        self.assertNotIn('private-invalid', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
