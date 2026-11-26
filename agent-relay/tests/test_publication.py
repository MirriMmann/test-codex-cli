import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_relay import AgentRelay, Config
from generation import GenerationCancelled
from git_publish_mcp import GitPublisher, PublishError, PublishUncertain
from publication import baseline, digest, seal
from relay_fakes import DirectTaskRunner
from verification import repository_snapshot


REMOTE = 'https://github.com/example/calculator.git'


class ReviewedGitTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.repo, self.remote = self.root / 'repo', self.root / 'remote.git'
        self.run_git('init', '--bare', '-q', str(self.remote))
        self.run_git('init', '-q', '-b', 'main', str(self.repo))
        self.git('config', 'user.name', 'Publication Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        (self.repo / 'calc.py').write_text('old calculator\n', encoding='utf-8')
        self.git('add', 'calc.py')
        self.git('commit', '-qm', 'Base')
        self.git('remote', 'add', 'origin', REMOTE)
        self.git('push', str(self.remote), 'HEAD:refs/heads/main')
        self.original_head = self.git('rev-parse', 'HEAD').decode().strip()
        self.cancel = threading.Event()
        self.before = baseline(str(self.repo), self.cancel)
        self.publisher = GitPublisher(self.repo)
        self.report = {'changes': [{'path': 'calc.py', 'summary': 'Fix addition'}]}
        (self.repo / 'calc.py').write_text('fixed calculator\n', encoding='utf-8')
        self.plan = self.make_plan()
        self.assertTrue(self.plan['eligible'], self.plan)
        self.network = []
        original = self.publisher.git
        def local_only(*args, **kwargs):
            if 'ls-remote' in args or 'push' in args:
                self.network.append(args)
                args = tuple(str(self.remote) if arg == REMOTE else arg for arg in args)
            return original(*args, **kwargs)
        self.local_only = local_only

    def run_git(self, *args):
        result = subprocess.run(['git', *args], capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        return result.stdout

    def git(self, *args):
        return self.run_git('-C', str(self.repo), *args)

    def make_plan(self):
        snapshot = repository_snapshot(str(self.repo), self.cancel)
        return seal(str(self.repo), self.before, self.report, {'snapshot': digest(snapshot)}, self.cancel)

    def publish(self, plan=None, runner=None):
        with patch.object(self.publisher, 'git', side_effect=runner or self.local_only):
            return self.publisher.publish_reviewed(plan or self.plan, 'AgentRelay task #1')

    def remote_head(self):
        return self.run_git('--git-dir', str(self.remote), 'rev-parse', 'refs/heads/main').decode().strip()

    def publish_current(self):
        pin = {'root': str(self.repo), 'mode': 'current', 'branch': 'main',
               'remote': REMOTE, 'head': self.publisher.head()}
        with patch.object(self.publisher, 'git', side_effect=self.local_only):
            return self.publisher.publish_current(pin, 'Publish current project')

    def test_current_existing_change_publishes_without_executor_report(self):
        result = self.publish_current()
        self.assertEqual(result['commit'], self.remote_head())
        self.assertTrue(result['created_commit'])
        self.assertTrue(result['technical_checks_only'])
        self.assertEqual(self.git('show', 'HEAD:calc.py'), b'fixed calculator\n')

    def test_current_clean_unpublished_commit_is_pushed_without_extra_commit(self):
        self.git('add', 'calc.py')
        self.git('commit', '-qm', 'Already completed locally')
        expected = self.publisher.head()
        result = self.publish_current()
        self.assertEqual(result['commit'], expected)
        self.assertEqual(self.remote_head(), expected)
        self.assertFalse(result['created_commit'])

    def test_current_publication_does_not_treat_whitespace_lint_as_git_failure(self):
        (self.repo / 'calc.py').write_text('current version with trailing space  \n', encoding='utf-8')
        result = self.publish_current()
        self.assertEqual(result['commit'], self.remote_head())

    def test_current_already_published_state_succeeds_without_empty_commit_or_push(self):
        self.git('checkout', '--', 'calc.py')
        result = self.publish_current()
        self.assertTrue(result['already_published'])
        self.assertFalse(result['created_commit'])
        self.assertEqual(self.publisher.head(), self.original_head)
        self.assertFalse(any('push' in args for args in self.network))

    def test_current_credential_content_is_rejected_before_commit(self):
        (self.repo / 'notes.txt').write_text('ghp_' + 'a' * 30, encoding='utf-8')
        with self.assertRaisesRegex(PublishError, 'credentials'):
            self.publish_current()
        self.assertEqual(self.publisher.head(), self.original_head)
        self.assertEqual(self.remote_head(), self.original_head)

    def test_current_secrets_removed_in_later_unpublished_commit_still_block_push(self):
        (self.repo / 'calc.py').write_text('ghp_' + 'a' * 30, encoding='utf-8')
        self.git('add', 'calc.py')
        self.git('commit', '-qm', 'Secret accidentally committed')
        (self.repo / 'calc.py').write_text('clean content\n', encoding='utf-8')
        self.git('add', 'calc.py')
        self.git('commit', '-qm', 'Removed secret from current file')
        with self.assertRaisesRegex(PublishError, 'credentials'):
            self.publish_current()
        self.assertEqual(self.remote_head(), self.original_head)

    def test_current_remote_ahead_is_rejected_without_changing_branch(self):
        self.git('add', 'calc.py')
        self.git('commit', '-qm', 'Remote ahead')
        ahead = self.publisher.head()
        self.git('push', str(self.remote), 'HEAD:refs/heads/main')
        self.git('reset', '--soft', self.original_head)
        with self.assertRaisesRegex(PublishError, 'ahead or diverged'):
            self.publish_current()
        self.assertEqual(self.publisher.head(), self.original_head)
        self.assertEqual(self.remote_head(), ahead)

    def test_current_publication_preserves_other_remote_branch(self):
        self.git('push', str(self.remote), self.original_head + ':refs/heads/other')
        self.publish_current()
        other = self.run_git('--git-dir', str(self.remote), 'rev-parse', 'refs/heads/other').decode().strip()
        self.assertEqual(other, self.original_head)

    def test_real_local_push_is_confirmed_by_exact_remote_branch_and_commit(self):
        result = self.publish()
        self.assertTrue(result['verified'])
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['branch'], 'main')
        self.assertEqual(result['commit'], self.remote_head())
        self.assertEqual(result['commit'], self.git('rev-parse', 'HEAD').decode().strip())
        self.assertEqual(sum('push' in args for args in self.network), 1)
        self.assertEqual(sum('ls-remote' in args for args in self.network), 2)
        self.assertEqual(self.git('show', '--format=', '--name-only', 'HEAD').strip(), b'calc.py')

    def test_unrelated_untracked_file_is_not_committed(self):
        self.git('checkout', '--', 'calc.py')
        (self.repo / 'notes.txt').write_text('unrelated', encoding='utf-8')
        self.before = baseline(str(self.repo), self.cancel)
        (self.repo / 'calc.py').write_text('task fix', encoding='utf-8')
        self.plan = self.make_plan()
        self.assertTrue(self.plan['eligible'])
        self.publish()
        self.assertEqual(self.git('show', '--format=', '--name-only', 'HEAD').strip(), b'calc.py')
        self.assertIn(b'?? notes.txt', self.git('status', '--porcelain'))

    def test_preexisting_edits_are_not_silently_included(self):
        self.before = baseline(str(self.repo), self.cancel)
        (self.repo / 'calc.py').write_text('user edits plus a task edit', encoding='utf-8')
        self.assertFalse(self.make_plan()['eligible'])

    def test_report_must_match_all_actual_changes(self):
        (self.repo / 'extra.py').write_text('unreported', encoding='utf-8')
        self.assertFalse(self.make_plan()['eligible'])
        (self.repo / 'extra.py').unlink()
        self.report['changes'].append({'path': 'missing.py', 'summary': 'invented'})
        self.assertFalse(self.make_plan()['eligible'])

    def test_stale_files_or_unrelated_staging_block_before_commit(self):
        (self.repo / 'calc.py').write_text('changed after verification', encoding='utf-8')
        with self.assertRaises(PublishError):
            self.publish()
        self.assertEqual(self.git('rev-parse', 'HEAD').decode().strip(), self.original_head)
        self.assertEqual(self.remote_head(), self.original_head)

    def test_wrong_branch_origin_or_checkout_are_rejected(self):
        for field, value in (('branch', 'other'), ('remote', 'https://github.com/other/repo.git'),
                             ('root', str(self.root)), ('head', '0' * 40)):
            with self.subTest(field=field), self.assertRaises(PublishError):
                self.publish({**self.plan, field: value})
        self.assertEqual(self.network, [])

    def test_remote_advance_is_rejected_without_local_commit(self):
        clone = self.root / 'other'
        self.run_git('clone', '-q', '-b', 'main', str(self.remote), str(clone))
        self.run_git('-C', str(clone), 'config', 'user.name', 'Other')
        self.run_git('-C', str(clone), 'config', 'user.email', 'other@example.invalid')
        (clone / 'other.txt').write_text('remote advanced', encoding='utf-8')
        self.run_git('-C', str(clone), 'add', 'other.txt')
        self.run_git('-C', str(clone), '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Other change')
        self.run_git('-C', str(clone), 'push', 'origin', 'HEAD:refs/heads/main')
        with self.assertRaisesRegex(PublishError, 'remote branch differs'):
            self.publish()
        self.assertEqual(self.git('rev-parse', 'HEAD').decode().strip(), self.original_head)

    def test_unpublished_ancestor_commits_are_not_swept_into_push(self):
        self.git('add', 'calc.py')
        self.git('commit', '-qm', 'Unpublished earlier work')
        self.before = baseline(str(self.repo), self.cancel)
        (self.repo / 'calc.py').write_text('new task', encoding='utf-8')
        self.plan = self.make_plan()
        with self.assertRaisesRegex(PublishError, 'remote branch differs'):
            self.publish()
        self.assertEqual(self.remote_head(), self.original_head)

    def test_push_rejection_never_returns_success_or_retries(self):
        pushes = []
        def reject(*args, **kwargs):
            if 'push' in args:
                pushes.append(args)
                raise PublishError('Push was rejected: non-fast-forward')
            return self.local_only(*args, **kwargs)
        with self.assertRaisesRegex(PublishUncertain, 'not confirmed'):
            self.publish(runner=reject)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(self.remote_head(), self.original_head)
        self.assertNotEqual(self.git('rev-parse', 'HEAD').decode().strip(), self.original_head)

    def test_successful_push_with_failed_remote_confirmation_is_unknown(self):
        reads = []
        def fail_confirmation(*args, **kwargs):
            if 'ls-remote' in args:
                reads.append(args)
                if len(reads) == 2:
                    raise PublishError('Network unavailable during confirmation')
            return self.local_only(*args, **kwargs)
        with self.assertRaises(PublishUncertain):
            self.publish(runner=fail_confirmation)
        self.assertEqual(self.remote_head(), self.git('rev-parse', 'HEAD').decode().strip())

    def test_post_commit_source_mutation_blocks_push(self):
        def change(*args, **kwargs):
            result = self.local_only(*args, **kwargs)
            if args[0] == 'commit':
                (self.repo / 'calc.py').write_text('changed by a hook', encoding='utf-8')
            return result
        with self.assertRaisesRegex(PublishError, 'push was blocked'):
            self.publish(runner=change)
        self.assertEqual(self.remote_head(), self.original_head)

    def test_tracked_deletion_is_published_and_confirmed(self):
        (self.repo / 'calc.py').unlink()
        self.plan = self.make_plan()
        self.assertTrue(self.plan['eligible'])
        result = self.publish()
        self.assertTrue(result['verified'])
        self.assertEqual(self.git('ls-tree', '--name-only', 'HEAD'), b'')

    def test_changed_destination_during_task_cannot_be_sealed(self):
        self.git('remote', 'set-url', 'origin', 'https://github.com/example/changed.git')
        self.assertFalse(self.make_plan()['eligible'])

    def test_staged_blob_tampering_is_detected_before_commit(self):
        blob = subprocess.run(['git', '-C', str(self.repo), 'hash-object', '-w', '--stdin'],
                              input=b'unreviewed content\n', capture_output=True, check=True).stdout.decode().strip()
        def tamper(*args, **kwargs):
            if args[0] == 'write-tree':
                self.git('update-index', '--cacheinfo', '100644', blob, 'calc.py')
            return self.local_only(*args, **kwargs)
        with self.assertRaisesRegex(PublishError, 'staged content differs'):
            self.publish(runner=tamper)
        self.assertEqual(self.remote_head(), self.original_head)

    def test_custom_clean_filter_is_rejected_before_staging(self):
        (self.repo / '.gitattributes').write_text('calc.py filter=custom\n', encoding='utf-8')
        self.git('config', 'filter.custom.clean', 'cat')
        self.report['changes'].append({'path': '.gitattributes', 'summary': 'Attributes'})
        self.plan = self.make_plan()
        self.assertTrue(self.plan['eligible'])
        with self.assertRaisesRegex(PublishError, 'Custom Git filters'):
            self.publish()
        self.assertEqual(self.git('diff', '--cached', '--name-only'), b'')

    def test_publication_does_not_execute_repository_hooks_on_host(self):
        hook = self.repo / '.git' / 'hooks' / 'pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        hook.chmod(0o755)
        self.assertTrue(self.publish()['verified'])

    def test_new_empty_repository_can_publish_its_first_verified_commit(self):
        self.repo = self.root / 'empty'
        self.run_git('init', '-q', '-b', 'newbranch', str(self.repo))
        self.git('config', 'user.name', 'Publication Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('remote', 'add', 'origin', REMOTE)
        self.before = baseline(str(self.repo), self.cancel)
        (self.repo / 'calc.py').write_text('first verified version', encoding='utf-8')
        self.plan = self.make_plan()
        self.assertTrue(self.plan['eligible'])
        self.publisher = GitPublisher(self.repo)
        original = self.publisher.git
        def offline(*args, **kwargs):
            args = tuple(str(self.remote) if arg == REMOTE else arg for arg in args)
            return original(*args, **kwargs)
        result = self.publish(runner=offline)
        remote = self.run_git('--git-dir', str(self.remote), 'rev-parse', 'refs/heads/newbranch').decode().strip()
        self.assertEqual(result['commit'], remote)


class PublicationControlTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.api, self.codex, self.publisher = Mock(), Mock(), Mock()
        self.api.call.return_value = {'id': 5, 'username': 'MyBot'}
        self.codex.send.return_value = SimpleNamespace(text='coding answer')
        self.publisher.send.return_value = SimpleNamespace(publication={
            'status': 'published', 'verified': True, 'branch': 'main', 'remote': REMOTE, 'commit': 'a' * 40})
        self.config = Config('fake', -10, str(self.root), 'unused.json', frozenset({'peerbot'}),
                             frozenset({20, 21}), frozenset({30}), min_interval_seconds=0,
                             state_file=str(self.root / 'state.json'), codex_aliases=('Сударь',),
                             codex_allow_push=True)
        self.app = AgentRelay(self.config, self.api, self.codex, orchestrator=DirectTaskRunner(self.codex), publisher=self.publisher)
        pin = patch('agent_relay.GitPublisher')
        self.git_pin = pin.start().return_value
        self.addCleanup(pin.stop)
        self.git_pin.destination.return_value = ('main', REMOTE)
        self.git_pin.head.return_value = 'b' * 40
        self.task = self.make_task()
        self.update = 100

    def make_task(self, update=1):
        store = self.app.tasks
        identifier = store.create(update_id=update, chat_id=-10, topic_id=7, owner_id=20,
                                  owner_is_bot=False, message_id=update, repository=str(self.root), goal='Calculator')
        store.set_status(identifier, 'running', count_attempt=False)
        store.begin_execution(identifier)
        store.save_report(identifier, {'changes': [], 'checks': []})
        store.set_status(identifier, 'checking')
        store.save_verification(identifier, {'status': 'passed'})
        store.save_manifest(identifier, {'eligible': True, 'root': str(self.root), 'branch': 'main',
                                         'remote': REMOTE, 'head': 'b' * 40, 'paths': ['calc.py'],
                                         'snapshot': 's', 'sources': 'f'})
        store.set_status(identifier, 'completed')
        store.save_final_report(identifier, 'Calculator changes and check results')
        return identifier

    def send(self, text, *, user=20, topic=7, bot=False):
        self.update += 1
        self.app.process_update({'update_id': self.update, 'message': {
            'message_id': self.update, 'chat': {'id': -10, 'type': 'supergroup'}, 'message_thread_id': topic,
            'from': {'id': user, 'is_bot': bot, 'username': 'peerbot' if bot else 'Human'}, 'text': text}})

    def texts(self):
        return [call.args[1]['text'] for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']

    def test_only_explicit_owner_command_publishes_and_confirms_branch_commit(self):
        self.send('/publish 1 main')
        self.publisher.send.assert_called_once()
        self.codex.send.assert_not_called()
        row = self.app.tasks.get(1)
        self.assertEqual(row['publication_status'], 'published')
        self.assertEqual(row['status'], 'completed')
        self.assertIn('Ветка: main', self.texts()[-1])
        self.assertIn('a' * 40, self.texts()[-1])
        self.send('/status 1')
        self.assertIn('a' * 40, self.texts()[-1])
        self.send('/publish 1 main')
        self.publisher.send.assert_called_once()

    def test_other_people_topics_and_peer_bots_cannot_authorize_publication(self):
        self.send('/publish 1 main', user=21)
        self.send('/publish 1 main', topic=8)
        self.send('/publish 1 main', user=30, bot=True)
        self.publisher.send.assert_not_called()

    def test_natural_language_quotes_and_commands_to_another_bot_do_not_grant_permission(self):
        self.send('Сударь, цитата: /publish 1 main')
        self.send('/publish@AnotherBot 1 main')
        self.publisher.send.assert_not_called()
        self.assertNotIn('allow_publish', self.codex.send.call_args.kwargs)

    def test_natural_language_publishes_existing_task_without_development(self):
        self.send('Сударь, змейка готова, push не был сделан, сделай сейчас')
        self.publisher.send.assert_called_once()
        self.codex.send.assert_not_called()
        self.assertEqual(self.app.tasks.get(1)['publication_status'], 'published')

    def test_exact_reported_push_request_never_becomes_a_codex_language_task(self):
        self.send('Сударь, попробуй сейчас сделать push')
        self.publisher.send.assert_called_once()
        self.codex.send.assert_not_called()

    def test_exact_request_without_new_changes_uses_current_git_state(self):
        self.send('Сударь опубликуй текущий файл в репозиторий, не смотря на отсутствие изменений')
        self.publisher.send.assert_called_once()
        self.codex.send.assert_not_called()

    def test_natural_publication_rejects_other_branch_but_does_not_require_model_report(self):
        self.send('Сударь, сделай push в ветку other')
        self.publisher.send.assert_not_called()
        with self.app.tasks.connection() as db:
            db.execute("UPDATE tasks SET verification='partial' WHERE id=1")
        self.send('Сударь, сделай push')
        self.publisher.send.assert_called_once()
        self.assertEqual(self.publisher.send.call_args.kwargs['plan']['mode'], 'current')
        self.assertEqual(self.app.tasks.get(1)['verification'], 'partial')

    def test_natural_publication_cannot_use_other_owner_or_topic(self):
        self.send('Сударь, сделай push задачи #1', user=21)
        self.send('Сударь, сделай push задачи #1', topic=8)
        self.publisher.send.assert_not_called()

    def test_completed_task_with_human_publication_request_publishes_once(self):
        from agent_relay import GenerationTask
        with self.app.tasks.connection() as db:
            db.execute("UPDATE tasks SET status='checking',goal=? WHERE id=1",
                       ('Добавь калькулятор и сделай push',))
        self.codex.send.return_value = SimpleNamespace(text='Result checked', work_status='completed')
        message = {'message_id': 90, 'chat': {'id': -10, 'type': 'supergroup'},
                   'message_thread_id': 7, 'from': {'id': 20}, 'text': 'Добавь калькулятор и сделай push'}
        task = GenerationTask(90, message, '', {}, work_id=1)
        self.app._finish_generation(task)
        self.app._finish_generation(task)
        self.publisher.send.assert_called_once()
        self.assertEqual(self.app.tasks.get(1)['publication_status'], 'published')

    def test_publication_requested_during_work_is_persisted(self):
        with self.app.tasks.connection() as db:
            db.execute("UPDATE tasks SET status='running' WHERE id=1")
        self.send('Сударь, сделай push')
        self.publisher.send.assert_not_called()
        authorization = json.loads(self.app.tasks.get(1)['publish_request_json'])
        self.assertEqual(authorization['update_id'], self.update)
        self.assertIn('сделай push', authorization['text'])

    def test_publication_precedes_next_queued_development(self):
        self.app._executor = Mock()
        next_coding = SimpleNamespace(work_id=99)
        self.app._pending.append(next_coding)
        self.send('Сударь, сделай push')
        self.assertIsNotNone(self.app._pending[0].publication_id)
        self.assertIs(self.app._pending[-1], next_coding)

    def test_failed_task_without_saved_evidence_can_publish_current_state_without_redevelopment(self):
        with self.app.tasks.connection() as db:
            db.execute("UPDATE tasks SET status='failed',verification='not_run',report_json='' WHERE id=1")
        self.send('Сударь, сделай push')
        self.publisher.send.assert_called_once()
        self.codex.send.assert_not_called()
        self.assertEqual(self.app.tasks.get(1)['status'], 'failed')

    def test_publication_without_previous_task_creates_only_host_request(self):
        self.send('Сударь, опубликуй текущий файл в репозиторий', topic=8)
        self.publisher.send.assert_called_once()
        self.codex.send.assert_not_called()
        row = self.app.tasks.owned(-10, 8, 20)[0]
        self.assertEqual((row['attempts'], row['verification'], row['publication_status']), (0, 'not_run', 'published'))

    def test_wrong_branch_or_unverified_task_is_rejected(self):
        self.send('/publish 1 other')
        with self.app.tasks.connection() as db:
            db.execute("UPDATE tasks SET verification='partial' WHERE id=1")
        self.send('/publish 1 main')
        self.publisher.send.assert_not_called()

    def test_disabled_publication_or_changed_repository_is_rejected(self):
        self.app.config = replace(self.config, codex_allow_push=False)
        self.send('/publish 1 main')
        self.app.config = replace(self.config, codex_workdir=str(self.root / 'other'))
        self.send('/publish 1 main')
        self.publisher.send.assert_not_called()

    def test_publication_failure_or_unverified_claim_never_announces_success(self):
        self.publisher.send.return_value = SimpleNamespace(publication={'status': 'unknown', 'error': 'Confirmation unavailable'})
        self.send('/publish 1 main')
        self.assertEqual(self.app.tasks.get(1)['publication_status'], 'unknown')
        self.assertIn('не подтверждена', self.texts()[-1])
        second = self.make_task(2)
        self.publisher.send.return_value = SimpleNamespace(publication={'status': 'published', 'verified': False})
        self.send(f'/publish {second} main')
        self.assertEqual(self.app.tasks.get(second)['publication_status'], 'unknown')
        self.assertNotIn('Публикация подтверждена', self.texts()[-1])

    def test_report_is_saved_and_retrieved_without_another_model_request(self):
        self.send('/report 1')
        self.assertIn('Calculator changes and check results', self.texts()[-1])
        self.codex.send.assert_not_called()
        self.publisher.send.assert_not_called()

    def test_queued_publication_can_be_cancelled_without_git(self):
        self.app._executor = Mock()
        self.send('/publish 1 main')
        self.send('/cancel 1')
        self.assertEqual(self.app.tasks.get(1)['publication_status'], 'cancelled')
        self.app._pump_generation()
        self.publisher.send.assert_not_called()

    def test_running_publication_does_not_block_status_and_emergency_stop(self):
        entered = threading.Event()
        def publish(*args, cancel_event, **kwargs):
            entered.set()
            if not cancel_event.wait(3):
                raise AssertionError('Publication not cancelled')
            raise GenerationCancelled()
        self.publisher.send.side_effect = publish
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.app._executor = pool
            self.send('/publish 1 main')
            self.app._pump_generation()
            self.assertTrue(entered.wait(2))
            self.send('/status 1')
            self.assertIn('Публикация: выполняется', self.texts()[-1])
            self.send('/stop')
            self.app._active.future.exception(timeout=2)
            self.app._pump_generation()
        self.assertEqual(self.app.tasks.get(1)['publication_status'], 'unknown')
        self.assertEqual(self.app.tasks.get(1)['status'], 'completed')
        self.publisher.send.assert_called_once()

    def test_restart_marks_pending_publication_cancelled_and_running_unknown(self):
        self.app._executor = Mock()
        self.send('/publish 1 main')
        second = self.make_task(2)
        self.send(f'/publish {second} main')
        pub = next(task for task in self.app._pending if task.work_id == second)
        self.app.tasks.start_publication(pub.publication_id)
        self.app.tasks.recover()
        self.assertEqual(self.app.tasks.get(1)['publication_status'], 'cancelled')
        self.assertEqual(self.app.tasks.get(second)['publication_status'], 'unknown')
        self.publisher.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
