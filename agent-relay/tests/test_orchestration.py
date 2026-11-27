import copy
import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config, GenerationTask
from generation import GenerationCancelled, GenerationFailed
from orchestration import (HOST_CONSTRAINTS, MAX_PUBLIC_CONTEXT_CHARS, TaskOrchestrator,
                           bounded_public_context, prepare_work_branch)
from task_protocol import OrchestrationError, format_report, parse_plan, parse_report
from task_store import TaskStore, task_status


PLAN = {'goal': 'Add calculator validation', 'constraints': ['Preserve the UI'],
        'acceptance_criteria': ['Invalid input shows an error']}
REPORT = {'status': 'completed', 'summary': 'Calculator updated',
          'changes': [{'path': 'index.html', 'summary': 'Validate input'}],
          'checks': [{'command': 'npm test', 'status': 'passed', 'exit_code': 0,
                      'summary': 'Existing tests passed'}], 'remaining': []}


def response(data):
    return SimpleNamespace(text=json.dumps(data, ensure_ascii=False))


def verification_response(prompt):
    order = json.loads(prompt.split('\n')[-1])
    result = response({'summary': 'Independent tests passed', 'issues': [], 'checks': REPORT['checks'],
                       'criteria': [{'index': i, 'status': 'passed', 'summary': 'Reviewed against actual source'}
                                    for i in range(len(order['acceptance_criteria']))]})
    result.commands = [{'command': 'npm test', 'exit_code': 0}]
    result.tools_used = False
    return result


class ProtocolTests(unittest.TestCase):
    def test_strict_plans_accept_only_known_complete_json_wrappers(self):
        for wrapper in ('{}', '```json\n{}\n```', '<escape>{}</escape>',
                        '<CodeBlock language="json" id="plan-1">{}</CodeBlock>',
                        '<escape><codeblock lang=json>{}</codeblock></escape>'):
            self.assertEqual(parse_plan(wrapper.replace('{}', json.dumps(PLAN))), PLAN)
        for value in ('explanation ' + json.dumps(PLAN), '[]',
                      '<codeblock execute="true">' + json.dumps(PLAN) + '</codeblock>',
                      '{"goal":"a","goal":"b","constraints":[],"acceptance_criteria":["x"]}',
                      json.dumps({**PLAN, 'repository': 'other-repo'}),
                      json.dumps({**PLAN, 'acceptance_criteria': []}),
                      json.dumps({**PLAN, 'constraints': ['x' * 501]})):
            with self.subTest(value=value[:80]), self.assertRaises(OrchestrationError):
                parse_plan(value)

    def test_report_checks_have_consistent_exit_codes(self):
        self.assertEqual(parse_report(json.dumps(REPORT)), REPORT)
        for status, code in (('passed', None), ('passed', 1), ('passed', False),
                             ('failed', 0), ('not_run', 0), ('unknown', None)):
            data = copy.deepcopy(REPORT)
            data['checks'][0].update(status=status, exit_code=code)
            with self.subTest(status=status, code=code), self.assertRaises(OrchestrationError):
                parse_report(json.dumps(data))

    def test_report_paths_cannot_escape_or_expose_runtime_credentials(self):
        for path in ('../outside', '/outside', 'C:\\private', '.env', 'auth_data.json',
                     '.git/config', 'tasks.sqlite3', 'folder/.ssh/key'):
            data = copy.deepcopy(REPORT)
            data['changes'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(OrchestrationError):
                parse_report(json.dumps(data))

    def test_report_rejects_unknown_fields_and_never_echoes_bad_response(self):
        secret = 'private response secret'
        for value in (secret, json.dumps({**REPORT, 'execute': 'shell command'}),
                      json.dumps({**REPORT, 'changes': ['not an object']}),
                      json.dumps({**REPORT, 'summary': 'x' * 4001})):
            with self.assertRaises(OrchestrationError) as caught:
                parse_report(value)
            self.assertNotIn(secret, str(caught.exception))

    def test_empty_checks_are_not_formatted_as_success(self):
        data = {**REPORT, 'checks': [], 'changes': []}
        text = format_report(parse_report(json.dumps(data)))
        self.assertIn('не указал проверок', text)
        self.assertIn('без независимого подтверждения', text)
        self.assertIn('критерии ещё не проверены', text)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        snapshot = patch('verification.repository_snapshot', return_value={'files': {}, 'git': 'fixed'})
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = TaskStore(str(Path(self.folder.name) / 'tasks.sqlite3'))
        self.task_id = self.store.create(update_id=1, chat_id=-123, topic_id=7, owner_id=10,
                                        owner_is_bot=False, message_id=1, repository=self.folder.name,
                                        goal='Human request: fix calculator without dependencies')
        self.store.set_status(self.task_id, 'running', count_attempt=False)
        self.task = GenerationTask(1, {'from': {'id': 10}, 'reply_to_message': {'text': 'public context'}},
                                   'AMBIENT GROUP CONTEXT MUST NOT BE FORWARDED', {'model': 'gpt-6.1-terra'},
                                   work_id=self.task_id)
        self.planner, self.executor = Mock(return_value=response(PLAN)), Mock()
        self.executor.send.return_value = response(REPORT)
        self.executor.send.side_effect = lambda prompt, **params: (verification_response(prompt)
            if params.get('verification') else self.executor.send.return_value)
        self.runner = TaskOrchestrator(self.store, self.executor, planner=self.planner,
                                       planner_params={'model': 'chat-model', 'reasoning_effort': 'low'})

    def test_plan_precedes_execution_and_independent_report_is_persisted_separately(self):
        def execute(prompt, **kwargs):
            if kwargs.get('verification'):
                self.assertEqual(self.store.get(self.task_id)['status'], 'checking')
                return verification_response(prompt)
            row = self.store.get(self.task_id)
            self.assertEqual(row['stage'], 'executing')
            self.assertEqual(row['attempts'], 1)
            self.assertTrue(row['plan_json'])
            self.assertTrue(kwargs['structured_report'])
            self.assertEqual(kwargs['model'], 'gpt-6.1-terra')
            self.assertIn(self.folder.name.replace('\\', '\\\\'), prompt)
            self.assertIn('original_human_request', prompt)
            self.assertIn('Preserve the UI', prompt)
            self.assertIn('Do not attempt to create or switch branches', prompt)
            self.assertIn('full specification file is absent', prompt)
            self.assertNotIn('AMBIENT GROUP CONTEXT', prompt)
            return response(REPORT)
        self.executor.send.side_effect = execute
        outcome = self.runner.run(self.task)
        row = self.store.get(self.task_id)
        self.assertEqual(json.loads(row['plan_json'])['host_constraints'], list(HOST_CONSTRAINTS))
        self.assertEqual(json.loads(row['checks_json']), REPORT['checks'])
        self.assertEqual(json.loads(row['report_json']), REPORT)
        self.assertEqual(json.loads(row['criteria_json']), PLAN['acceptance_criteria'])
        self.assertEqual(row['verification'], 'passed')
        self.assertEqual(row['stage'], 'verification_finished')
        self.assertEqual(outcome.work_status, 'completed')
        self.assertIn('без независимого подтверждения', outcome.text)
        self.assertIn('Проверка пройдена', task_status(row))
        self.assertEqual(self.planner.call_args.kwargs['model'], 'chat-model')
        self.assertNotIn('allow_publish', self.planner.call_args.kwargs)
        self.assertNotIn(self.folder.name, self.planner.call_args.args[0])

    def test_public_group_agreement_reaches_planner_and_executor(self):
        peer_plan = 'Peer agreed: first stage parses Guitar Pro files and displays tuning.'
        self.task.public_context = '\n'.join(json.dumps({'message_id': i, 'sender_kind': 'bot',
            'author': 'PeerBot', 'text': peer_plan if i == 2 else 'Earlier public detail'},
            ensure_ascii=False) for i in (1, 2))
        self.runner.run(self.task)
        planner_input = self.planner.call_args.args[0]
        executor_input = next(call.args[0] for call in self.executor.send.call_args_list
                              if not call.kwargs.get('verification'))
        self.assertIn(peer_plan, planner_input)
        self.assertIn(peer_plan, executor_input)
        self.assertIn('empty repository alone does not block', planner_input)
        self.assertNotIn('AMBIENT GROUP CONTEXT', executor_input)

    def test_context_budget_keeps_complete_recent_messages(self):
        old = json.dumps({'text': 'old' * 6000})
        recent = json.dumps({'text': 'latest agreement'})
        self.assertEqual(bounded_public_context(old + '\n' + recent), recent)
        self.assertLessEqual(len(bounded_public_context(recent)), MAX_PUBLIC_CONTEXT_CHARS)

    def test_requested_branch_is_created_by_host_and_main_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            def git(*args):
                return subprocess.run(['git', '-C', folder, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-b', 'main')
            Path(folder, 'README.md').write_text('initial\n', encoding='utf-8')
            git('add', 'README.md')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                '-c', 'core.hooksPath=NUL', 'commit', '-m', 'Initial')
            main_head = git('rev-parse', 'HEAD')
            record = {'repository': folder, 'goal': 'Начинай разработку в своей ветке', 'id': 12}
            self.assertEqual(prepare_work_branch(record), 'agentrelay/task-12')
            self.assertEqual(git('rev-parse', 'HEAD'), main_head)
            self.assertEqual(git('rev-parse', 'main'), main_head)
            self.assertEqual(prepare_work_branch(record), 'agentrelay/task-12')

    def test_requested_branch_refuses_dirty_main(self):
        with tempfile.TemporaryDirectory() as folder:
            def git(*args):
                return subprocess.run(['git', '-C', folder, *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git('init', '-b', 'main')
            Path(folder, 'README.md').write_text('initial\n', encoding='utf-8')
            git('add', 'README.md')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                '-c', 'core.hooksPath=NUL', 'commit', '-m', 'Initial')
            Path(folder, 'README.md').write_text('changed\n', encoding='utf-8')
            with self.assertRaisesRegex(GenerationFailed, 'working tree has changes'):
                prepare_work_branch({'repository': folder, 'goal': 'Work in a new branch', 'id': 13})
            self.assertEqual(git('branch', '--show-current'), 'main')

    def test_planning_failure_never_launches_codex_or_retries(self):
        for result in ('malformed private planner response', json.dumps({**PLAN, 'allow_publish': True})):
            self.planner.return_value = SimpleNamespace(text=result)
            with self.assertRaises(OrchestrationError):
                self.runner.run(self.task)
            self.executor.send.assert_not_called()
            self.assertEqual(self.store.get(self.task_id)['attempts'], 0)
            self.assertEqual(self.store.get(self.task_id)['stage'], 'planning_failed')

    def test_unavailable_planner_does_not_silently_switch_to_codex_only_mode(self):
        self.planner.side_effect = GenerationFailed('ChatGPT unavailable')
        with self.assertRaises(GenerationFailed):
            self.runner.run(self.task)
        self.planner.assert_called_once()
        self.executor.send.assert_not_called()

    def test_codex_only_installation_builds_an_explicit_local_work_order(self):
        runner = TaskOrchestrator(self.store, self.executor)
        runner.run(self.task)
        order = json.loads(self.store.get(self.task_id)['plan_json'])
        self.assertEqual(order['planner'], 'local')
        self.assertEqual(order['goal'], self.store.get(self.task_id)['goal'])
        self.assertTrue(order['acceptance_criteria'])
        self.planner.assert_not_called()

    def test_cancel_during_planning_prevents_execution(self):
        def plan(*args, **kwargs):
            self.task.cancelled.set()
            return response(PLAN)
        self.planner.side_effect = plan
        with self.assertRaises(GenerationCancelled):
            self.runner.run(self.task)
        self.executor.send.assert_not_called()
        self.assertEqual(self.store.get(self.task_id)['plan_json'], '')

    def test_cancel_after_plan_is_saved_still_prevents_execution(self):
        original = self.store.save_plan
        def save(*args):
            saved = original(*args)
            self.task.cancelled.set()
            return saved
        with patch.object(self.store, 'save_plan', side_effect=save):
            with self.assertRaises(GenerationCancelled):
                self.runner.run(self.task)
        self.executor.send.assert_not_called()

    def test_late_executor_response_cannot_overwrite_cancellation(self):
        def execute(*args, **kwargs):
            self.store.request_cancel(self.task_id, -123, 7, 10)
            self.task.cancelled.set()
            return response(REPORT)
        self.executor.send.side_effect = execute
        with self.assertRaises(GenerationCancelled):
            self.runner.run(self.task)
        self.assertEqual(self.store.get(self.task_id)['report_json'], '')

    def test_explicit_recheck_verifies_saved_work_without_replaying_development(self):
        self.runner.run(self.task)
        self.store.set_status(self.task_id, 'failed')
        with self.store.connection() as db:
            db.execute('UPDATE tasks SET baseline_json=? WHERE id=?',
                       (json.dumps({'snapshot': {}, 'dirty': []}), self.task_id))
        attempts = self.store.get(self.task_id)['attempts']
        self.assertTrue(self.store.prepare_recheck(self.task_id, (-123, 7, 10), 2, 'Сделай push'))
        self.store.set_status(self.task_id, 'running', count_attempt=False)
        self.executor.reset_mock()
        self.planner.reset_mock()
        self.runner.verifier = Mock()
        self.runner.verifier.verify.return_value = {
            'status': 'passed', 'summary': 'Rechecked', 'criteria': [], 'checks': [], 'issues': []}
        with patch('orchestration.seal', return_value={'eligible': True, 'branch': 'main'}):
            result = self.runner.recheck(self.task)
        self.assertEqual(result.work_status, 'completed')
        self.executor.send.assert_not_called()
        self.planner.assert_not_called()
        self.runner.verifier.verify.assert_called_once()
        self.assertEqual(self.store.get(self.task_id)['attempts'], attempts)

    def test_failed_declared_check_is_stopped_after_two_corrections(self):
        report = copy.deepcopy(REPORT)
        report['checks'][0].update(status='failed', exit_code=1, summary='Tests failed')
        self.executor.send.return_value = response(report)
        self.assertEqual(self.runner.run(self.task).work_status, 'failed')
        self.assertEqual(self.executor.send.call_count, 6)
        self.assertEqual(self.store.get(self.task_id)['verification'], 'failed')
        self.assertEqual(self.store.get(self.task_id)['attempts'], 3)
        self.assertEqual(self.store.get(self.task_id)['stop_reason'], 'corrections_exhausted')

    def test_malformed_executor_report_fails_without_replaying_edits(self):
        self.executor.send.return_value = SimpleNamespace(text='private raw output')
        with self.assertRaises(OrchestrationError) as caught:
            self.runner.run(self.task)
        self.assertNotIn('private raw output', str(caught.exception))
        self.assertEqual(self.store.get(self.task_id)['stage'], 'report_parsing_failed')
        self.executor.send.assert_called_once()


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        snapshot = patch('verification.repository_snapshot', return_value={'files': {}, 'git': 'fixed'})
        snapshot.start()
        self.addCleanup(snapshot.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api, self.executor, self.chat = Mock(), Mock(), Mock()
        self.api.call.return_value = {'id': 5, 'username': 'MyBot'}
        self.executor.send.return_value = response(REPORT)
        self.executor.send.side_effect = lambda prompt, **params: (verification_response(prompt)
            if params.get('verification') else self.executor.send.return_value)
        self.chat.send.return_value = response(PLAN)
        self.config = Config('fake', -123, self.folder.name, 'unused.json', frozenset(),
                             frozenset({10}), min_interval_seconds=0,
                             state_file=str(Path(self.folder.name) / 'state.json'),
                             chat_conversation_id='existing-chat')
        self.app = AgentRelay(self.config, self.api, self.executor, self.chat)

    def event(self, text):
        return {'update_id': 1, 'message': {'message_id': 1, 'chat': {'id': -123, 'type': 'supergroup'},
                                          'from': {'id': 10}, 'text': text}}

    def test_human_task_uses_real_orchestrator_and_persists_outcome(self):
        self.app.process_update(self.event('/task fix calculator'))
        self.chat.send.assert_not_called()
        self.assertEqual(self.executor.send.call_count, 2)
        row = self.app.tasks.get(1)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['verification'], 'passed')
        self.assertEqual(row['attempts'], 1)
        posted = [call.args[1]['text'] for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']
        self.assertTrue(posted[-1].startswith('codex\n\nЗадача #1'))
        self.assertIn('npm test', posted[-1])

    def test_task_receives_only_trusted_same_topic_history(self):
        self.app.config = replace(self.config, peers=frozenset({'peerbot'}),
                                  peer_bot_ids=frozenset({20}))
        peer = self.event('Stage one: read tabs and display tuning')
        peer['message']['from'] = {'id': 20, 'username': 'PeerBot', 'is_bot': True}
        self.app.process_update(peer)
        other = self.event('UNRELATED-TOPIC-SECRET')
        other['update_id'] = other['message']['message_id'] = 2
        other['message']['message_thread_id'] = 9
        self.app.process_update(other)
        request = self.event('/task Implement the stage agreed with PeerBot')
        request['update_id'] = request['message']['message_id'] = 3
        self.app.process_update(request)
        prompt = next(call.args[0] for call in self.executor.send.call_args_list
                      if not call.kwargs.get('verification'))
        self.assertIn('Stage one: read tabs and display tuning', prompt)
        self.assertNotIn('UNRELATED-TOPIC-SECRET', prompt)

    def test_bad_plan_results_in_failed_task_and_no_executor_call(self):
        self.chat.send.return_value = SimpleNamespace(text='bad plan')
        self.app.tasks.set_mode(-123, 0, 10, 'codex')
        self.app.process_update(self.event('@MyBot fix calculator'))
        self.executor.send.assert_not_called()
        self.assertEqual(self.app.tasks.get(1)['status'], 'failed')
        self.assertEqual(self.app.tasks.get(1)['attempts'], 0)

    def test_declared_executor_failure_marks_task_failed_instead_of_completed(self):
        self.executor.send.return_value = response({**REPORT, 'status': 'blocked', 'remaining': ['Missing dependencies']})
        self.app.process_update(self.event('/task fix calculator'))
        self.assertEqual(self.app.tasks.get(1)['status'], 'failed')
        self.assertEqual(self.app.tasks.get(1)['verification'], 'not_run')
        self.assertEqual(self.app.tasks.get(1)['stop_reason'], 'executor_blocked')
        self.executor.send.assert_called_once()

    def test_unavailable_verification_preserves_report_and_does_not_retry_edits(self):
        def send(prompt, **params):
            if params.get('verification'):
                raise GenerationFailed('Verifier unavailable')
            return response(REPORT)
        self.executor.send.side_effect = send
        self.app.process_update(self.event('/task fix calculator'))
        row = self.app.tasks.get(1)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['verification'], 'not_run')
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(json.loads(row['report_json']), REPORT)
        self.assertEqual(self.executor.send.call_count, 2)
        texts = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
        self.assertIn('Проверка не выполнена', texts[-1])
        self.assertIn('Calculator updated', texts[-1])

    def test_status_discussion_and_cancel_work_while_verifying(self):
        checking, chatted, cancelled = threading.Event(), threading.Event(), threading.Event()
        def send(prompt, *, cancel_event, **params):
            if not params.get('verification'):
                return response(REPORT)
            checking.set()
            if not cancel_event.wait(5):
                raise AssertionError('Verification was not cancelled')
            cancelled.set()
            return verification_response(prompt)  # deliberately late success
        def chat(prompt, **params):
            if prompt.startswith('[AgentRelay / task planning'):
                return response(PLAN)
            chatted.set()
            return SimpleNamespace(text='Discussion during verification')
        self.executor.send.side_effect, self.chat.send.side_effect = send, chat
        self.app.cursor.advance(1)
        polls, failures = [], []
        def update(number, text):
            event = self.event(text)
            event['update_id'], event['message']['message_id'] = number, number
            return event
        def api(method, data=None, **kwargs):
            if method == 'getWebhookInfo':
                return {}
            if method == 'sendMessage':
                return {}
            if method != 'getUpdates':
                raise AssertionError('Unexpected API method')
            polls.append(data)
            try:
                if len(polls) == 1:
                    return [update(1, '/task fix calculator')]
                if len(polls) == 2:
                    self.assertTrue(checking.wait(2))
                    self.assertEqual(self.app.tasks.get(1)['status'], 'checking')
                    return [update(2, '/status 1'), update(3, '/discuss explain this')]
                if len(polls) == 3:
                    self.assertTrue(chatted.wait(2))
                    texts = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
                    self.assertIn('Проверяется', '\n'.join(texts))
                    return [update(4, '/cancel 1')]
                self.assertTrue(cancelled.wait(2))
            except AssertionError as exc:
                failures.append(str(exc))
            raise KeyboardInterrupt()
        self.api.call.side_effect = api
        with patch('builtins.print'):
            self.app.serve()
        self.assertEqual(failures, [])
        row = self.app.tasks.get(1)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['verification_json'], '')
        self.assertEqual(self.executor.send.call_count, 2)

    def test_status_discussion_and_emergency_stop_work_during_correction(self):
        correcting, chatted, cancelled = threading.Event(), threading.Event(), threading.Event()
        def send(prompt, *, cancel_event, **params):
            if params.get('verification'):
                review_response = verification_response(prompt)
                data = json.loads(review_response.text)
                data['criteria'][0].update(status='failed', summary='Input validation still missing')
                review_response.text = json.dumps(data)
                return review_response
            if prompt.startswith('[AgentRelay / correction pass]'):
                correcting.set()
                if not cancel_event.wait(5):
                    raise AssertionError('Correction was not stopped')
                cancelled.set()
            return response(REPORT)
        def chat(prompt, **params):
            if prompt.startswith('[AgentRelay / task planning'):
                return response(PLAN)
            chatted.set()
            return SimpleNamespace(text='Discussion during correction')
        self.executor.send.side_effect, self.chat.send.side_effect = send, chat
        self.app.cursor.advance(1)
        polls, failures = [], []
        def update(number, text):
            event = self.event(text)
            event['update_id'], event['message']['message_id'] = number, number
            return event
        def api(method, data=None, **kwargs):
            if method == 'getWebhookInfo':
                return {}
            if method == 'sendMessage':
                return {}
            if method != 'getUpdates':
                raise AssertionError('Unexpected API method')
            polls.append(data)
            try:
                if len(polls) == 1:
                    return [update(1, '/task fix calculator')]
                if len(polls) == 2:
                    self.assertTrue(correcting.wait(2))
                    self.assertEqual(self.app.tasks.get(1)['attempts'], 2)
                    return [update(2, '/status 1'), update(3, '/discuss explain this')]
                if len(polls) == 3:
                    self.assertTrue(chatted.wait(2))
                    texts = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
                    self.assertIn('Исправление по замечаниям', '\n'.join(texts))
                    self.assertIn('Исправлений: 1 из 2', '\n'.join(texts))
                    return [update(4, '/stop')]
                self.assertTrue(cancelled.wait(2))
            except AssertionError as exc:
                failures.append(str(exc))
            raise KeyboardInterrupt()
        self.api.call.side_effect = api
        with patch('builtins.print'):
            self.app.serve()
        self.assertEqual(failures, [])
        row = self.app.tasks.get(1)
        self.assertEqual(row['status'], 'cancelled')
        self.assertEqual(row['attempts'], 2)
        self.assertEqual(self.executor.send.call_count, 3)
        history = self.app.tasks.attempt_history(1)
        self.assertEqual(history[1]['report_json'], '')

    def test_explicit_handoff_still_routes_after_structured_report_formatting(self):
        self.config = replace(self.config, peers=frozenset({'friendbot'}), peer_bot_ids=frozenset({20}))
        self.app = AgentRelay(self.config, self.api, self.executor, self.chat)
        self.executor.send.return_value = response({**REPORT, 'summary': 'Done [[handoff:FriendBot]]'})
        self.app.process_update(self.event('/task fix calculator and hand the floor to FriendBot'))
        texts = [call.args[1]['text'] for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']
        self.assertIn('/relay@FriendBot', texts[-1])
        self.assertNotIn('[[handoff:', texts[-1])
        self.assertIn('allowed_peer_bots', self.executor.send.call_args_list[0].args[0])

    def test_status_and_cancel_are_available_during_live_planning(self):
        self.app.tasks.set_mode(-123, 0, 10, 'codex')
        entered, cancelled = threading.Event(), threading.Event()
        def plan(*args, cancel_event, **kwargs):
            entered.set()
            if not cancel_event.wait(3):
                raise AssertionError('Planning was not cancelled')
            cancelled.set()
            return response(PLAN)
        self.chat.send.side_effect = plan
        self.app.cursor.advance(1)
        polls, failures = [], []
        def api(method, data=None, **kwargs):
            if method == 'getWebhookInfo':
                return {}
            if method == 'sendMessage':
                return {}
            if method != 'getUpdates':
                raise AssertionError('Unexpected API method')
            polls.append(data)
            try:
                if len(polls) == 1:
                    return [self.event('@MyBot fix calculator')]
                if len(polls) == 2:
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(self.app.tasks.get(1)['stage'], 'planning')
                    self.assertEqual(self.app.tasks.get(1)['attempts'], 0)
                    status = self.event('/status 1')
                    status['update_id'], status['message']['message_id'] = 2, 2
                    cancel = self.event('/cancel 1')
                    cancel['update_id'], cancel['message']['message_id'] = 3, 3
                    return [status, cancel]
                self.assertTrue(cancelled.wait(1))
            except AssertionError as exc:
                failures.append(str(exc))
            raise KeyboardInterrupt()
        self.api.call.side_effect = api
        with patch('builtins.print'):
            self.app.serve()
        self.assertEqual(failures, [])
        self.executor.send.assert_not_called()
        self.assertEqual(self.app.tasks.get(1)['status'], 'cancelled')
        texts = [call.args[1]['text'] for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']
        self.assertIn('Постановка задания', '\n'.join(texts))

    def test_discussion_continues_after_planning_while_executor_is_blocked(self):
        executing, chat_answered, cancelled = threading.Event(), threading.Event(), threading.Event()
        def chat(prompt, **kwargs):
            if prompt.startswith('[AgentRelay / task planning'):
                return response(PLAN)
            chat_answered.set()
            return SimpleNamespace(text='ordinary chat can answer')
        def execute(*args, cancel_event, **kwargs):
            executing.set()
            if not cancel_event.wait(3):
                raise AssertionError('Executor was not cancelled')
            cancelled.set()
            raise GenerationCancelled()
        self.chat.send.side_effect, self.executor.send.side_effect = chat, execute
        self.app.cursor.advance(1)
        polls, failures = [], []
        def api(method, data=None, **kwargs):
            if method == 'getWebhookInfo':
                return {}
            if method == 'sendMessage':
                return {}
            if method != 'getUpdates':
                raise AssertionError('Unexpected API method')
            polls.append(data)
            try:
                if len(polls) == 1:
                    return [self.event('/task fix calculator')]
                if len(polls) == 2:
                    self.assertTrue(executing.wait(1))
                    question = self.event('/discuss explain this')
                    question['update_id'], question['message']['message_id'] = 2, 2
                    return [question]
                if len(polls) == 3:
                    self.assertTrue(chat_answered.wait(1))
                    # The backend returned, but durable public notes may still
                    # be saving before the future is ready for delivery.
                    if self.app._chat_active is not None:
                        self.app._chat_active.future.result(timeout=2)
                    return []
                if len(polls) == 4:
                    texts = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
                    self.assertIn('ordinary chat can answer', '\n'.join(texts))
                    cancel = self.event('/cancel 1')
                    cancel['update_id'], cancel['message']['message_id'] = 3, 3
                    return [cancel]
                self.assertTrue(cancelled.wait(1))
            except AssertionError as exc:
                failures.append(str(exc))
            raise KeyboardInterrupt()
        self.api.call.side_effect = api
        with patch('builtins.print'):
            self.app.serve()
        self.assertEqual(failures, [])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.app.tasks.get(1)['attempts'], 1)

    def test_planner_waiting_for_shared_chat_can_be_cancelled(self):
        entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
        def blocked_chat(*args, **kwargs):
            entered.set()
            release.wait(3)
            return SimpleNamespace(text='ordinary answer')
        self.chat.send.side_effect = blocked_chat
        job = self.app.tasks.create(update_id=1, chat_id=-123, topic_id=0, owner_id=10,
                                    owner_is_bot=False, message_id=1, repository=self.folder.name, goal='fix')
        self.app.tasks.set_status(job, 'running', count_attempt=False)
        task = GenerationTask(1, {}, 'not forwarded', {}, work_id=job, cancelled=cancel)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                ordinary = pool.submit(self.app._send_chat, 'ordinary question', cancel_event=threading.Event())
                self.assertTrue(entered.wait(1))
                planning = pool.submit(self.app.orchestrator.run, task)
                cancel.set()
                try:
                    with self.assertRaises(GenerationCancelled):
                        planning.result(timeout=1)
                    self.executor.send.assert_not_called()
                    self.chat.send.assert_called_once()
                finally:
                    release.set()
                self.assertEqual(ordinary.result(timeout=1).text, 'ordinary answer')
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
