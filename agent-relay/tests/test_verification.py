import copy
import json
import shlex
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from generation import GenerationCancelled, GenerationFailed
from task_protocol import OrchestrationError, parse_verification
from verification import TaskVerifier, repository_snapshot


PLAN = {'goal': 'Fix calculator', 'constraints': ['Preserve UI'],
        'acceptance_criteria': ['Invalid input shows an error']}
REPORT = {'status': 'completed', 'summary': 'Fixed', 'changes': [], 'checks': [], 'remaining': []}
REVIEW = {'summary': 'Tests and input handling reviewed',
          'criteria': [{'index': 0, 'status': 'passed', 'summary': 'Input handling matches requirement'}],
          'checks': [{'command': 'python -m unittest', 'status': 'passed', 'exit_code': 0,
                      'summary': 'Regression test passed'}], 'issues': []}


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        (self.root / 'calc.py').write_text('def add(a, b): return a + b\n', encoding='utf-8')
        self.record = {'id': 1, 'repository': str(self.root), 'goal': 'Reject invalid input without changing UI'}
        self.cancel = threading.Event()
        self.review = copy.deepcopy(REVIEW)
        self.cli = SimpleNamespace(text='', commands=[{'command': 'python -m unittest', 'exit_code': 0}],
                                   tools_used=False)
        self.client = Mock()
        self.client.send.side_effect = self.respond
        self.verifier = TaskVerifier(self.client)

    def respond(self, *args, **kwargs):
        self.cli.text = json.dumps(self.review)
        return self.cli

    def run_review(self, report=None):
        return self.verifier.verify(self.record, PLAN, report or REPORT, cancel_event=self.cancel,
                                    params={'model': 'gpt-6.1-terra', 'reasoning_effort': 'medium',
                                            'allow_publish': True, 'structured_report': True})

    def test_pass_requires_actual_command_and_stable_repository(self):
        result = self.run_review()
        self.assertEqual(result['status'], 'passed')
        self.assertFalse(result['correction_allowed'])
        self.assertTrue(result['checks'][0]['observed'])
        self.assertEqual(len(result['snapshot']), 64)
        args = self.client.send.call_args
        self.assertTrue(args.kwargs['verification'])
        self.assertNotIn('allow_publish', args.kwargs)
        self.assertNotIn('structured_report', args.kwargs)
        self.assertEqual(args.kwargs['reasoning_effort'], 'medium')
        self.assertIn(self.record['goal'], args.args[0])
        self.assertIn('acceptance_criteria', args.args[0])
        self.assertIn('read-only', args.args[0])

    def test_reported_success_cannot_override_failed_actual_exit_code(self):
        self.cli.commands[0]['exit_code'] = 1
        result = self.run_review()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['checks'][0]['observed'])
        self.assertEqual(result['checks'][0]['observed_exit_code'], 1)

    def test_executor_claims_and_echoed_command_are_not_execution_evidence(self):
        self.cli.commands = [{'command': 'echo "python -m unittest"', 'exit_code': 0}]
        self.assertEqual(self.run_review()['status'], 'partial')
        self.cli.commands = []
        self.assertEqual(self.run_review({**REPORT, 'checks': REVIEW['checks']})['status'], 'partial')

    def test_known_shell_wrapper_can_match_exact_check(self):
        self.cli.commands[0]['command'] = 'powershell.exe -NoProfile -Command "python -m unittest"'
        self.assertEqual(self.run_review()['status'], 'passed')

    def test_cli_escaped_powershell_script_matches_exactly_without_execution(self):
        script = "if ((Get-Content -Raw 'probe.txt') -ceq 'second') { exit 0 } else { exit 1 }"
        wrapped = shlex.join([r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
                              '-NoProfile', '-Command', script])
        self.review['checks'][0]['command'] = script
        self.cli.commands = [{'command': wrapped, 'exit_code': 0}]
        self.assertEqual(self.run_review()['status'], 'passed')
        self.cli.commands[0]['exit_code'] = 1
        self.assertEqual(self.run_review()['status'], 'failed')
        self.cli.commands = [{'command': shlex.join(['powershell.exe', '-Command', 'Write-Output ' + repr(script)]),
                              'exit_code': 0}]
        self.assertEqual(self.run_review()['status'], 'partial')

    def test_last_exit_code_for_repeated_command_is_used(self):
        self.cli.commands.append({'command': 'python -m unittest', 'exit_code': 1})
        self.assertEqual(self.run_review()['status'], 'failed')

    def test_no_checks_never_means_pass(self):
        self.review['checks'] = []
        self.assertEqual(self.run_review()['status'], 'partial')

    def test_skipped_or_manual_checks_are_incomplete(self):
        self.review['checks'][0].update(status='not_run', exit_code=None, summary='Missing dependencies')
        self.assertEqual(self.run_review()['status'], 'partial')
        self.review = copy.deepcopy(REVIEW)
        self.review['criteria'][0]['status'] = 'not_checked'
        self.assertEqual(self.run_review()['status'], 'partial')

    def test_failed_test_or_criterion_is_failure(self):
        self.review['checks'][0].update(status='failed', exit_code=1, summary='Regression failed')
        self.cli.commands[0]['exit_code'] = 1
        result = self.run_review()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['correction_allowed'])
        self.review = copy.deepcopy(REVIEW)
        self.cli.commands[0]['exit_code'] = 0
        self.review['criteria'][0]['status'] = 'failed'
        self.assertEqual(self.run_review()['status'], 'failed')

    def test_blocked_executor_or_remaining_work_cannot_be_verified_complete(self):
        blocked = self.run_review({**REPORT, 'status': 'blocked'})
        self.assertEqual(blocked['status'], 'failed')
        self.assertFalse(blocked['correction_allowed'])
        self.assertEqual(self.run_review({**REPORT, 'remaining': ['Missing button']})['status'], 'partial')

    def test_files_changed_during_check_are_not_verified(self):
        original = self.client.send.side_effect
        def modify(*args, **kwargs):
            (self.root / 'calc.py').write_text('changed while checking', encoding='utf-8')
            return original(*args, **kwargs)
        self.client.send.side_effect = modify
        result = self.run_review()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['snapshot'], '')

    def test_snapshot_failure_cannot_be_silently_ignored(self):
        with patch('verification.repository_snapshot', return_value=None):
            self.assertEqual(self.run_review()['status'], 'partial')

    def test_file_change_or_mcp_tool_rejects_review(self):
        self.cli.tools_used = True
        result = self.run_review()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['correction_allowed'])

    def test_unavailable_or_malformed_verifier_is_not_run_without_raw_output(self):
        for value in ('private secret raw output', json.dumps({**REVIEW, 'criteria': []})):
            self.client.send.side_effect = None
            self.client.send.return_value = SimpleNamespace(text=value)
            result = self.run_review()
            self.assertEqual(result['status'], 'not_run')
            self.assertNotIn('private secret', json.dumps(result))
        self.client.send.side_effect = GenerationFailed('private provider details')
        self.assertEqual(self.run_review()['status'], 'not_run')

    def test_long_real_command_can_be_confirmed_without_truncation(self):
        command = "@'\n" + '// regression check\n' * 110 + "\n'@ | node"
        self.review['checks'][0]['command'] = command
        self.cli.commands = [{'command': shlex.join(['powershell.exe', '-Command', command]), 'exit_code': 0}]
        result = self.run_review()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['checks'][0]['command'], command)
        self.assertTrue(result['checks'][0]['observed'])

    def test_rejected_review_records_safe_field_diagnostic(self):
        self.review['checks'][0]['command'] = 'PRIVATE-BODY-' + 'x' * 8000
        result = self.run_review()
        self.assertEqual(result['status'], 'not_run')
        self.assertFalse(result['correction_allowed'])
        self.assertIn('checks[0].command', result['issues'][0])
        self.assertNotIn('PRIVATE-BODY', repr(result))

    def test_cancellation_discards_late_review(self):
        def late(*args, **kwargs):
            self.cancel.set()
            return self.respond()
        self.client.send.side_effect = late
        with self.assertRaises(GenerationCancelled):
            self.run_review()

    def test_snapshot_tracks_untracked_dirty_deleted_files_and_index(self):
        first = repository_snapshot(str(self.root), self.cancel)
        self.assertIn('calc.py', first['files'])
        subprocess.run(['git', '-C', str(self.root), 'add', 'calc.py'], check=True, capture_output=True)
        indexed = repository_snapshot(str(self.root), self.cancel)
        self.assertNotEqual(first['git'], indexed['git'])
        (self.root / 'calc.py').write_text('dirty', encoding='utf-8')
        dirty = repository_snapshot(str(self.root), self.cancel)
        (self.root / 'calc.py').write_text('still dirty', encoding='utf-8')
        self.assertNotEqual(dirty, repository_snapshot(str(self.root), self.cancel))
        (self.root / 'calc.py').unlink()
        self.assertEqual(repository_snapshot(str(self.root), self.cancel)['files']['calc.py'], 'deleted')

    def test_snapshot_does_not_open_secret_or_ignored_files(self):
        (self.root / '.env').write_text('test fake private data', encoding='utf-8')
        (self.root / '.gitignore').write_text('ignored.txt\n', encoding='utf-8')
        (self.root / 'ignored.txt').write_text('build output', encoding='utf-8')
        original = Path.open
        def safe_open(path, *args, **kwargs):
            if path.name in ('.env', 'ignored.txt'):
                raise AssertionError('Private/ignored file must not be read')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', safe_open):
            result = repository_snapshot(str(self.root), self.cancel)
        self.assertIsNotNone(result)
        self.assertNotIn('.env', result['files'])
        self.assertNotIn('ignored.txt', result['files'])

    def test_snapshot_rejects_subdirectory_and_cancelled_scan(self):
        child = self.root / 'child'
        child.mkdir()
        self.assertIsNone(repository_snapshot(str(child), self.cancel))
        self.cancel.set()
        with self.assertRaises(GenerationCancelled):
            repository_snapshot(str(self.root), self.cancel)

    def test_protocol_requires_all_criteria_exactly_once_and_valid_exit_codes(self):
        cases = [dict(REVIEW, criteria=[]), dict(REVIEW, criteria=REVIEW['criteria'] * 2),
                 dict(REVIEW, criteria=[{**REVIEW['criteria'][0], 'index': True}]),
                 dict(REVIEW, criteria=[{**REVIEW['criteria'][0], 'index': 1}]),
                 dict(REVIEW, checks=[{**REVIEW['checks'][0], 'exit_code': 1}])]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(OrchestrationError):
                parse_verification(json.dumps(case), 1)


if __name__ == '__main__':
    unittest.main()
