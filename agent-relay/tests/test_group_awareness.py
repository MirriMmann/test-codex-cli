import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config, is_addressed, usernames
from group_awareness import GroupAwareness, addressed_by_alias, explicit_handoff, trusted_sender
from relay_fakes import DirectTaskRunner


class GroupAwarenessTests(unittest.TestCase):
    def setUp(self):
        self.human = {'id': 10, 'first_name': 'Alice', 'is_bot': False}
        self.peer = {'id': 20, 'username': 'FriendBot', 'is_bot': True}
        self.peers = usernames('FriendBot')
        self.allowed = frozenset({10})
        self.peer_ids = frozenset({20})

    def msg(self, sender, text, mid=1, group=-123):
        return {'chat': {'id': group, 'type': 'supergroup'},
                'message_id': mid, 'from': sender, 'text': text}

    def test_allowlist_and_sender_chat(self):
        self.assertTrue(trusted_sender(self.msg(self.human, 'hello'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertTrue(trusted_sender(self.msg(self.peer, 'hello'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender(self.msg({**self.peer, 'id': 99}, 'hi'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender(self.msg({**self.peer, 'username': 'fakebot'}, 'hi'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender(self.msg({**self.peer, 'id': 5}, 'hi'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender(self.msg({'id': 11}, 'hi'), self.allowed, self.peers, self.peer_ids, 5))
        self.assertFalse(trusted_sender({**self.msg(self.human, 'hi'), 'sender_chat': {'id': -123}}, self.allowed, self.peers, self.peer_ids, 5))

    def test_bounded_context_and_exclusion(self):
        group = GroupAwareness(2)
        group.remember(self.msg(self.human, 'first', 1))
        group.remember(self.msg(self.human, 'second', 2))
        group.remember(self.msg(self.human, 'third', 3))
        self.assertNotIn('first', group.recent_text())
        self.assertIn('second', group.recent_text())
        self.assertNotIn('third', group.recent_text(exclude_message_id=3))
        group.remember_own_reply('MyBot', 'reply')
        self.assertIn({'message_id': None, 'sender_kind': 'bot', 'author': 'MyBot', 'text': 'reply'},
                      [json.loads(line) for line in group.recent_text().splitlines()])
        group.remember(self.msg(self.human, 'x' * 800, 4))
        self.assertNotIn('x' * 800, group.recent_text())
        self.assertEqual(GroupAwareness(0).recent_text(), '')

    def test_alias_exact_words_only(self):
        self.assertTrue(addressed_by_alias('Рагна, что думаешь?', ('Рагна',)))
        self.assertTrue(addressed_by_alias('Hi Ragna!', ('Ragna',)))
        self.assertFalse(addressed_by_alias('NotRagna or @Ragna', ('Ragna',)))
        self.assertFalse(addressed_by_alias('он про рог', ('Рагна',)))
        self.assertFalse(addressed_by_alias('Hi Al', ('Al',)))
        msg = self.msg(self.human, 'Рагна, помоги')
        self.assertTrue(is_addressed(msg, 'MyBot', 5, self.peers, self.allowed, self.peer_ids, ('Рагна',)))
        self.assertTrue(is_addressed(self.msg(self.peer, 'Рагна, помоги'), 'MyBot', 5,
                                       self.peers, self.allowed, self.peer_ids, ('Рагна',)))

    def test_peer_alias_requires_trusted_id_and_username(self):
        for sender in ({**self.peer, 'id': 99}, {**self.peer, 'username': 'OtherBot'},
                       {**self.peer, 'username': None}, {'id': 99, 'is_bot': False}):
            with self.subTest(sender=sender):
                self.assertFalse(is_addressed(self.msg(sender, 'Mir, continue'), 'MyBot', 5,
                                              self.peers, self.allowed, self.peer_ids, ('Mir',)))

    def test_explicit_handoff(self):
        out = explicit_handoff('Привет! [[handoff:FriendBot]]', self.peers)
        self.assertEqual(out, '/relay@FriendBot Привет!')
        self.assertEqual(explicit_handoff('@FriendBot hi [[handoff:friendbot]]', self.peers), '/relay@friendbot hi')
        self.assertEqual(explicit_handoff('@FriendBot hi', self.peers), '/relay@FriendBot hi')
        self.assertEqual(explicit_handoff('Quoted @FriendBot hi', self.peers), 'Quoted @FriendBot hi')
        self.assertEqual(explicit_handoff('Hi [[handoff:BadBot]]', self.peers), 'Hi')
        self.assertEqual(explicit_handoff('Hi [[handoff:FriendBot]]', self.peers), '/relay@FriendBot Hi')
        self.assertEqual(explicit_handoff('Hello', self.peers), 'Hello')

    def test_telegram_command_mention_from_peer(self):
        self.assertTrue(is_addressed(self.msg(self.peer, '/relay@MyBot Привет'), 'MyBot', 5,
                                     self.peers, self.allowed, self.peer_ids))
        self.assertTrue(is_addressed(self.msg(self.peer, '/relay@mybot Привет'), 'MyBot', 5,
                                     self.peers, self.allowed, self.peer_ids))
        self.assertFalse(is_addressed(self.msg({**self.peer, 'id': 99}, '/relay@MyBot Привет'), 'MyBot', 5,
                                      self.peers, self.allowed, self.peer_ids))
        self.assertFalse(is_addressed(self.msg(self.peer, '/relay@OtherBot Привет'), 'MyBot', 5,
                                      self.peers, self.allowed, self.peer_ids))

    def test_group_context_does_not_trigger_ai_and_handoff_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = 'Передаю слово. [[handoff:FriendBot]]'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, bot_aliases=('Рагна',))
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa, orchestrator=DirectTaskRunner(cwa))
                app.process_update({'update_id': 11, 'message': self.msg(self.human, 'Обсудим планы', 1)})
                cwa.send.assert_not_called()
                app.process_update({'update_id': 12, 'message': self.msg(self.peer, 'Мнение второго бота', 2)})
                cwa.send.assert_not_called()
                app.process_update({'update_id': 13, 'message': self.msg({'id': 99}, 'чужая реплика', 3)})
                app.process_update({'update_id': 14, 'message': self.msg(self.human, 'Рагна, продолжай', 4)})
            cwa.send.assert_called_once()
            prompt = cwa.send.call_args.args[0]
            self.assertIn('Обсудим планы', prompt)
            self.assertIn('Мнение второго бота', prompt)
            self.assertNotIn('чужая реплика', prompt)
            self.assertEqual(prompt.count('Рагна, продолжай'), 1)
            posted = [x.args[1]['text'] for x in api.call.call_args_list if x.args[0] == 'sendMessage']
            self.assertEqual(len(posted), 1)
            self.assertTrue(posted[0].startswith('codex\n\nЗадача #'))
            self.assertTrue(posted[0].endswith('\n\n/relay@FriendBot Передаю слово.'))

    def test_bot_reply_can_handoff_back_to_peer(self):
        with tempfile.TemporaryDirectory() as folder:
            api, cwa = Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            cwa.send.return_value.text = '@FriendBot продолжим [[handoff:FriendBot]]'
            cfg = Config('secret', -123, 'existing-conversation', 'auth.json', self.peers,
                         self.allowed, self.peer_ids, state_file=str(Path(folder) / 'cursor.json'),
                         min_interval_seconds=0, bot_aliases=('Mir',))
            with patch('builtins.print'):
                app = AgentRelay(cfg, api, cwa, orchestrator=DirectTaskRunner(cwa))
                app.process_update({'update_id': 11, 'message': self.msg(self.peer, 'mir, continue', 1)})
            cwa.send.assert_called_once()
            posted = [x.args[1]['text'] for x in api.call.call_args_list if x.args[0] == 'sendMessage']
            self.assertEqual(len(posted), 1)
            self.assertTrue(posted[0].endswith('\n\n/relay@FriendBot продолжим'))
            prompt = cwa.send.call_args.args[0]
            self.assertIn('You may also hand off when replying to another bot', prompt)
            self.assertNotIn('Do not hand off when replying to another bot', prompt)
            self.assertNotIn('reply_parameters', [x.args[1] for x in api.call.call_args_list if x.args[0] == 'sendMessage'][0])

    def test_handoff_continues_across_multiple_bot_turns(self):
        with tempfile.TemporaryDirectory() as folder:
            apps = []
            clients = []
            for own_id, own_name, peer_id, peer_name in (
                (5, 'MyBot', 20, 'FriendBot'), (20, 'FriendBot', 5, 'MyBot'),
            ):
                api, client = Mock(), Mock()
                api.call.return_value = {'id': own_id, 'username': own_name}
                client.send.return_value.text = f'Continue [[handoff:{peer_name}]]'
                config = Config('secret', -123, 'conversation', 'auth.json', usernames(peer_name),
                                self.allowed, frozenset({peer_id}), min_interval_seconds=0,
                                state_file=str(Path(folder) / f'{own_id}.json'))
                with patch('builtins.print'):
                    apps.append(AgentRelay(config, api, client, orchestrator=DirectTaskRunner(client)))
                clients.append(client)
            incoming = self.msg(self.human, '@MyBot discuss', 1)
            for turn, app in enumerate((apps[0], apps[1], apps[0], apps[1])):
                with patch('builtins.print'):
                    app.process_update({'update_id': 100 + turn, 'message': incoming})
                posted = [call.args[1]['text'] for call in app.api.call.call_args_list
                          if call.args[0] == 'sendMessage'][-1]
                expected_peer = 'FriendBot' if app.bot_id == 5 else 'MyBot'
                self.assertTrue(posted.startswith('codex\n\nЗадача #'))
                self.assertTrue(posted.endswith(f'\n\n/relay@{expected_peer} Continue'))
                incoming = self.msg({'id': app.bot_id, 'username': app.bot_username, 'is_bot': True},
                                    posted, turn + 2)
            self.assertEqual([client.send.call_count for client in clients], [2, 2])


if __name__ == '__main__':
    unittest.main()
