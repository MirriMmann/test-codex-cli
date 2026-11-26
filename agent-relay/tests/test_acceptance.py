"""Acceptance boundaries: synthetic canaries, real Git/checks, no external services."""
import itertools
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from generation import GenerationFailed
from git_publish_mcp import GitPublisher
from privacy import GuardedClient, PrivacyGuard
from verification import TaskVerifier


TOKEN = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
PRIVATE = 'PRIVATE-DIRECT-MESSAGE-CANARY-83c1'
AUTH = 'local-session-canary-never-disclose-29'
PLAN = {'goal': 'Fix addition', 'constraints': [], 'acceptance_criteria': ['2 + 3 returns 5']}


def response(value, **kwargs):
    return SimpleNamespace(text=json.dumps(value), **kwargs)


class PrivacyAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        for target in ('builtins.print', 'agent_relay.record_event'):
            mocked = patch(target)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.api, self.executor, self.chat = Mock(), Mock(), Mock()
        self.api.call.return_value = {'id': 5, 'username': 'MyBot'}
        self.chat.send.return_value = SimpleNamespace(text='public answer')
        self.uid = itertools.count(1)
        self.guard = PrivacyGuard((TOKEN, AUTH))
        self.config = Config(TOKEN, -123, str(self.root), 'unused.json', frozenset(),
                             frozenset({10}), state_file=str(self.root / 'state.json'),
                             min_interval_seconds=0, chat_conversation_id='personal-history-must-not-be-used')
        self.app = AgentRelay(self.config, self.api, self.executor, self.chat, privacy=self.guard)

    def event(self, text, *, topic=7, private=False):
        number = next(self.uid)
        return {'update_id': number, 'message': {'message_id': number, 'text': text,
                'message_thread_id': topic, 'chat': {'id': -123, 'type': 'private' if private else 'supergroup'},
                'from': {'id': 10, 'is_bot': False}}}

    def send(self, text, **kwargs):
        self.app.process_update(self.event(text, **kwargs))

    def outgoing(self):
        return '\n'.join(call.args[1]['text'] for call in self.api.call.call_args_list
                         if call.args[0] == 'sendMessage')

    def test_private_updates_and_other_topics_do_not_reach_discussion_or_planning(self):
        self.send(PRIVATE, private=True)
        self.send('OTHER-TOPIC-CANARY', topic=8)
        self.send('PUBLIC-TOPIC-CONTEXT')
        self.send('/discuss explain addition')
        prompt = self.chat.send.call_args.args[0]
        self.assertIn('PUBLIC-TOPIC-CONTEXT', prompt)
        self.assertNotIn(PRIVATE, prompt)
        self.assertNotIn('OTHER-TOPIC-CANARY', prompt)
        self.chat.send.return_value = response(PLAN)
        self.executor.send.return_value = SimpleNamespace(text='malformed report')
        self.chat.send.reset_mock()
        self.send('/task fix addition')
        self.chat.send.assert_not_called()  # direct route has no web planning turn
        executor = self.executor.send.call_args.args[0]
        for prompt in (executor,):
            for canary in (PRIVATE, 'OTHER-TOPIC-CANARY', 'PUBLIC-TOPIC-CONTEXT'):
                self.assertNotIn(canary, prompt)
        self.assertNotIn(PRIVATE, self.outgoing())

    def test_credential_in_input_and_public_reply_is_scrubbed_before_storage_or_dispatch(self):
        event = self.event('/task fix addition ' + TOKEN)
        event['message']['reply_to_message'] = {'text': 'use ' + AUTH}
        self.chat.send.return_value = response(PLAN)
        self.executor.send.return_value = SimpleNamespace(text='malformed report')
        self.app.process_update(event)
        self.assertIn(TOKEN, event['message']['text'])  # caller input was not mutated
        for value in (repr(self.chat.send.call_args_list), repr(self.executor.send.call_args_list),
                      json.dumps(self.app.tasks.get(1)), self.outgoing()):
            self.assertNotIn(TOKEN, value)
            self.assertNotIn(AUTH, value)

    def test_valid_json_planner_with_credential_is_blocked_before_codex_and_persistence(self):
        self.chat.send.return_value = response({**PLAN, 'constraints': ['deploy with ' + AUTH]})
        self.send('/task')
        self.send('@MyBot fix addition')  # legacy Codex mode still exercises the isolated planner
        self.executor.send.assert_not_called()
        task = self.app.tasks.get(1)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['attempts'], 0)
        self.assertEqual(task['plan_json'], '')
        self.assertNotIn(AUTH, self.outgoing())

    def test_chat_credential_response_is_never_sent_or_added_to_group_context(self):
        self.chat.send.return_value = SimpleNamespace(text='Here is ' + AUTH)
        self.send('/discuss answer')
        self.assertNotIn(AUTH, self.outgoing())
        self.assertNotIn(AUTH, self.app.group_context.recent_text(topic_id=7))

    def test_executor_credential_is_not_persisted_or_forwarded_to_verifier(self):
        self.chat.send.return_value = response(PLAN)
        self.executor.send.return_value = response({'status': 'completed', 'summary': AUTH,
                                                   'changes': [], 'checks': [], 'remaining': []})
        self.send('/task fix addition')
        self.executor.send.assert_called_once()
        task = self.app.tasks.get(1)
        self.assertEqual(task['report_json'], '')
        self.assertEqual(task['status'], 'failed')
        self.assertNotIn(AUTH, self.outgoing())

    def test_old_saved_report_is_scrubbed_at_telegram_boundary(self):
        identifier = self.app.tasks.create(update_id=50, chat_id=-123, topic_id=7, owner_id=10,
                    owner_is_bot=False, message_id=50, repository=str(self.root), goal='old task')
        self.app.tasks.save_final_report(identifier, 'old answer ' + AUTH)
        self.send('/report ' + str(identifier))
        self.assertIn('[REDACTED]', self.outgoing())
        self.assertNotIn(AUTH, self.outgoing())

    def test_auth_registry_and_encoded_json_and_command_evidence_are_checked(self):
        auth = self.root / 'fake-auth.json'
        auth.write_text(json.dumps({'accessToken': AUTH, 'cookies': {'session': TOKEN},
                                    'displayName': 'Public Name'}), encoding='utf-8')
        guard = PrivacyGuard.configured(TOKEN, str(auth))
        self.assertEqual(guard.redact('Public Name'), 'Public Name')
        self.assertNotIn(AUTH, guard.redact(AUTH))
        unicode_secret = 'секретная-строка-длиннее-12'
        with self.assertRaises(GenerationFailed):
            PrivacyGuard((unicode_secret,)).require_clean(json.dumps({'goal': unicode_secret}))
        client = Mock()
        client.send.return_value = SimpleNamespace(text='clean', commands=[{'command': 'echo ' + AUTH}])
        with self.assertRaises(GenerationFailed):
            GuardedClient(client, guard).send('check')

    def test_recognized_secret_formats_are_blocked_without_knowing_the_value(self):
        guard = PrivacyGuard()
        for secret in (TOKEN, 'sk-proj-' + 'a' * 45, 'ghp_' + 'b' * 40,
                       'Bearer ' + 'c' * 40, 'eyJ' + 'd' * 12 + '.' + 'e' * 12 + '.' + 'f' * 12,
                       '-----BEGIN PRIVATE KEY-----\nprivate bytes\n-----END PRIVATE KEY-----'):
            with self.subTest(kind=secret[:8]), self.assertRaises(GenerationFailed):
                guard.require_clean(secret)

    def test_verifier_credential_is_unavailable_not_a_pass_or_correction_input(self):
        client = Mock()
        client.send.return_value = response({'summary': AUTH, 'issues': [],
            'criteria': [{'index': 0, 'status': 'passed', 'summary': 'ok'}],
            'checks': [{'command': 'test', 'status': 'passed', 'exit_code': 0, 'summary': 'ok'}]},
            commands=[{'command': 'test', 'exit_code': 0}], tools_used=False)
        with patch('verification.repository_snapshot', return_value={'files': {}, 'git': 'fixed'}):
            result = TaskVerifier(GuardedClient(client, self.guard)).verify(
                {'repository': str(self.root), 'id': 1, 'goal': 'fix'}, PLAN,
                {'status': 'completed', 'summary': 'done', 'changes': [], 'checks': [], 'remaining': []},
                cancel_event=threading.Event(), params={})
        self.assertEqual(result['status'], 'not_run')
        self.assertFalse(result['correction_allowed'])
        self.assertNotIn(AUTH, json.dumps(result))

    def test_adapter_prepare_for_isolated_planning_contains_no_personal_conversation_id(self):
        from chatgpt_web_adapter.conversation_prepare import build_text_prepare_payload
        payload = build_text_prepare_payload('public message', model='gpt-6-instant',
                                             conversation=None, temporary=True)
        self.assertNotIn('conversation_id', payload)
        self.assertTrue(payload['history_and_training_disabled'])
        self.assertEqual(payload['parent_message_id'], 'client-created-root')


class LifecycleAcceptanceTests(unittest.TestCase):
    def test_real_check_correction_publication_and_restart_without_replay(self):
        self.run_publication_scenario('command')

    def test_natural_publication_of_existing_work_and_restart(self):
        self.run_publication_scenario('natural')

    def test_development_and_publication_in_one_human_request(self):
        self.run_publication_scenario('automatic')

    def run_publication_scenario(self, publication_mode):
        with tempfile.TemporaryDirectory() as folder, patch('builtins.print'), patch('agent_relay.record_event'):
            root = Path(folder)
            repo, remote = root / 'repo', root / 'remote.git'
            def git(*args, cwd=None):
                result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                return result.stdout.decode().strip()
            git('init', '--bare', '-q', str(remote))
            git('init', '-q', '-b', 'main', str(repo))
            git('config', 'user.name', 'Acceptance', cwd=repo)
            git('config', 'user.email', 'acceptance@example.invalid', cwd=repo)
            git('config', 'commit.gpgsign', 'false', cwd=repo)
            (repo / 'calc.py').write_text('def add(a, b): return 0\n', encoding='utf-8')
            (repo / 'check_calc.py').write_text('import calc\nassert calc.add(2,3)==5\n', encoding='utf-8')
            git('add', '.', cwd=repo)
            git('commit', '-qm', 'Base', cwd=repo)
            origin = 'https://github.com/example/acceptance.git'
            git('remote', 'add', 'origin', origin, cwd=repo)
            git('push', str(remote), 'HEAD:refs/heads/main', cwd=repo)
            original = git('rev-parse', 'HEAD', cwd=repo)
            executor, chat, api, publisher = Mock(), Mock(), Mock(), Mock()
            api.call.return_value = {'id': 5, 'username': 'MyBot'}
            chat.send.return_value = response(PLAN)
            executions, checks = [], []
            command = 'python -B check_calc.py'
            def execute(prompt, **params):
                self.assertNotIn(PRIVATE, prompt)
                if params.get('verification'):
                    run = subprocess.run([sys.executable, '-B', 'check_calc.py'], cwd=repo,
                                         capture_output=True, timeout=5)
                    checks.append(run.returncode)
                    state = 'failed' if run.returncode else 'passed'
                    return response({'summary': 'Addition regression', 'issues': [],
                        'criteria': [{'index': i, 'status': state, 'summary': '2 + 3 equals 5'}
                                     for i in range(len(json.loads(prompt.split('\n')[-1])['acceptance_criteria']))],
                        'checks': [{'command': command, 'status': state, 'exit_code': run.returncode,
                                    'summary': 'Real Python check'}]},
                        commands=[{'command': command, 'exit_code': run.returncode}], tools_used=False)
                executions.append(prompt)
                self.assertFalse(params['allow_publish'])
                operation = '-' if len(executions) == 1 else '+'
                (repo / 'calc.py').write_text(f'def add(a,b): return a {operation} b\n', encoding='utf-8')
                return response({'status': 'completed', 'summary': 'Addition updated',
                    'changes': [{'path': 'calc.py', 'summary': 'Fix addition'}], 'checks': [], 'remaining': []})
            executor.send.side_effect = execute
            real_publisher = GitPublisher(repo)
            original_git = real_publisher.git
            def offline_git(*args, **kwargs):
                # Only the network destination is substituted. Git still commits,
                # pushes and confirms the exact remote branch in a real bare repo.
                return original_git(*(str(remote) if arg == origin else arg for arg in args), **kwargs)
            def publish(message, *, plan, **params):
                with patch.object(real_publisher, 'git', side_effect=offline_git):
                    method = real_publisher.publish_current if plan.get('mode') == 'current' else real_publisher.publish_reviewed
                    outcome = method(plan, message)
                return SimpleNamespace(publication=outcome)
            publisher.send.side_effect = publish
            config = Config('fake', -123, str(repo), 'unused.json', frozenset(), frozenset({10, 11}),
                state_file=str(root / 'state.json'), min_interval_seconds=0,
                chat_conversation_id='unused-private-history', codex_allow_push=True, bot_aliases=('Юки',))
            app = AgentRelay(config, api, executor, chat, publisher=publisher)
            uid = itertools.count(1)
            def send(text, user=10):
                n = next(uid)
                event = {'update_id': n, 'message': {'message_id': n, 'message_thread_id': 7,
                    'chat': {'id': -123, 'type': 'supergroup'}, 'from': {'id': user}, 'text': text}}
                app.process_update(event)
                return event
            send('/discuss')
            send('/task')
            self.assertEqual(app.tasks.mode(-123, 7, 10, default='chat'), 'codex')
            self.assertEqual(app.tasks.mode(-123, 7, 11, default='chat'), 'chat')
            task_event = send('/task fix addition and push changes' if publication_mode == 'automatic' else '/task fix addition')
            row = app.tasks.get(1)
            self.assertEqual((row['status'], row['verification'], row['attempts']), ('completed', 'passed', 2))
            self.assertEqual(checks, [1, 0])
            if publication_mode != 'automatic':
                self.assertEqual(git('rev-parse', 'HEAD', cwd=repo), original)  # no implicit commit
            self.assertTrue(json.loads(row['manifest_json'])['eligible'])
            send('/status 1')
            if publication_mode != 'automatic':
                send('/publish 1 wrong-branch')
                send('/publish 1 main', user=11)
                publisher.send.assert_not_called()
                send('Юки, сделай push' if publication_mode == 'natural' else '/publish 1 main')
            publisher.send.assert_called_once()
            result = json.loads(app.tasks.get(1)['publication_json'])
            remote_head = git('--git-dir', str(remote), 'rev-parse', 'refs/heads/main')
            self.assertEqual(result['commit'], remote_head)
            self.assertTrue(result['verified'])
            self.assertEqual(result['branch'], 'main')
            self.assertNotEqual(remote_head, original)
            app = AgentRelay(config, api, executor, chat, publisher=publisher)
            app.process_update(task_event)  # cursor must reject an old update
            send('/report 1')
            send('/status 1')
            self.assertEqual(len(executions), 2)
            publisher.send.assert_called_once()
            self.assertEqual(app.tasks.mode(-123, 7, 10, default='chat'), 'codex')
            posted = '\n'.join(c.args[1]['text'] for c in api.call.call_args_list if c.args[0] == 'sendMessage')
            self.assertIn(remote_head, posted)
            self.assertIn('python -B check_calc.py', posted)
            self.assertIn('codex\n\n', posted)


if __name__ == '__main__':
    unittest.main()
