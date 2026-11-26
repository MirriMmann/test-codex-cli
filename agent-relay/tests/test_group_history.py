"""Real SQLite persistence, restart boundaries, access changes and safe failures."""
import json
import os
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_chat_sessions as sessions
import test_context_restore as context
from agent_relay import AgentRelay, Config
from group_awareness import GroupAwareness
from group_history import GroupHistory, GroupHistoryError
from privacy import PrivacyGuard
from task_protocol import relative_path


class GroupHistoryTests(unittest.TestCase):
    setUp = sessions.ChatSessionTests.setUp
    restart = sessions.ChatSessionTests.restart
    event = context.ContextRestoreTests.event

    def test_restart_restores_unaddressed_message_without_starting_generation(self):
        self.config = replace(self.config, default_backend='chat', chat_aliases=('юки',))
        self.restart(sessions.FIRST)
        self.app.process_update(self.event(1, 'До обращения: код 7319'))
        self.chat.send.assert_not_called()
        self.restart(sessions.FIRST)
        self.chat.send.assert_not_called()
        self.app.process_update(self.event(2, 'юки, какой код был до перезапуска?'))
        self.chat.send.assert_called_once()
        self.assertIn('До обращения: код 7319', self.chat.send.call_args.args[0])
        self.assertEqual(self.app.cursor.offset, 3)

    def test_own_reply_survives_restart_without_service_intro_or_actor_ids_in_prompt(self):
        self.app.process_update(self.event(1, '/discuss Initial question'))
        self.restart(sessions.FIRST)
        self.assertEqual(self.chat.send.call_count, 1)
        transcript = self.app.group_context.recent_text(topic_id=7)
        rows = [json.loads(line) for line in transcript.splitlines()]
        self.assertEqual([row['text'] for row in rows], ['Initial question', 'Public answer'])
        self.assertEqual(rows[-1]['sender_kind'], 'bot')
        self.assertTrue(all(set(row) == {'message_id', 'sender_kind', 'author', 'text'} for row in rows))
        saved = self.app.group_context.store.load()
        self.assertNotIn('[AgentRelay / server startup context]', repr(saved))
        self.app.process_update(self.event(2, '/discuss Continue'))
        self.assertIn('Public answer', self.chat.send.call_args.args[0])
        self.assertNotIn('"sender":', self.chat.send.call_args.args[0])

    def test_same_file_never_mixes_groups_or_bot_accounts(self):
        self.app.process_update(self.event(1, 'GROUP-123-BOT-5'))
        self.config = replace(self.config, chat_id=-456)
        self.restart(sessions.FIRST)
        self.assertEqual(self.app.group_context.recent_text(topic_id=7), '')
        self.app.process_update(self.event(2, 'GROUP-456-BOT-5', chat=-456))
        self.config = replace(self.config, chat_id=-123)
        self.api.call.return_value = {'id': 6, 'username': 'OtherBot'}
        self.restart(sessions.FIRST)
        self.assertEqual(self.app.group_context.recent_text(topic_id=7), '')
        self.app.process_update(self.event(3, 'GROUP-123-BOT-6'))
        self.api.call.return_value = {'id': 5, 'username': 'MyBot'}
        self.restart(sessions.FIRST)
        transcript = self.app.group_context.recent_text(topic_id=7)
        self.assertIn('GROUP-123-BOT-5', transcript)
        self.assertNotIn('GROUP-456-BOT-5', transcript)
        self.assertNotIn('GROUP-123-BOT-6', transcript)

    def test_restored_history_keeps_topic_boundaries(self):
        self.app.process_update(self.event(1, 'TOPIC-SEVEN-7319'))
        self.app.process_update(self.event(2, 'TOPIC-EIGHT-PRIVATE-CONTEXT', topic=8))
        self.restart(sessions.FIRST)
        self.app.process_update(self.event(3, '/discuss What was the code?'))
        self.assertIn('TOPIC-SEVEN-7319', self.chat.send.call_args.args[0])
        self.assertNotIn('TOPIC-EIGHT-PRIVATE-CONTEXT', self.chat.send.call_args.args[0])

    def test_removed_human_and_renamed_peer_are_pruned_on_load(self):
        self.config = replace(self.config, allowed_user_ids=frozenset({10, 11}),
                              peers=frozenset({'friendbot'}), peer_bot_ids=frozenset({20}))
        self.restart(sessions.FIRST)
        self.app.process_update(self.event(1, 'KEEP-CONTEXT'))
        self.app.process_update(self.event(2, 'REVOKED-HUMAN', sender=11))
        peer = self.event(3, 'REVOKED-PEER', sender=20)
        peer['message']['from'].update(is_bot=True, username='FriendBot')
        self.app.process_update(peer)
        self.config = replace(self.config, allowed_user_ids=frozenset({10}), peers=frozenset({'renamedbot'}))
        self.restart(sessions.FIRST)
        saved = repr(self.app.group_context.store.load())
        self.assertIn('KEEP-CONTEXT', saved)
        self.assertNotIn('REVOKED-HUMAN', saved)
        self.assertNotIn('REVOKED-PEER', saved)

    def test_private_untrusted_and_credential_content_never_enters_store(self):
        token = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
        self.app.process_update(self.event(1, 'PRIVATE-CANARY', kind='private'))
        self.app.process_update(self.event(2, 'OTHER-GROUP-CANARY', chat=-999))
        self.app.process_update(self.event(3, 'UNTRUSTED-CANARY', sender=99))
        self.app.process_update(self.event(4, 'Public token ' + token))
        saved = repr(self.app.group_context.store.load())
        for value in ('PRIVATE-CANARY', 'OTHER-GROUP-CANARY', 'UNTRUSTED-CANARY', token):
            self.assertNotIn(value, saved)
        self.assertIn('[REDACTED]', saved)

    def test_newly_known_credential_is_redacted_again_on_restart(self):
        credential = 'session-canary-newly-protected-7e9d'
        self.app.process_update(self.event(1, 'Old public quote ' + credential))
        self.app = AgentRelay(self.config, self.api, self.codex, self.chat,
                              privacy=PrivacyGuard((credential,)))
        self.assertNotIn(credential, self.app.group_context.recent_text(topic_id=7))
        self.assertNotIn(credential, repr(self.app.group_context.store.load()))
        self.assertNotIn(credential.encode(), self.app.group_context.store.path.read_bytes())

    def test_reduced_limit_prunes_saved_entries_and_old_topics_stay_bounded(self):
        memory = GroupAwareness(2)
        for topic in range(1, 101):
            for number in range(1, 5):
                memory.remember(self.event(number, f'fact-{topic}-{number}', topic=topic)['message'])
        store = self.app.group_context.store
        store.save(memory._snapshot(memory.entries))
        self.config = replace(self.config, group_context_limit=1)
        self.restart(sessions.FIRST)
        saved = self.app.group_context.store.load()
        self.assertEqual(len(saved), 32)
        self.assertTrue(all(len(topic['entries']) == 2 for topic in saved))
        self.assertEqual(self.app.group_context.recent_text(topic_id=1), '')
        self.assertIn('fact-100-4', self.app.group_context.recent_text(topic_id=100))

    def test_zero_limit_stops_reading_and_writing_but_keeps_existing_history(self):
        self.app.process_update(self.event(1, 'SAVED-BEFORE-DISABLING'))
        path = self.app.group_context.store.path
        before = path.read_bytes()
        self.config = replace(self.config, group_context_limit=0)
        self.restart(sessions.FIRST)
        self.assertIsNone(self.app.group_context.store)
        self.app.process_update(self.event(2, 'NOT-OBSERVED-WHILE-DISABLED'))
        self.assertEqual(path.read_bytes(), before)
        self.config = replace(self.config, group_context_limit=30)
        self.restart(sessions.FIRST)
        transcript = self.app.group_context.recent_text(topic_id=7)
        self.assertIn('SAVED-BEFORE-DISABLING', transcript)
        self.assertNotIn('NOT-OBSERVED-WHILE-DISABLED', transcript)
        disabled = self.root / 'disabled.sqlite3'
        self.config = replace(self.config, group_context_limit=0, group_context_file=str(disabled))
        self.restart(sessions.FIRST)
        self.assertFalse(disabled.exists())

    def test_history_failure_prevents_cursor_advance_and_generation(self):
        self.config = replace(self.config, default_backend='chat')
        self.restart(sessions.FIRST)
        update = self.event(1, '@MyBot new request')
        with patch.object(self.app.group_context.store, 'save', side_effect=GroupHistoryError('Cannot save history')):
            with self.assertRaises(GroupHistoryError):
                self.app.process_update(update)
        self.assertEqual(self.app.cursor.offset, 0)
        self.assertEqual(self.app.group_context.entries, {})
        self.chat.send.assert_not_called()
        self.codex.send.assert_not_called()
        self.app.process_update(update)
        self.chat.send.assert_called_once()

    def test_cursor_failure_after_commit_redelivers_without_duplicate_context(self):
        self.config = replace(self.config, default_backend='chat')
        self.restart(sessions.FIRST)
        update = self.event(1, '@MyBot request after crash')
        with patch.object(self.app.cursor, '_persist', side_effect=OSError('cursor write failed')):
            with self.assertRaises(OSError):
                self.app.process_update(update)
        self.chat.send.assert_not_called()
        self.restart(sessions.FIRST)
        self.assertEqual(self.app.cursor.offset, 0)
        self.app.process_update(update)
        self.app.process_update(update)
        self.chat.send.assert_called_once()
        rows = self.app.group_context.store.load()[0]['entries']
        self.assertEqual(sum(row['message_id'] == 1 for row in rows), 1)

    def test_reply_history_failure_does_not_resend_a_delivered_answer(self):
        store = self.app.group_context.store
        save = store.save
        def fail_on_answer(snapshot):
            if any(entry['own'] for topic in snapshot for entry in topic['entries']):
                raise GroupHistoryError('Cannot save reply context')
            save(snapshot)
        with patch.object(store, 'save', side_effect=fail_on_answer), patch('sys.stderr'):
            update = self.event(1, '/discuss Answer once')
            self.app.process_update(update)
            self.app.process_update(update)
        sends = [call for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']
        self.assertEqual(len(sends), 1)
        self.assertIn('Public answer', sends[0].args[1]['text'])
        self.chat.send.assert_called_once()

    def test_corrupt_snapshot_stops_startup_without_disclosing_or_resetting_it(self):
        self.app.process_update(self.event(1, 'Initial fact'))
        path = self.app.group_context.store.path
        for malformed in ('PRIVATE-DATABASE-CANARY', json.dumps([{'topic_id': 7, 'entries': 'invalid'}])):
            with self.subTest(value=malformed), closing(sqlite3.connect(path)) as db:
                db.execute('UPDATE telegram_context SET snapshot=?', (malformed,))
                db.commit()
                with self.assertRaises(GroupHistoryError) as caught:
                    self.restart(sessions.FIRST)
                self.assertNotIn(malformed, str(caught.exception))
                self.assertIn('GROUP_CONTEXT_FILE', str(caught.exception))
                self.assertEqual(db.execute('SELECT snapshot FROM telegram_context').fetchone()[0], malformed)
        self.chat.send.assert_not_called()

    def test_custom_path_and_publication_exclusion(self):
        path = self.root / 'private' / 'context.sqlite3'
        self.config = replace(self.config, group_context_file=str(path))
        self.restart(sessions.FIRST)
        self.app.process_update(self.event(1, 'Custom path fact'))
        self.assertTrue(path.exists())
        self.restart(sessions.FIRST)
        self.assertIn('Custom path fact', self.app.group_context.recent_text(topic_id=7))
        with self.assertRaises(ValueError):
            relative_path('private/context.sqlite3')
        with self.assertRaises(GroupHistoryError):
            GroupHistory(str(self.root / 'public.json'), bot_id=5, chat_id=-123)

    def test_context_file_environment_validation_is_safe(self):
        values = {'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': '-123',
                  'CODEX_WORKDIR': str(self.root), 'ALLOWED_USER_IDS': '10'}
        with patch('agent_relay.load_dotenv'), patch.dict(os.environ, values, clear=True):
            self.assertEqual(Config.from_env().group_context_file, '')
            for value in ('private.sqlite3', 'C:\\local\\context.sqlite3'):
                os.environ['GROUP_CONTEXT_FILE'] = value
                self.assertEqual(Config.from_env().group_context_file, value)
            for value in ('PRIVATE-CANARY.json', 'PRIVATE-CANARY.txt'):
                os.environ['GROUP_CONTEXT_FILE'] = value
                with self.assertRaises(ValueError) as caught:
                    Config.from_env()
                self.assertNotIn('PRIVATE-CANARY', str(caught.exception))

    def test_storage_failure_stops_polling_before_acknowledgment_or_generation(self):
        self.api.call.side_effect = lambda method, *args, **kwargs: {} if method == 'getWebhookInfo' else [
            self.event(1, '@MyBot do not start until saved')]
        # Existing-cursor polling path; initial catch-up has its own tests.
        self.app.cursor.advance(1)
        with patch.object(self.app.group_context.store, 'save', side_effect=GroupHistoryError('Storage unavailable')):
            with self.assertRaises(GroupHistoryError):
                self.app.serve()
        self.assertEqual(self.app.cursor.offset, 1)
        self.assertIsNone(self.app._executor)
        self.chat.send.assert_not_called()
        self.codex.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
