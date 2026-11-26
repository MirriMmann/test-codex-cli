import json
import tempfile
import unittest
from pathlib import Path

from task_store import TaskStore, task_status


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / 'tasks.sqlite3')
        self.store = TaskStore(self.path)

    def create(self, update=1):
        return self.store.create(update_id=update, chat_id=-10, topic_id=7, owner_id=20,
                                 owner_is_bot=False, message_id=update, repository='local-repo',
                                 goal='private request', constraints=('no new dependencies',),
                                 criteria=('existing tests pass',))

    def test_records_and_personal_modes_survive_restart(self):
        first = self.create()
        self.store.set_mode(-10, 7, 20, 'codex')
        restored = TaskStore(self.path)
        record = restored.get(first)
        self.assertEqual(record['goal'], 'private request')
        self.assertIn('no new dependencies', record['constraints_json'])
        self.assertIn('existing tests pass', record['criteria_json'])
        self.assertEqual(restored.mode(-10, 7, 20, default='chat'), 'codex')
        self.assertEqual(restored.mode(-10, 8, 20, default='chat'), 'chat')
        self.assertEqual(restored.mode(-10, 7, 21, default='chat'), 'chat')
        self.assertEqual(restored.mode(-11, 7, 20, default='chat'), 'chat')
        self.assertGreater(restored.create(update_id=2, chat_id=-10, topic_id=7, owner_id=20,
                                           owner_is_bot=False, message_id=2, repository='r', goal='next'), first)

    def test_duplicate_update_never_creates_a_second_job(self):
        self.assertEqual(self.create(), 1)
        self.assertIsNone(self.create())
        self.assertEqual(len(self.store.owned(-10, 7, 20)), 1)

    def test_recovery_interrupts_unfinished_work_without_replay(self):
        queued, running, checking, completed = [self.create(n) for n in range(1, 5)]
        self.store.set_status(running, 'running')
        self.store.set_status(checking, 'running')
        self.store.set_status(checking, 'checking')
        self.store.set_status(completed, 'running')
        self.store.set_status(completed, 'completed')
        self.store.recover()
        self.assertTrue(all(self.store.get(n)['status'] == 'interrupted' for n in (queued, running, checking)))
        self.assertEqual(self.store.get(completed)['status'], 'completed')
        self.assertEqual(self.store.owned(-10, 7, 20, active=True), [])

    def test_cancelled_job_cannot_be_completed_by_late_worker(self):
        job = self.create()
        self.assertTrue(self.store.set_status(job, 'running'))
        self.assertEqual(self.store.request_cancel(job, -10, 7, 20), 'running')
        self.assertFalse(self.store.set_status(job, 'completed'))
        self.assertFalse(self.store.set_status(job, 'failed'))
        self.assertTrue(self.store.set_status(job, 'cancelled'))
        self.assertFalse(self.store.set_status(job, 'running'))

    def test_cancel_is_scoped_to_owner_group_and_topic(self):
        job = self.create()
        for scope in ((-11, 7, 20), (-10, 8, 20), (-10, 7, 21)):
            self.assertIsNone(self.store.request_cancel(job, *scope))
        self.assertEqual(self.store.get(job)['status'], 'queued')
        self.assertEqual(self.store.request_cancel(job, -10, 7, 20), 'cancelled')

    def test_completed_is_not_a_claim_that_checks_passed(self):
        job = self.create()
        self.store.set_status(job, 'running')
        self.store.set_status(job, 'completed')
        record = self.store.get(job)
        self.assertEqual(record['attempts'], 1)
        self.assertEqual(record['max_corrections'], 2)
        self.assertEqual(record['verification'], 'not_run')
        self.assertIn('не подтверждены', task_status(record))
        self.assertNotIn('private request', task_status(record))
        self.assertNotIn('local-repo', task_status(record))

    def test_corrupt_database_is_not_silently_discarded(self):
        path = Path(self.folder.name) / 'broken.sqlite3'
        path.write_text('not a database')
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            TaskStore(str(path))
        self.assertEqual(path.read_text(), 'not a database')

    def test_v1_database_migrates_without_losing_tasks_modes_or_sequence(self):
        first = self.create()
        self.store.set_mode(-10, 7, 20, 'codex')
        with self.store.connection() as db:
            for column in ('stage', 'plan_json', 'report_json', 'verification_json'):
                db.execute(f'ALTER TABLE tasks DROP COLUMN {column}')
            db.execute('PRAGMA user_version=1')
        restored = TaskStore(self.path)
        row = restored.get(first)
        self.assertEqual(row['goal'], 'private request')
        self.assertEqual(row['stage'], '')
        self.assertEqual(row['report_json'], '')
        self.assertEqual(restored.mode(-10, 7, 20, default='chat'), 'codex')
        self.assertGreater(self.create(2), first)
        with restored.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)

    def test_cancelled_task_cannot_receive_plan_or_report_from_old_worker(self):
        task = self.create()
        self.store.set_status(task, 'running', count_attempt=False)
        self.store.request_cancel(task, -10, 7, 20)
        self.assertFalse(self.store.save_plan(task, {'constraints': [], 'acceptance_criteria': ['done']}))
        self.assertFalse(self.store.save_report(task, {'changes': [], 'checks': []}))
        self.assertFalse(self.store.begin_execution(task))
        self.assertEqual(self.store.get(task)['attempts'], 0)

    def test_v2_database_migrates_and_keeps_executor_report(self):
        task = self.create()
        self.store.set_status(task, 'running')
        report = {'changes': [], 'checks': []}
        self.store.save_report(task, report)
        with self.store.connection() as db:
            db.execute('ALTER TABLE tasks DROP COLUMN verification_json')
            db.execute('PRAGMA user_version=2')
        restored = TaskStore(self.path)
        self.assertEqual(json.loads(restored.get(task)['report_json']), report)
        self.assertEqual(restored.get(task)['verification_json'], '')

    def test_verification_saved_separately_without_incrementing_executor_attempts(self):
        task = self.create()
        self.store.set_status(task, 'running', count_attempt=False)
        self.store.begin_execution(task)
        result = {'status': 'partial', 'summary': 'Manual test missing', 'checks': []}
        self.assertFalse(self.store.save_verification(task, result))
        self.store.set_status(task, 'checking')
        self.assertTrue(self.store.set_stage(task, 'verifying'))
        self.assertTrue(self.store.save_verification(task, result))
        restored = TaskStore(self.path)
        row = restored.get(task)
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(json.loads(row['verification_json']), result)
        self.assertIn('Проверка неполная', task_status(row))
        self.store.request_cancel(task, -10, 7, 20)
        self.assertFalse(self.store.save_verification(task, {**result, 'status': 'passed'}))
        self.assertFalse(self.store.set_status(task, 'completed'))
        self.assertEqual(self.store.get(task)['verification'], 'partial')

    def test_plan_and_executor_report_survive_restart_with_provenance(self):
        task = self.create()
        self.store.set_status(task, 'running', count_attempt=False)
        plan = {'goal': 'planned task', 'constraints': ['minimal edit'], 'acceptance_criteria': ['test passes']}
        report = {'changes': [{'path': 'index.html', 'summary': 'fixed'}], 'checks': []}
        self.assertTrue(self.store.save_plan(task, plan))
        self.assertTrue(self.store.begin_execution(task))
        self.assertTrue(self.store.save_report(task, report))
        restored = TaskStore(self.path)
        row = restored.get(task)
        self.assertEqual(json.loads(row['plan_json']), plan)
        self.assertEqual(json.loads(row['report_json']), report)
        self.assertEqual(row['goal'], 'private request')
        self.assertEqual(row['verification'], 'unverified')
        self.assertEqual(row['attempts'], 1)

    def test_v3_migration_preserves_last_report_and_modes_as_history(self):
        task = self.create()
        self.store.set_mode(-10, 7, 20, 'chat')
        self.store.set_status(task, 'running', count_attempt=False)
        self.store.begin_execution(task)
        report = {'changes': [], 'checks': []}
        review = {'status': 'failed', 'summary': 'A regression failed'}
        self.store.save_report(task, report)
        self.store.set_status(task, 'checking')
        self.store.save_verification(task, review)
        with self.store.connection() as db:
            db.execute('DROP TABLE task_attempts')
            db.execute('ALTER TABLE tasks DROP COLUMN stop_reason')
            db.execute('PRAGMA user_version=3')
        restored = TaskStore(self.path)
        self.assertEqual(restored.mode(-10, 7, 20, default='codex'), 'chat')
        self.assertEqual(restored.get(task)['attempts'], 1)
        history = restored.attempt_history(task)
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0]['report_json']), report)
        self.assertEqual(json.loads(history[0]['verification_json']), review)

    def test_store_refuses_fourth_execution_even_if_limit_is_increased(self):
        task = self.create()
        self.store.set_status(task, 'running', count_attempt=False)
        with self.store.connection() as db:
            db.execute('UPDATE tasks SET max_corrections=999 WHERE id=?', (task,))
        for number in range(1, 4):
            self.assertTrue(self.store.begin_execution(task))
            self.assertFalse(self.store.begin_execution(task))  # no duplicate dispatch while running
            row = self.store.get(task)
            self.assertEqual(row['attempts'], number)
            self.assertEqual(row['verification'], 'not_run')
            self.store.save_report(task, {'changes': [], 'checks': []})
            self.store.set_status(task, 'checking')
            self.store.save_verification(task, {'status': 'failed'})
        self.assertFalse(self.store.begin_execution(task))
        self.assertEqual(len(self.store.attempt_history(task)), 3)

    def test_cancellation_fences_new_attempt_and_history_writes(self):
        task = self.create()
        self.store.set_status(task, 'running', count_attempt=False)
        self.store.begin_execution(task)
        self.store.save_report(task, {'changes': [], 'checks': []})
        self.store.set_status(task, 'checking')
        self.store.save_verification(task, {'status': 'failed'})
        self.store.request_cancel(task, -10, 7, 20)
        self.assertFalse(self.store.begin_execution(task))
        self.assertFalse(self.store.finish_cycle(task, 'corrections_exhausted'))
        self.assertFalse(self.store.save_verification(task, {'status': 'passed'}))
        history = self.store.attempt_history(task)
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0]['verification_json'])['status'], 'failed')

    def test_v4_migration_keeps_reports_but_does_not_grant_publication_to_old_tasks(self):
        task = self.create()
        self.store.set_status(task, 'running', count_attempt=False)
        self.store.begin_execution(task)
        self.store.save_report(task, {'changes': [], 'checks': []})
        self.store.set_status(task, 'checking')
        self.store.save_verification(task, {'status': 'passed'})
        self.store.set_status(task, 'completed')
        with self.store.connection() as db:
            db.execute('DROP TABLE publications')
            for column in ('baseline_json', 'manifest_json', 'final_report', 'publication_status', 'publication_json'):
                db.execute(f'ALTER TABLE tasks DROP COLUMN {column}')
            db.execute('PRAGMA user_version=4')
        restored = TaskStore(self.path)
        self.assertEqual(len(restored.attempt_history(task)), 1)
        self.assertEqual(restored.get(task)['verification'], 'passed')
        with self.assertRaises(ValueError):
            restored.create_publication(task, (-10, 7, 20), 100, 'main')


if __name__ == '__main__':
    unittest.main()
