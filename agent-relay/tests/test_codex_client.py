import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import agent_relay
from codex_client import CodexCLIClient, CodexError, SessionStore, child_environment, check_login, cli_failure, validate_parameters, validate_workspace


THREAD = '0199a213-81c0-7800-8aa1-bbab2a035a53'


class CodexClientTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.session = self.root / 'session.json'
        self.worker = self.root / 'fake_codex.py'
        self.worker.write_text(
            'import json,sys\nfrom pathlib import Path\n'
            f'THREAD={THREAD!r}\n'
            'prompt=sys.stdin.read()\nargs=sys.argv[1:]\n'
            'Path("last-call.json").write_text(json.dumps({"args":args,"prompt":prompt}),encoding="utf-8")\n'
            'if "--output-schema" in args:\n'
            ' schema=Path(args[args.index("--output-schema")+1]).read_text(encoding="utf-8")\n'
            ' Path("last-schema.json").write_text(schema,encoding="utf-8")\n'
            'print(json.dumps({"type":"thread.started","thread_id":THREAD}),flush=True)\n'
            'print(json.dumps({"type":"item.completed","item":{"type":"command_execution","command":"npm test","exit_code":0,"aggregated_output":"private tool output"}}))\n'
            'if prompt=="fail":\n'
            ' print(json.dumps({"type":"turn.failed"}))\n'
            ' print("private raw log",file=sys.stderr)\n'
            ' sys.exit(1)\n'
            'if prompt=="structured-fail":\n'
            ' print(json.dumps({"type":"error","error":{"code":"model_not_found","status":400}}))\n'
            ' print(json.dumps({"type":"turn.failed"}))\n'
            ' print("HTTP 401 telemetry noise",file=sys.stderr)\n'
            ' sys.exit(1)\n'
            'if prompt=="reconnect":\n'
            ' print(json.dumps({"type":"error","message":"Reconnecting 1/5"}))\n'
            'answer=Path(args[args.index("--output-last-message")+1])\n'
            'answer.write_text("final:"+prompt,encoding="utf-8")\n'
            'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"commentary"}}))\n'
            'print(json.dumps({"type":"turn.completed"}))\n', encoding='utf-8',
        )

    def client(self):
        with patch('codex_client.validate_workspace', return_value=self.root), \
             patch('codex_client.resolve_codex_binary', return_value='fake'):
            return CodexCLIClient(workdir=str(self.root), session_file=str(self.session))

    def run_turn(self, prompt, **kwargs):
        with patch('codex_client.codex_command', return_value=[sys.executable, str(self.worker)]):
            return self.client().send(prompt, **kwargs)

    def test_final_message_and_explicit_session_resume(self):
        prompt = 'literal `echo nope` $(nope) "quoted"\nsecond line'
        self.assertEqual(self.run_turn(prompt).text, 'final:' + prompt)
        first = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
        self.assertEqual(first['prompt'], prompt)
        self.assertNotIn(prompt, first['args'])
        self.assertNotIn('resume', first['args'])
        self.assertNotIn('--last', first['args'])
        self.assertIn('workspace-write', first['args'])
        self.assertGreater(first['args'].index('-s'), first['args'].index('exec'))
        self.assertGreater(first['args'].index('-C'), first['args'].index('exec'))
        self.assertIn('sandbox_mode="workspace-write"', first['args'])
        self.assertIn('approval_policy="never"', first['args'])
        if os.name == 'nt':
            self.assertIn('windows.sandbox="elevated"', first['args'])
        self.assertIn('never', first['args'])
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', first['args'])
        self.assertEqual(SessionStore(str(self.session), self.root).read(), THREAD)
        self.assertEqual(self.run_turn('continue').text, 'final:continue')
        second = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
        self.assertEqual(second['args'][second['args'].index('resume') + 1], THREAD)
        self.assertLess(second['args'].index('-s'), second['args'].index('resume'))
        self.assertGreater(second['args'].index('sandbox_mode="workspace-write"'), second['args'].index('resume'))
        if os.name == 'nt':
            self.assertIn('windows.sandbox="elevated"', second['args'])

    def test_rendered_command_evidence_longer_than_report_limit_is_retained(self):
        from task_protocol import MAX_COMMAND_CHARS
        # Windows argv quoting can exceed the inner script length. Retain exact evidence.
        source = self.worker.read_text(encoding='utf-8')
        command = 'powershell.exe -Command ' + 'x' * (MAX_COMMAND_CHARS + 100)
        self.worker.write_text(source.replace('"command":"npm test"', '"command":' + repr(command)), encoding='utf-8')
        result = self.run_turn('long command evidence')
        self.assertEqual(result.commands, [{'command': command, 'exit_code': 0}])

    def test_workspace_change_starts_a_new_session(self):
        SessionStore(str(self.session), self.root / 'other-workspace').write(THREAD)
        self.run_turn('new workspace')
        data = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
        self.assertNotIn('resume', data['args'])

    def test_legacy_session_is_not_resumed_after_context_isolation_upgrade(self):
        self.session.write_text(json.dumps({'workspace': str(self.root), 'thread_id': THREAD}), encoding='utf-8')
        self.run_turn('new public task')
        data = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
        self.assertNotIn('resume', data['args'])
        self.assertEqual(SessionStore(str(self.session), self.root).read(), THREAD)

    def test_verification_is_fresh_read_only_and_does_not_touch_coding_session(self):
        from task_protocol import VERIFICATION_SCHEMA
        SessionStore(str(self.session), self.root).write(THREAD, interrupted=True)
        previous = self.session.read_bytes()
        result = self.run_turn('independent review', verification=True, model='gpt-6.1-terra', reasoning_effort='medium')
        data = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
        args = data['args']
        self.assertEqual(args[args.index('-s') + 1], 'read-only')
        self.assertIn('sandbox_mode="read-only"', args)
        self.assertGreater(args.index('-s'), args.index('exec'))
        if os.name == 'nt':
            self.assertIn('windows.sandbox="elevated"', args)
        self.assertIn('--ephemeral', args)
        self.assertIn('--ignore-user-config', args)
        self.assertIn('--ignore-rules', args)
        self.assertTrue(any('trust_level="untrusted"' in item for item in args))
        self.assertIn('mcp_servers.agent_relay_git.enabled=false', args)
        self.assertNotIn('resume', args)
        self.assertEqual(data['prompt'], 'independent review')
        self.assertEqual(self.session.read_bytes(), previous)
        self.assertEqual(json.loads((self.root / 'last-schema.json').read_text()), VERIFICATION_SCHEMA)
        self.assertEqual(result.commands, [{'command': 'npm test', 'exit_code': 0}])
        self.assertNotIn('private tool output', repr(result))

    def test_verification_cannot_enable_publishing(self):
        with self.assertRaises(CodexError):
            self.run_turn('publish', verification=True, allow_publish=True)
        self.assertFalse((self.root / 'last-call.json').exists())

    def test_report_schema_is_passed_to_both_new_and_resumed_cli_turns(self):
        from task_protocol import REPORT_SCHEMA
        for prompt in ('structured first', 'structured continuation'):
            self.run_turn(prompt, structured_report=True)
            data = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
            schema = json.loads((self.root / 'last-schema.json').read_text(encoding='utf-8'))
            self.assertEqual(schema, REPORT_SCHEMA)
            self.assertEqual(data['args'].count('--output-schema'), 1)
            self.assertFalse(Path(data['args'][data['args'].index('--output-schema') + 1]).exists())
            if prompt.endswith('continuation'):
                self.assertIn('resume', data['args'])

    def test_legacy_publishing_is_disabled_for_new_and_resumed_turns(self):
        with self.assertRaisesRegex(CodexError, 'relay publishes'):
            self.run_turn('publish index.html', allow_publish=True)
        self.run_turn('code task')
        first = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
        self.assertIn('mcp_servers.agent_relay_git.enabled=false', first['args'])
        self.assertIn('mcp_servers.agent_relay_git.enabled_tools=[]', first['args'])
        self.assertIn('--ignore-user-config', first['args'])
        self.assertIn('--ignore-rules', first['args'])
        self.assertIn('sandbox_workspace_write.network_access=false', first['args'])
        self.assertIn('sandbox_workspace_write.writable_roots=[]', first['args'])
        self.assertIn('--no-daemon', first['args'])
        self.assertGreater(first['args'].index('mcp_servers.agent_relay_git.enabled=false'),
                           first['args'].index('exec'))
        self.assertNotIn('publishing capability', first['prompt'])
        self.assertIn('workspace-write', first['args'])
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', first['args'])
        self.run_turn('peer request')
        second = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
        self.assertIn('resume', second['args'])
        self.assertIn('mcp_servers.agent_relay_git.enabled=false', second['args'])
        self.assertIn('--ignore-user-config', second['args'])
        self.assertIn('sandbox_workspace_write.network_access=false', second['args'])
        self.assertNotIn('publishing capability', second['prompt'])

    def test_cli_failure_does_not_expose_logs_or_tool_output(self):
        with self.assertRaises(CodexError) as caught:
            self.run_turn('fail')
        self.assertNotIn('private', str(caught.exception))
        # Save the explicitly allocated session even if the turn fails/stops.
        self.assertEqual(SessionStore(str(self.session), self.root).read(), THREAD)

    def test_interrupted_turn_is_not_automatically_continued(self):
        SessionStore(str(self.session), self.root).write(THREAD, interrupted=True)
        self.run_turn('new request')
        data = json.loads((self.root / 'last-call.json').read_text(encoding='utf-8'))
        self.assertIn('Do not continue its unfinished task automatically', data['prompt'])
        self.assertTrue(data['prompt'].endswith('new request'))
        self.assertFalse(SessionStore(str(self.session), self.root).read_state()['interrupted'])

    def test_recovered_connection_error_does_not_discard_completed_answer(self):
        self.assertEqual(self.run_turn('reconnect').text, 'final:reconnect')

    def test_corrupt_session_fails_without_resuming_somebody_else(self):
        self.session.write_text('not json', encoding='utf-8')
        with self.assertRaisesRegex(CodexError, 'session state'):
            self.run_turn('hello')

    def test_credentials_not_exported_to_coding_process(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'private', 'CWA_AUTH_FILE': 'private',
                                     'CODEX_THREAD_ID': 'unrelated-parent-session', 'PATH': 'test'}, clear=True):
            env = child_environment()
        self.assertNotIn('TELEGRAM_BOT_TOKEN', env)
        self.assertNotIn('CWA_AUTH_FILE', env)
        self.assertNotIn('CODEX_THREAD_ID', env)
        self.assertEqual(env['PATH'], 'test')

    def test_login_failure_is_actionable(self):
        with patch('codex_client.subprocess.run', return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(CodexError, 'codex login'):
                check_login('codex.exe')

    def test_model_error_takes_priority_over_telemetry_and_auth_mentions(self):
        error = json.dumps({'status': 400, 'error': {'code': 'model_not_found',
                             'message': "The 'GPT-6.1 sol' model is not supported with ChatGPT authentication"}})
        failure = cli_failure('2026-11-22T15:32:24.429Z telemetry failed', 1, error)
        self.assertIn('CODEX_MODEL', str(failure))
        self.assertNotIn('login', str(failure))

    def test_timestamp_and_request_id_digits_are_not_http_status(self):
        for log in ('2026-11-22T15:32:24.429Z private raw log',
                    '2026-11-22T15:32:24.401Z request_id=429401 path=/429/'):
            with self.subTest(log=log):
                self.assertIn('CLI failed', str(cli_failure(log, 1)))
                self.assertNotIn('private', str(cli_failure(log, 1)))

    def test_structured_error_without_message_survives_empty_failed_event(self):
        with self.assertRaisesRegex(CodexError, 'CODEX_MODEL'):
            self.run_turn('structured-fail')

    def test_http_status_and_nested_errors_are_classified(self):
        cases = [
            ('HTTP 401 Unauthorized', 'authentication'),
            ('status code: 429', 'usage limit'),
            (json.dumps({'status': 401, 'error': {'message': 'private'}}), 'authentication'),
            (json.dumps({'error': {'code': 'insufficient_quota'}}), 'usage limit'),
            (json.dumps({'message': json.dumps({'status': 400, 'error': {'code': 'model_not_found'}})}), 'CODEX_MODEL'),
            ('Invalid reasoning.effort value; private model detail', 'CODEX_REASONING_EFFORT'),
            ('This model is not supported with reasoning.effort=xhigh', 'CODEX_REASONING_EFFORT'),
            ('Failed to initialize Windows sandbox', 'sandbox could not start'),
        ]
        for log, expected in cases:
            with self.subTest(log=log):
                result = str(cli_failure(log, 1))
                self.assertIn(expected, result)
                self.assertNotIn('private', result)

    def test_display_model_name_rejected_before_starting_cli(self):
        with patch('codex_client.subprocess.Popen') as launch:
            with self.assertRaisesRegex(CodexError, 'CODEX_MODEL'):
                self.client().send('hello', model='GPT-6.1 sol')
        launch.assert_not_called()
        validate_parameters('gpt-6.1-terra', 'medium')
        with self.assertRaisesRegex(CodexError, 'CODEX_REASONING_EFFORT'):
            validate_parameters('gpt-6.1-terra', 'medum')

    def test_non_git_workspace_is_rejected(self):
        with self.assertRaisesRegex(CodexError, 'Git working tree'):
            validate_workspace(self.root)

    def test_startup_uses_configured_repo_and_cancellable_client(self):
        cfg = agent_relay.Config('token', -123, str(self.root), str(self.session), frozenset())
        with patch.object(agent_relay.Config, 'from_env', return_value=cfg), \
             patch.object(agent_relay, 'validate_workspace', return_value=self.root), \
             patch.object(agent_relay, 'resolve_codex_binary', return_value='codex.exe'), \
             patch.object(agent_relay, 'check_login') as login, \
             patch.object(agent_relay, 'BotInstance'), \
             patch.object(agent_relay, 'Telegram') as telegram, \
             patch('generation.ProcessClient') as client, \
             patch.object(agent_relay, 'AgentRelay') as relay:
            agent_relay.main()
        login.assert_called_once_with('codex.exe')
        client.assert_called_once_with(workdir=str(self.root), session_file=str(self.session),
                                       binary='codex.exe', sandbox='workspace-write', timeout_seconds=1800)
        self.assertEqual(relay.call_count, 1)
        self.assertEqual(relay.call_args.args, (cfg, telegram.return_value, client.return_value))
        self.assertIsNone(relay.call_args.kwargs['chat_client'])
        self.assertIsInstance(relay.call_args.kwargs['privacy'], agent_relay.PrivacyGuard)
        relay.return_value.serve.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
