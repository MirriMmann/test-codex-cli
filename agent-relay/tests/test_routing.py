import itertools
import json
import os
import tempfile
import unittest
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config, Cursor, GenerationTask
from generation import GenerationFailed
from routing import choose_backend
from relay_fakes import DirectTaskRunner


class AliasRoutingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for target in ('builtins.print', 'agent_relay.record_event'):
            mock = patch(target)
            mock.start()
            self.addCleanup(mock.stop)
        self.api, self.codex, self.chat = Mock(), Mock(), Mock()
        self.codex.send.return_value = SimpleNamespace(text='project result')
        self.chat.send.return_value = SimpleNamespace(text='ordinary chat answer')
        self.sent_ids = itertools.count(1001)
        self.uid = itertools.count(1)
        self.api.call.side_effect = self.api_call
        self.config = Config(
            'fake-token', -123, '.', 'unused-session.json', frozenset({'peerbot'}),
            frozenset({10}), frozenset({20}), min_interval_seconds=0,
            state_file=str(Path(self.folder.name) / 'state.json'), codex_allow_push=True,
            codex_aliases=('Сударь',), chat_aliases=('чувак',), default_backend='chat',
            chat_conversation_id='existing-chat',
        )
        self.app = self.new_app()

    def api_call(self, method, data=None, **kwargs):
        if method == 'getMe':
            return {'id': 5, 'username': 'MyBot'}
        if method == 'sendMessage':
            return {'message_id': next(self.sent_ids)}
        raise AssertionError('Unexpected Telegram method: ' + method)

    def new_app(self):
        return AgentRelay(self.config, self.api, self.codex, chat_client=self.chat,
                          orchestrator=DirectTaskRunner(self.codex))

    def event(self, text, *, sender=None, reply=None, group=-123):
        uid = next(self.uid)
        message = {'message_id': uid, 'chat': {'id': group, 'type': 'supergroup'},
                   'text': text, 'from': sender if sender is not None else {'id': 10, 'is_bot': False}}
        if reply is not None:
            message['reply_to_message'] = {'message_id': reply, 'from': {'id': 5}, 'text': 'earlier answer'}
        return {'update_id': uid, 'message': message}

    def test_names_select_exactly_one_backend(self):
        self.app.process_update(self.event('Сударь, создай калькулятор'))
        self.codex.send.assert_called_once()
        self.chat.send.assert_not_called()
        self.assertIn('Codex CLI', self.codex.send.call_args.args[0])
        self.app.process_update(self.event('чувак, объясни арифметику'))
        self.chat.send.assert_called_once()
        self.codex.send.assert_called_once()
        self.assertIn('ChatGPT chat', self.chat.send.call_args.args[0])

    def test_source_label_comes_from_route_even_if_model_claims_another_source(self):
        for backend, client, alias, label, claimed_source in (
            ('codex', self.codex, self.config.codex_aliases[0], 'codex', 'caatgpt'),
            ('chat', self.chat, self.config.chat_aliases[0], 'ChatGPT', 'codex'),
        ):
            with self.subTest(backend=backend):
                self.api.call.reset_mock()
                client.send.return_value = SimpleNamespace(text=claimed_source + '\nanswer')
                self.app.process_update(self.event(alias + ', answer'))
                posted = [call.args[1]['text'] for call in self.api.call.call_args_list
                          if call.args[0] == 'sendMessage']
                self.assertEqual(len(posted), 1)
                self.assertTrue(posted[0].startswith(label + '\n\n'))
                payload = posted[0].split('\n\n', 2)[2] if backend == 'codex' else posted[0].split('\n\n', 1)[1]
                self.assertEqual(payload, claimed_source + '\nanswer')

    def test_every_long_reply_part_has_source_label_within_telegram_limit(self):
        for client, alias, label in (
            (self.codex, self.config.codex_aliases[0], 'codex'),
            (self.chat, self.config.chat_aliases[0], 'ChatGPT'),
        ):
            with self.subTest(label=label):
                self.api.call.reset_mock()
                body = '\U0001f680' * 4500
                client.send.return_value = SimpleNamespace(text=body)
                self.app.process_update(self.event(alias + ', answer'))
                posted = [call.args[1]['text'] for call in self.api.call.call_args_list
                          if call.args[0] == 'sendMessage']
                prefix = label + '\n\n'
                self.assertGreater(len(posted), 1)
                self.assertTrue(all(part.startswith(prefix) for part in posted))
                self.assertTrue(all(len(part.encode('utf-16-le')) // 2 <= 3900 for part in posted))
                payloads = [part.split('\n\n', 2)[2] if label == 'codex' else part[len(prefix):]
                            for part in posted]
                self.assertEqual(''.join(payloads), body)

    def test_alias_case_punctuation_and_word_boundaries(self):
        for text in ('СУДАРЬ!', '(сударь), пожалуйста'):
            self.assertEqual(choose_backend(text, ('Сударь',), ('чувак',), default='chat'), 'codex')
        for text in ('ЧУВАК?', '«Чувак»'):
            self.assertEqual(choose_backend(text, ('Сударь',), ('чувак',), default='codex'), 'chat')
        for text in ('государь', 'сударь123', 'мой_Сударь', '@Сударь', 'чуваки', '@чувак'):
            with self.subTest(text=text):
                self.app.process_update(self.event(text))
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()

    def test_both_names_route_once_using_the_first_name(self):
        self.app.process_update(self.event('чувак, спроси Сударя; Сударь, ответь'))
        self.chat.send.assert_called_once()
        self.codex.send.assert_not_called()
        self.app.process_update(self.event('Сударь, спроси чувак, пожалуйста'))
        self.codex.send.assert_called_once()
        self.chat.send.assert_called_once()

    def test_plain_mention_uses_chat_default(self):
        self.app.process_update(self.event('@MyBot, привет'))
        self.chat.send.assert_called_once()
        self.codex.send.assert_not_called()

    def test_reply_keeps_backend_after_restart(self):
        self.app.process_update(self.event('Сударь, поправь проект'))
        self.assertEqual(Cursor(self.config.state_file).reply_routes['1001'], 'codex')
        self.app = self.new_app()
        self.app.process_update(self.event('продолжай', reply=1001))
        self.assertEqual(self.codex.send.call_count, 2)
        self.chat.send.assert_not_called()
        self.app.process_update(self.event('чувак, привет'))
        self.assertEqual(Cursor(self.config.state_file).reply_routes['1003'], 'chat')
        self.app = self.new_app()
        self.app.process_update(self.event('расскажи подробнее', reply=1003))
        self.assertEqual(self.chat.send.call_count, 2)

    def test_explicit_alias_overrides_reply_backend(self):
        self.app.cursor.remember_reply(500, 'codex')
        self.app.cursor.remember_reply(501, 'chat')
        self.app.process_update(self.event('чувак, объясни', reply=500))
        self.chat.send.assert_called_once()
        self.codex.send.assert_not_called()
        self.app.process_update(self.event('Сударь, измени', reply=501))
        self.codex.send.assert_called_once()

    def test_model_effort_and_publish_capability_stay_with_backend(self):
        self.app.config = replace(self.config, model='gpt-6.1-terra', effort='medium',
                                  chat_model='gpt-6-instant', chat_effort='low')
        self.app.process_update(self.event('Сударь, измени'))
        codex_params = self.codex.send.call_args.kwargs
        self.assertEqual(codex_params['model'], 'gpt-6.1-terra')
        self.assertEqual(codex_params['reasoning_effort'], 'medium')
        self.assertNotIn('allow_publish', codex_params)
        self.app.process_update(self.event('чувак, расскажи'))
        chat_params = self.chat.send.call_args.kwargs
        self.assertEqual(chat_params['model'], 'gpt-6-instant')
        self.assertEqual(chat_params['reasoning_effort'], 'low')
        self.assertNotIn('allow_publish', chat_params)
        peer = {'id': 20, 'username': 'PeerBot', 'is_bot': True}
        self.app.process_update(self.event('Сударь, помоги', sender=peer))
        self.assertNotIn('allow_publish', self.codex.send.call_args.kwargs)
        self.app.process_update(self.event('чувак, помоги', sender=peer))
        self.assertNotIn('allow_publish', self.chat.send.call_args.kwargs)

    def test_empty_models_are_omitted_for_each_backend(self):
        for text, client in (('Сударь, ответь', self.codex), ('чувак, ответь', self.chat)):
            self.app.process_update(self.event(text))
            self.assertNotIn('model', client.send.call_args.kwargs)
            self.assertNotIn('reasoning_effort', client.send.call_args.kwargs)

    def test_chat_failure_never_falls_back_to_codex_or_replays(self):
        self.chat.send.side_effect = GenerationFailed('ChatGPT unavailable')
        event = self.event('чувак, привет')
        self.app.process_update(event)
        self.app.process_update(event)
        self.chat.send.assert_called_once()
        self.codex.send.assert_not_called()
        self.assertFalse(self.app.cursor.reply_routes)

    def test_missing_chat_transport_never_uses_codex(self):
        self.app.chat_client = None
        self.app.process_update(self.event('чувак, привет'))
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()

    def test_stop_pauses_both_names_until_resume(self):
        self.app.process_update(self.event('/stop'))
        self.app.process_update(self.event('Сударь, работай'))
        self.app.process_update(self.event('чувак, ответь'))
        self.assertTrue(self.app.paused)
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()
        self.app.process_update(self.event('/resume'))
        self.app.process_update(self.event('Сударь, работай'))
        self.app.process_update(self.event('чувак, ответь'))
        self.assertFalse(self.app.paused)
        self.codex.send.assert_called_once()
        self.chat.send.assert_called_once()

    def test_stop_cancels_either_active_backend_and_discards_other_queue(self):
        for backend in ('codex', 'chat'):
            with self.subTest(backend=backend):
                task = GenerationTask(99, self.event('question')['message'], 'prompt', {}, backend=backend)
                task.future = Future()
                self.app._active = task
                self.app._pending.append(GenerationTask(100, {}, 'queued', {}, backend='chat' if backend == 'codex' else 'codex'))
                self.app.process_update(self.event('/stop'))
                self.assertTrue(task.cancelled.is_set())
                self.assertFalse(self.app._pending)
                self.app.process_update(self.event('/resume'))
                task.future.set_result(SimpleNamespace(text='late result'))
                self.app._pump_generation()
                self.assertIsNone(self.app._active)
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()

    def test_allowlists_apply_to_both_backend_aliases(self):
        invalid_senders = ({'id': 99, 'is_bot': False},
                           {'id': True, 'is_bot': False},
                           {'id': 5, 'is_bot': True, 'username': 'MyBot'},
                           {'id': 99, 'is_bot': True, 'username': 'PeerBot'},
                           {'id': 20, 'is_bot': True, 'username': 'OtherBot'})
        for text in ('Сударь, ответь', 'чувак, ответь'):
            for sender in invalid_senders:
                self.app.process_update(self.event(text, sender=sender))
            self.app.process_update(self.event(text, group=-999))
            anonymous = self.event(text)
            anonymous['message']['sender_chat'] = {'id': -123}
            self.app.process_update(anonymous)
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()


class ReplyIndexTests(unittest.TestCase):
    def test_reply_index_is_bounded_and_contains_no_chat_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            cursor = Cursor(str(path))
            cursor.advance(20)
            for message_id in range(1, 205):
                cursor.remember_reply(message_id, 'chat' if message_id % 2 else 'codex')
            restored = Cursor(str(path))
            self.assertEqual(restored.offset, 20)
            self.assertEqual(len(restored.reply_routes), 200)
            self.assertNotIn('4', restored.reply_routes)
            self.assertEqual(restored.reply_routes['5'], 'chat')
            self.assertEqual(restored.reply_routes['204'], 'codex')
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(set(data), {'offset', 'reply_routes'})

    def test_legacy_cursor_loads_without_reply_index(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            path.write_text('{"offset": 17}', encoding='utf-8')
            cursor = Cursor(str(path))
            self.assertEqual(cursor.offset, 17)
            self.assertEqual(cursor.reply_routes, {})


class RoutingConfigTests(unittest.TestCase):
    def config(self, **overrides):
        values = {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '-123',
                  'ALLOWED_USER_IDS': '10', 'CODEX_WORKDIR': '.', **overrides}
        with patch.dict(os.environ, values, clear=True), patch('agent_relay.load_dotenv'):
            return Config.from_env()

    def test_names_and_chat_parameters_are_separate_from_codex(self):
        config = self.config(CODEX_ALIASES='Сударь, сэр', CHAT_ALIASES='чувак',
                             DEFAULT_BACKEND='chat', CWA_CONVERSATION_ID='https://chatgpt.com/c/6b0737f7-7054-83ed-a188-1ad6012e48e1',
                             CWA_MODEL='gpt-6-instant', CWA_REASONING_EFFORT='low',
                             CODEX_MODEL='gpt-6.1-terra', CODEX_REASONING_EFFORT='medium')
        self.assertEqual(config.codex_aliases, ('Сударь', 'сэр'))
        self.assertEqual(config.chat_aliases, ('чувак',))
        self.assertEqual(config.chat_model, 'gpt-6-instant')
        self.assertEqual(config.model, 'gpt-6.1-terra')
        self.assertEqual(config.chat_effort, 'low')
        self.assertEqual(config.effort, 'medium')
        self.assertTrue(config.chat_enabled)
        self.assertEqual(config.chat_conversation_id, '6b0737f7-7054-83ed-a188-1ad6012e48e1')

    def test_ambiguous_names_and_missing_chat_config_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'different names'):
            self.config(CODEX_ALIASES='Сударь', CHAT_ALIASES='СУДАРЬ', CWA_CONVERSATION_ID='existing-chat')
        for values in ({'CHAT_ALIASES': 'чувак'}, {'DEFAULT_BACKEND': 'chat'}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, 'CWA_CONVERSATION_ID'):
                self.config(**values)


if __name__ == '__main__':
    unittest.main()
