"""Acceptance across real interpreter restarts and .env changes, without services."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


FIRST = '11111111-1111-4111-8111-111111111111'
SECOND = '22222222-2222-4222-8222-222222222222'
INTRO = '[AgentRelay / server startup context]'
HANDOVER = '[AgentRelay / selected ChatGPT conversation changed]'
AUTH = 'fixture-access-token-never-forward-17'


class ContextAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'project').mkdir()
        (self.root / 'auth_data.json').write_text(json.dumps({
            'accessToken': AUTH, 'sessionToken': 'fixture-session-token-never-forward-18'}), encoding='utf-8')

    def event(self, number, text, *, topic=7, sender=10, chat=-123, kind='supergroup'):
        return {'update_id': number, 'message': {'message_id': number, 'text': text,
            'chat': {'id': chat, 'type': kind}, 'message_thread_id': topic,
            'from': {'id': sender, 'first_name': 'Acceptance User'}}}

    def phase(self, *, target=FIRST, operations=(), catchup=(), fail=False):
        (self.root / '.env').write_text('\n'.join((
            'TELEGRAM_BOT_TOKEN=fixture-telegram-token', 'TELEGRAM_CHAT_ID=-123',
            'ALLOWED_USER_IDS=10,11', 'CODEX_WORKDIR=project', 'CODEX_MODEL=gpt-6-sol',
            'CODEX_REASONING_EFFORT=medium', 'CODEX_ALLOW_PUSH=true',
            'CODEX_ALIASES=Сударь', 'CHAT_ALIASES=юки', 'DEFAULT_BACKEND=chat',
            'CWA_CONVERSATION_ID=https://chatgpt.com/c/' + target,
            'CWA_AUTH_FILE=auth_data.json', 'MIN_INTERVAL_SECONDS=0',
            'GROUP_CONTEXT_LIMIT=30', 'STATE_FILE=state.json',
        )), encoding='utf-8')
        # No inherited API keys, real bot settings, user PYTHONPATH or Git overrides.
        env = {key: value for key, value in os.environ.items() if key.upper() in {
            'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATH', 'PATHEXT', 'COMSPEC'}}
        env['PYTHONIOENCODING'] = 'utf-8'
        worker = Path(__file__).with_name('context_acceptance_worker.py')
        result = subprocess.run([sys.executable, str(worker)], cwd=self.root, env=env,
            input=json.dumps({'operations': operations, 'catchup': catchup, 'fail_chat': fail}),
            capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['startup_calls'], 0)
        self.assertEqual(data['startup_posts'], 0)
        self.assertEqual(data['counters'], {'codex': 0, 'publication': 0})
        self.assertEqual(data['tasks'], 0)
        return data

    def test_context_survives_process_restarts_and_chat_replacement_with_one_combined_intro(self):
        first = self.phase(catchup=[self.event(1, 'Pending fact 8426'),
            self.event(2, '/task DO-NOT-REPLAY'), self.event(3, '/publish 1 main'),
            self.event(4, 'юки, OLD-MENTION-NOT-AN-INVOCATION')], operations=[
            self.event(5, '/discuss FIRST-PUBLIC-QUESTION'),
            self.event(6, 'Unaddressed fact after answer 9531'),
            self.event(7, 'PRIVATE-DM-CANARY', kind='private'),
            self.event(8, 'OTHER-GROUP-CANARY', chat=-999),
            self.event(9, 'Public credential quote ' + AUTH),
            self.event(10, '/discuss OTHER-TOPIC-NOTE-CANARY', topic=8)])
        self.assertEqual(len(first['calls']), 2)
        self.assertIn('8426', first['calls'][0]['prompt'])
        second = self.phase(operations=[self.event(11, 'юки'),
                                        self.event(12, '/discuss NEXT-CURRENT-QUESTION')])
        self.assertIn('9531', second['restored'])
        self.assertEqual(len(second['calls']), 2)
        self.assertIn(INTRO, second['calls'][0]['prompt'])
        self.assertNotIn(INTRO, second['calls'][1]['prompt'])
        self.assertNotIn(HANDOVER, second['calls'][0]['prompt'])
        third = self.phase(target=SECOND, operations=[self.event(13, '/discuss AFTER-CHAT-REPLACEMENT'),
                                                      self.event(14, '/discuss FOLLOW-UP')])
        self.assertEqual(len(third['calls']), 2)
        combined = third['calls'][0]['prompt']
        for marker in (INTRO, HANDOVER, '8426', '9531', 'FIRST-PUBLIC-QUESTION', 'AFTER-CHAT-REPLACEMENT'):
            self.assertIn(marker, combined)
        self.assertNotIn(INTRO, third['calls'][1]['prompt'])
        self.assertNotIn(HANDOVER, third['calls'][1]['prompt'])
        for phase, target in ((second, FIRST), (third, SECOND)):
            for call in phase['calls']:
                self.assertEqual(call['params']['conversation'], target)
                self.assertIs(call['params']['temporary'], False)
                for canary in ('PRIVATE-DM-CANARY', 'OTHER-GROUP-CANARY', 'OTHER-TOPIC-NOTE-CANARY', AUTH):
                    self.assertNotIn(canary, call['prompt'])
            self.assertTrue(all(post['text'].startswith('ChatGPT\n\n') for post in phase['posts']))

    def test_failed_new_chat_never_uses_anonymous_or_codex_fallback_and_waits_for_new_request(self):
        self.phase(operations=[self.event(1, '/discuss OLD-PUBLIC-GOAL')])
        failed = self.phase(target=SECOND, fail=True, operations=[self.event(2, 'юки, new request')])
        self.assertEqual(len(failed['calls']), 1)
        self.assertEqual(failed['posts'], [])
        self.assertEqual(failed['calls'][0]['params'], {'conversation': SECOND, 'temporary': False})
        self.assertTrue(failed['intro_pending'])
        recovered = self.phase(target=SECOND, operations=[self.event(3, 'юки, NEW-EXPLICIT-REQUEST')])
        self.assertEqual(len(recovered['calls']), 1)
        for marker in (INTRO, HANDOVER, 'OLD-PUBLIC-GOAL', 'NEW-EXPLICIT-REQUEST'):
            self.assertIn(marker, recovered['calls'][0]['prompt'])
        self.assertFalse(recovered['intro_pending'])

    def test_controls_and_isolated_planning_do_not_consume_intro_or_replay_historical_work(self):
        first = self.phase(operations=[self.event(1, '/task'),
            self.event(2, '/mode', sender=11), self.event(3, '/mode', topic=8),
            {'planner': True}, self.event(4, '/discuss'), self.event(5, 'юки, FIRST-ACTUAL-QUESTION'),
            self.event(6, '/stop'), self.event(7, 'юки, PAUSED-REQUEST'),
            self.event(8, '/resume'), self.event(9, 'юки, SECOND-ACTUAL-QUESTION')])
        self.assertEqual(len(first['calls']), 3)
        self.assertEqual(first['calls'][0]['prompt'], 'EXPLICIT_TASK_ONLY')
        self.assertEqual(first['calls'][0]['params'], {'conversation': None, 'temporary': True})
        self.assertIn(INTRO, first['calls'][1]['prompt'])
        self.assertNotIn(INTRO, first['calls'][2]['prompt'])
        self.assertNotIn('PAUSED-REQUEST', first['calls'][2]['prompt'])
        self.assertEqual(first['other_person_mode7'], 'chat')
        self.assertEqual(first['mode8'], 'chat')
        restarted = self.phase(operations=[self.event(10, '/status'),
                                          self.event(11, 'юки, AFTER-RESTART')])
        self.assertEqual(len(restarted['calls']), 1)
        self.assertIn(INTRO, restarted['calls'][0]['prompt'])
        self.assertNotIn(HANDOVER, restarted['calls'][0]['prompt'])


if __name__ == '__main__':
    unittest.main()
