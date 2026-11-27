import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config, Cursor, is_addressed, make_prompt, numeric_ids, split_for_telegram, usernames
from relay_fakes import DirectTaskRunner


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.human = {'id': 10, 'first_name': 'Human', 'is_bot': False}
        self.peer = {'id': 20, 'username': 'FriendBot', 'is_bot': True}
        self.message = {'chat': {'id': -123, 'type': 'supergroup'}, 'message_id': 7, 'from': self.human, 'text': 'hey @MyBot!'}
        self.peers = usernames('FriendBot')
        self.allowed = frozenset({10})
        self.peer_ids = frozenset({20})

    def test_mentions_and_replies(self):
        self.assertTrue(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))
        self.message['text'] = 'normal message'
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))
        self.message['reply_to_message'] = {'from': {'id': 5}}
        self.assertTrue(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))

    def test_bot_must_be_allowlisted_and_explicit(self):
        self.message['from'] = self.peer
        self.message['text'] = 'Hi @MyBot'
        self.assertTrue(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, frozenset(), self.allowed, self.peer_ids))
        self.message['text'] = 'No mention'
        self.message['reply_to_message'] = {'from': {'id': 5}}
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))

    def test_anonymous_and_wrong_user_ignored(self):
        self.message['from'] = {'id': 30, 'is_bot': False}
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))
        self.message['from'] = self.human
        self.message['sender_chat'] = {'id': -123}
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))

    def test_bot_id_cannot_be_spoofed_by_username(self):
        self.message['from'] = {'id': 77, 'username': 'FriendBot', 'is_bot': True}
        self.assertFalse(is_addressed(self.message, 'MyBot', 5, self.peers, self.allowed, self.peer_ids))

    def test_bad_ids_rejected(self):
        for item in ('alice', '-1', '12,bad', '0'):
            with self.assertRaises(ValueError):
                numeric_ids(item, 'ALLOWED_USER_IDS')
        self.assertEqual(numeric_ids('10, 20', 'ALLOWED_USER_IDS'), frozenset({10,20}))

    def test_private_chat_ignored_even_for_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'))
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa, orchestrator=DirectTaskRunner(cwa))
                app.process_update({'update_id': 15, 'message': {**self.message, 'chat': {'id': 10, 'type': 'private'}}})
                app.process_update({'update_id': 16, 'message': {**self.message, 'chat': {'id': -123, 'type': 'private'}}})
            cwa.send.assert_not_called()

    def test_codex_config_needs_no_web_auth_or_conversation(self):
        values = {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '-123',
                  'ALLOWED_USER_IDS': '10', 'CODEX_WORKDIR': '.'}
        with patch.dict(os.environ, values, clear=True), patch('agent_relay.load_dotenv'):
            config = Config.from_env()
        self.assertEqual(config.codex_sandbox, 'workspace-write')
        self.assertEqual(config.model, '')
        self.assertEqual(config.codex_workdir, str(Path('.').resolve()))

    def test_network_access_config_is_explicit_and_validated(self):
        values = {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '-123',
                  'ALLOWED_USER_IDS': '10', 'CODEX_WORKDIR': '.', 'CODEX_NETWORK_ACCESS': 'true'}
        with patch.dict(os.environ, values, clear=True), patch('agent_relay.load_dotenv'):
            self.assertTrue(Config.from_env().codex_network_access)
        values['CODEX_NETWORK_ACCESS'] = 'tru'
        with patch.dict(os.environ, values, clear=True), patch('agent_relay.load_dotenv'):
            with self.assertRaisesRegex(ValueError, 'CODEX_NETWORK_ACCESS'):
                Config.from_env()

    def test_codex_workspace_is_required(self):
        values = {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '-123', 'ALLOWED_USER_IDS': '10'}
        with patch.dict(os.environ, values, clear=True), patch('agent_relay.load_dotenv'):
            with self.assertRaisesRegex(ValueError, 'CODEX_WORKDIR'):
                Config.from_env()

    def test_codex_config_rejects_unbounded_execution(self):
        values = {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '-123',
                  'ALLOWED_USER_IDS': '10', 'CODEX_WORKDIR': '.', 'CODEX_SANDBOX': 'danger-full-access'}
        with patch.dict(os.environ, values, clear=True), patch('agent_relay.load_dotenv'):
            with self.assertRaisesRegex(ValueError, 'CODEX_SANDBOX'):
                Config.from_env()

    def test_normal_human_and_peer_turns_never_receive_publication_capability(self):
        with tempfile.TemporaryDirectory() as folder:
            api, client = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            client.send.return_value.text = 'done'
            cfg = Config('secret', -123, '.', 'session.json', self.peers,
                         self.allowed, self.peer_ids, min_interval_seconds=0,
                         state_file=str(Path(folder) / 'cursor.json'), codex_allow_push=True)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, client, orchestrator=DirectTaskRunner(client))
                app.process_update({'update_id': 101, 'message': self.message})
                self.assertNotIn('allow_publish', client.send.call_args.kwargs)
                app.process_update({'update_id': 102, 'message': {**self.message, 'from': self.peer}})
                self.assertNotIn('allow_publish', client.send.call_args.kwargs)

    def test_publishing_config_requires_write_mode(self):
        values = {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '-123',
                  'ALLOWED_USER_IDS': '10', 'CODEX_WORKDIR': '.', 'CODEX_ALLOW_PUSH': 'true',
                  'CODEX_SANDBOX': 'read-only'}
        with patch.dict(os.environ, values, clear=True), patch('agent_relay.load_dotenv'):
            with self.assertRaisesRegex(ValueError, 'CODEX_ALLOW_PUSH'):
                Config.from_env()

    def test_utf16_split(self):
        text = '🧠' * 2400
        parts = split_for_telegram(text)
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(part.encode('utf-16-le')) // 2 <= 3900 for part in parts))

    def test_prompt_indicates_public_group(self):
        prompt = make_prompt(self.message, self.peers)
        self.assertIn('PUBLIC group', prompt)
        self.assertIn('Human', prompt)
        self.assertIn('@friendbot', prompt)

    def test_cursor_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'cursor.json')
            cursor = Cursor(path)
            cursor.advance(4)
            cursor.advance(3)
            self.assertEqual(Cursor(path).offset, 4)

    def test_one_codex_turn_no_replay_or_web_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'ok @FriendBot'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'), min_interval_seconds=0)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa, orchestrator=DirectTaskRunner(cwa))
                event = {'update_id': 11, 'message': self.message}
                app.process_update(event)
                app.process_update(event)
            cwa.send.assert_called_once()
            self.assertNotIn('conversation', cwa.send.call_args.kwargs)
            send_calls = [c for c in api.call.call_args_list if c.args[0] == 'sendMessage']
            self.assertEqual(len(send_calls), 1)
            self.assertEqual(Cursor(cfg.state_file).offset, 12)

    def test_no_retry_after_codex_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.side_effect = TimeoutError('potentially submitted')
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'), min_interval_seconds=0)
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa, orchestrator=DirectTaskRunner(cwa))
                event = {'update_id': 11, 'message': self.message}
                app.process_update(event)
                app.process_update(event)
            cwa.send.assert_called_once()
            self.assertEqual(Cursor(cfg.state_file).offset, 12)

    def test_other_group_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers, self.allowed, self.peer_ids,
                         state_file=str(Path(folder) / 'cursor.json'))
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa, orchestrator=DirectTaskRunner(cwa))
                app.process_update({'update_id': 1, 'message': {**self.message, 'chat': {'id': -999}}})
            cwa.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
