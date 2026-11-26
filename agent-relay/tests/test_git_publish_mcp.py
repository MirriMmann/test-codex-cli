import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from git_publish_mcp import GitPublisher, PublishError, git_failure, handle_request, serve


class GitPublisherTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Relay Test')
        self.git('config', 'user.email', 'relay@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('remote', 'add', 'origin', 'https://github.com/example/project.git')
        self.publisher = GitPublisher(self.root)
        self.pushes = []

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout

    def write(self, name, text='hello'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def publish_offline(self, paths, message='Test publication'):
        original = self.publisher.git

        def run(*args, **kwargs):
            if args[0] == 'push':
                self.pushes.append(args)
                return b''
            return original(*args, **kwargs)

        with patch.object(self.publisher, 'git', side_effect=run):
            return self.publisher.publish(paths, message)

    def test_commits_only_selected_files_then_pushes_current_branch(self):
        self.write('index.html', '<h1>Calculator</h1>')
        self.write('other.txt')
        result = self.publish_offline(['index.html'], 'Add calculator')
        self.assertEqual(result['branch'], 'main')
        self.assertEqual(result['remote'], 'https://github.com/example/project.git')
        self.assertRegex(result['commit'], r'^[0-9a-f]{40,64}$')
        self.assertEqual(self.git('show', '--format=', '--name-only', 'HEAD').strip(), b'index.html')
        self.assertEqual(self.pushes, [('push', '--set-upstream', 'origin', result['commit'] + ':refs/heads/main')])
        self.assertIn(b'?? other.txt', self.git('status', '--porcelain'))

    def test_clean_checkout_can_publish_existing_commit_without_new_commit(self):
        self.write('index.html')
        first = self.publish_offline(['index.html'])
        second = self.publish_offline([], 'Publish existing commit')
        self.assertEqual(first['commit'], second['commit'])
        self.assertEqual(self.git('rev-list', '--count', 'HEAD').strip(), b'1')

    def test_refuses_unrequested_staged_changes(self):
        self.write('index.html')
        self.write('other.txt')
        self.git('add', 'other.txt')
        with self.assertRaisesRegex(PublishError, 'outside the requested'):
            self.publish_offline(['index.html'])
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.git('diff', '--cached', '--name-only').strip(), b'other.txt')

    def test_requested_directory_cannot_stage_credential_files(self):
        self.write('web/index.html')
        self.write('web/.env', 'secret')
        with self.assertRaisesRegex(PublishError, 'Credential files'):
            self.publish_offline(['web'])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), b'')
        self.assertEqual(self.pushes, [])

    def test_task_database_cannot_be_published_through_a_directory_request(self):
        self.write('local/index.html')
        self.write('local/tasks.sqlite3', 'private task text and modes')
        with self.assertRaises(PublishError):
            self.publish_offline(['local'])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), b'')
        self.assertEqual(self.pushes, [])

    def test_env_example_and_literal_git_pathspec_filename_are_allowed(self):
        self.write('.env.example', 'TOKEN=')
        # A leading colon is forbidden, but wildcard filenames must remain literal.
        self.write('file[1].txt')
        self.write('file1.txt')
        self.publish_offline(['.env.example', 'file[1].txt'])
        files = self.git('show', '--format=', '--name-only', 'HEAD').decode().splitlines()
        self.assertEqual(files, ['.env.example', 'file[1].txt'])

    def test_rejects_path_escape_metadata_and_secret_names(self):
        for value in ('../outside', '/outside', 'C:\\outside', '..\\outside', '.git/config',
                      'folder/.git/config', '.env', '.env.production', 'auth_data.json',
                      'id_ed25519', 'key.pem', '.ssh/config', ':file', 'file:stream',
                      '.agent-relay-state.tasks.sqlite3', 'tasks.sqlite3-wal'):
            with self.subTest(value=value), self.assertRaises(PublishError):
                self.publisher.safe_path(value)

    def test_rejects_unsafe_and_multiple_origin_push_urls(self):
        for remote in ('git@github.com:example/project.git', 'https://token@github.com/example/project.git',
                       'https://evil.example/example/project.git', 'https://github.com/example/project.git?token=secret'):
            self.git('remote', 'set-url', 'origin', remote)
            with self.subTest(remote=remote), self.assertRaisesRegex(PublishError, 'restricted'):
                self.publisher.destination()
        self.git('remote', 'set-url', 'origin', 'https://github.com/example/project.git')
        self.git('config', '--add', 'remote.origin.pushurl', 'https://github.com/example/project.git')
        self.git('config', '--add', 'remote.origin.pushurl', 'https://github.com/example/other.git')
        with self.assertRaisesRegex(PublishError, 'exactly one'):
            self.publisher.destination()

    def test_rejects_detached_head(self):
        self.write('index.html')
        self.publish_offline(['index.html'])
        self.git('checkout', '--detach')
        with self.assertRaisesRegex(PublishError, 'detached HEAD'):
            self.publisher.destination()

    def test_input_errors_do_not_run_git(self):
        for paths, message in (({}, 'test'), (['../bad'], 'test'), ([], ''), ([], 'bad\x00message'), ([False], 'test')):
            with self.subTest(paths=paths, message=message), patch.object(self.publisher, 'git') as run:
                with self.assertRaises(PublishError):
                    self.publisher.publish(paths, message)
                run.assert_not_called()

    def test_git_environment_does_not_override_repository(self):
        result = subprocess.CompletedProcess([], 0, stdout=b'', stderr=b'')
        with patch.dict(os.environ, {'GIT_DIR': 'outside', 'GIT_INDEX_FILE': 'outside-index'}), \
                patch('git_publish_mcp.subprocess.run', return_value=result) as run:
            self.publisher.git('status')
        kwargs = run.call_args.kwargs
        self.assertNotIn('GIT_DIR', kwargs['env'])
        self.assertNotIn('GIT_INDEX_FILE', kwargs['env'])
        self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
        self.assertNotIn('shell', kwargs)

    def test_timeout_and_auth_diagnostics_never_expose_raw_output(self):
        with patch('git_publish_mcp.subprocess.run', side_effect=subprocess.TimeoutExpired('git', 1)):
            with self.assertRaisesRegex(PublishError, 'timed out'):
                self.publisher.git('push')
        detail = git_failure(b'Authentication failed for https://super-secret@github.com/example/project')
        self.assertIn('authentication failed', detail.lower())
        self.assertNotIn('super-secret', detail)


class MCPProtocolTests(unittest.TestCase):
    def test_jsonl_initialization_notifications_tools_and_errors(self):
        publisher = Mock()
        publisher.publish.return_value = {'commit': 'a' * 40, 'branch': 'main', 'remote': 'https://github.com/o/r'}
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                'name': 'publish_changes', 'arguments': {'paths': ['index.html'], 'message': 'Add calculator'}}},
        ]
        output = io.StringIO()
        serve(publisher, io.StringIO('\n'.join(json.dumps(value) for value in requests) + '\n{invalid}\n'), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]['result']['protocolVersion'], '2025-03-26')
        self.assertEqual([tool['name'] for tool in responses[1]['result']['tools']], ['publish_changes'])
        self.assertEqual(json.loads(responses[2]['result']['content'][0]['text'])['branch'], 'main')
        self.assertEqual(responses[3]['error']['code'], -32700)
        publisher.publish.assert_called_once_with(['index.html'], 'Add calculator')

    def test_tool_errors_and_invalid_requests_are_safe(self):
        publisher = Mock()
        publisher.publish.side_effect = PublishError('GitHub authentication failed.')
        response = handle_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'publish_changes', 'arguments': {'paths': [], 'message': 'Publish'}}}, publisher)
        self.assertTrue(response['result']['isError'])
        self.assertIn('authentication failed', response['result']['content'][0]['text'])
        self.assertEqual(handle_request([], publisher)['error']['code'], -32600)
        self.assertEqual(handle_request({'jsonrpc': '2.0', 'id': 1, 'method': 'unknown'}, publisher)['error']['code'], -32601)
        response = handle_request({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'publish_changes', 'arguments': {'paths': [], 'message': 'Publish', 'force': True}}}, publisher)
        self.assertTrue(response['result']['isError'])


if __name__ == '__main__':
    unittest.main()
