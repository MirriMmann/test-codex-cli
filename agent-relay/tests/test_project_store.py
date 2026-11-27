import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from privacy import PrivacyGuard, PrivacyViolation
from project_store import BudgetExceeded, DEFAULT_LIMITS, ProjectError, ProjectStore


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = str(Path(self.folder.name) / 'projects.sqlite3')
        self.now = 100.0
        self.scope = (-123, 7, 10)
        self.store = ProjectStore(self.path, clock=lambda: self.now)
        self.pid = self.store.create(self.scope, self.folder.name, 'Build a tab adapter', request_key='create:1')

    def documents(self):
        self.store.save_document(self.pid, self.scope, 'spec', 'Import and export GP7', expected_version=0)
        self.store.save_document(self.pid, self.scope, 'plan', 'Implement adapter; test round trip', expected_version=0)

    def round(self):
        self.documents()
        return self.store.begin_round(self.pid, self.scope, request_key='round1')

    def test_create_replay_and_restart_preserve_one_project_and_event(self):
        other = ProjectStore(self.path, clock=lambda: self.now)
        self.assertEqual(other.create(self.scope, self.folder.name, 'Build a tab adapter', request_key='create:1'), self.pid)
        self.assertEqual(other.latest(self.scope), self.pid)
        self.assertEqual(len(other.events(self.pid, self.scope)), 1)
        with self.assertRaises(ProjectError):
            other.create(self.scope, self.folder.name, 'Different', request_key='create:1')

    def test_access_requires_membership_group_and_topic(self):
        for scope in ((-999, 7, 10), (-123, 8, 10), (-123, 7, 11)):
            with self.assertRaises(ProjectError):
                self.store.get(self.pid, scope)
            self.assertIsNone(self.store.latest(scope))
        self.store.add_member(self.pid, self.scope, 11)
        self.assertEqual(self.store.latest((-123, 7, 11)), self.pid)
        with self.assertRaises(ProjectError):
            self.store.add_member(self.pid, (-123, 7, 11), 12)

    def test_document_versions_hashes_conflicts_and_stale_plan(self):
        self.documents()
        spec = self.store.document(self.pid, self.scope, 'spec')
        self.assertEqual(spec['sha256'], hashlib.sha256(spec['content'].encode()).hexdigest())
        with self.assertRaises(ProjectError):
            self.store.save_document(self.pid, self.scope, 'spec', 'Concurrent change', expected_version=0)
        self.assertEqual(self.store.save_document(self.pid, self.scope, 'spec', spec['content'], expected_version=1), 1)
        self.store.save_document(self.pid, self.scope, 'spec', 'New requirements', expected_version=1)
        with self.assertRaises(ProjectError):
            self.store.begin_round(self.pid, self.scope, request_key='stale-plan')
        self.assertEqual(self.store.document(self.pid, self.scope, 'spec', 1), spec)
        previous_plan = self.store.document(self.pid, self.scope, 'plan')
        self.assertEqual(self.store.save_document(self.pid, self.scope, 'plan',
            previous_plan['content'], expected_version=1), 2)
        self.assertEqual(self.store.document(self.pid, self.scope, 'plan')['spec_version'], 2)

    def test_plan_requires_spec(self):
        with self.assertRaises(ProjectError):
            self.store.save_document(self.pid, self.scope, 'plan', 'Do something', expected_version=0)

    def test_round_snapshot_replay_and_document_freeze(self):
        rid = self.round()
        self.assertEqual(self.store.begin_round(self.pid, self.scope, request_key='round1'), rid)
        project = self.store.get(self.pid, self.scope)
        self.assertEqual(project['usage']['rounds'], 1)
        self.assertEqual(project['rounds'][0]['plan_sha256'], self.store.document(self.pid, self.scope, 'plan')['sha256'])
        with self.assertRaises(ProjectError):
            self.store.begin_round(self.pid, self.scope, request_key='duplicate-open')
        with self.assertRaises(ProjectError):
            self.store.save_document(self.pid, self.scope, 'spec', 'Extra feature', expected_version=1)

    def test_round_state_machine_and_completion(self):
        rid = self.round()
        with self.assertRaises(ProjectError):
            self.store.advance_round(self.pid, self.scope, rid, expected_state='planning', state='completed')
        for before, after in zip(('planning', 'agreed', 'executing', 'reviewing', 'integrating'),
                                 ('agreed', 'executing', 'reviewing', 'integrating', 'completed')):
            self.store.advance_round(self.pid, self.scope, rid, expected_state=before, state=after)
        self.assertEqual(self.store.get(self.pid, self.scope)['state'], 'ready')
        self.store.complete(self.pid, self.scope)
        self.assertEqual(self.store.get(self.pid, self.scope)['state'], 'completed')
        with self.assertRaises(ProjectError):
            self.store.control(self.pid, self.scope, 'resume')

    def test_cannot_complete_without_completed_round(self):
        with self.assertRaises(ProjectError):
            self.store.complete(self.pid, self.scope)

    def test_pause_survives_restart_and_cancel_closes_round(self):
        rid = self.round()
        self.store.control(self.pid, self.scope, 'pause')
        other = ProjectStore(self.path, clock=lambda: self.now)
        self.assertEqual(other.get(self.pid, self.scope)['state'], 'paused')
        with self.assertRaises(ProjectError):
            other.consume(self.pid, self.scope, 'agent_calls', request_key='call')
        other.control(self.pid, self.scope, 'resume')
        self.assertEqual(other.get(self.pid, self.scope)['state'], 'planning')
        other.control(self.pid, self.scope, 'cancel')
        self.assertEqual(other.get(self.pid, self.scope)['rounds'][0]['state'], 'cancelled')
        with self.assertRaises(ProjectError):
            other.advance_round(self.pid, self.scope, rid, expected_state='planning', state='agreed')

    def test_waiting_for_input_is_distinct_from_pause_and_survives_restart(self):
        self.round()
        self.store.wait_for_input(self.pid, self.scope, 'Choose the supported input format')
        self.store.control(self.pid, self.scope, 'pause')
        project = ProjectStore(self.path, clock=lambda: self.now).get(self.pid, self.scope)
        self.assertEqual(project['state'], 'awaiting_input')
        self.assertEqual(project['reason'], 'Choose the supported input format')
        self.store.control(self.pid, self.scope, 'resume')
        self.assertEqual(self.store.get(self.pid, self.scope)['state'], 'planning')

    def test_changed_round_state_does_not_accept_stale_transition(self):
        rid = self.round()
        self.store.advance_round(self.pid, self.scope, rid, expected_state='planning', state='agreed')
        with self.assertRaises(ProjectError):
            self.store.advance_round(self.pid, self.scope, rid, expected_state='planning', state='failed')
        self.assertEqual(self.store.get(self.pid, self.scope)['rounds'][0]['state'], 'agreed')

    def test_usage_replay_is_charged_once_and_payload_conflict_is_rejected(self):
        self.store.consume(self.pid, self.scope, 'agent_calls', request_key='call')
        self.store.consume(self.pid, self.scope, 'agent_calls', request_key='call')
        self.assertEqual(self.store.get(self.pid, self.scope)['usage']['agent_calls'], 1)
        with self.assertRaises(ProjectError):
            self.store.consume(self.pid, self.scope, 'corrections', request_key='call')

    def test_budget_exhaustion_is_durable_and_requires_limit_increase(self):
        self.store.set_limits(self.pid, self.scope, {'agent_calls': 1})
        self.store.consume(self.pid, self.scope, 'agent_calls', request_key='first')
        with self.assertRaises(BudgetExceeded):
            self.store.consume(self.pid, self.scope, 'agent_calls', request_key='second')
        other = ProjectStore(self.path, clock=lambda: self.now)
        self.assertEqual(other.get(self.pid, self.scope)['state'], 'limited')
        with self.assertRaises(ProjectError):
            other.control(self.pid, self.scope, 'resume')
        other.set_limits(self.pid, self.scope, {'agent_calls': 2})
        other.control(self.pid, self.scope, 'resume')
        other.consume(self.pid, self.scope, 'agent_calls', request_key='new-call')
        self.assertEqual(other.get(self.pid, self.scope)['usage']['agent_calls'], 2)

    def test_concurrent_workers_cannot_both_spend_last_call(self):
        self.store.set_limits(self.pid, self.scope, {'agent_calls': 1})
        def consume(key):
            try:
                ProjectStore(self.path).consume(self.pid, self.scope, 'agent_calls', request_key=key)
                return True
            except ProjectError:
                return False
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(consume, ['a', 'b']))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.store.get(self.pid, self.scope)['usage']['agent_calls'], 1)

    def test_elapsed_limit_includes_restart_and_pause(self):
        self.store.set_limits(self.pid, self.scope, {'elapsed_seconds': 10})
        self.store.consume(self.pid, self.scope, 'agent_calls', request_key='first')
        self.store.control(self.pid, self.scope, 'pause')
        self.now += 11
        self.store.control(self.pid, self.scope, 'resume')
        with self.assertRaises(BudgetExceeded):
            self.store.consume(self.pid, self.scope, 'agent_calls', request_key='second')
        self.assertEqual(self.store.get(self.pid, self.scope)['reason'], 'elapsed_seconds')

    def test_credential_is_rejected_before_document_or_event_persistence(self):
        guard = PrivacyGuard(('private-credential-canary-123',))
        other = ProjectStore(self.path, privacy=guard)
        with self.assertRaises(PrivacyViolation):
            other.save_document(self.pid, self.scope, 'spec', 'private-credential-canary-123', expected_version=0)
        self.assertIsNone(other.document(self.pid, self.scope, 'spec'))
        self.assertEqual(len(other.events(self.pid, self.scope)), 1)

    def test_limits_and_event_cursor_are_validated(self):
        for changes in ({'unknown': 1}, {'agent_calls': True}, {'rounds': 0}, {'elapsed_seconds': 9999999}):
            with self.assertRaises(ProjectError):
                self.store.set_limits(self.pid, self.scope, changes)
        self.documents()
        first = self.store.events(self.pid, self.scope, limit=1)[0]
        self.assertEqual(len(self.store.events(self.pid, self.scope, after=first['id'])), 2)
        self.assertEqual(self.store.get(self.pid, self.scope)['limits'], DEFAULT_LIMITS)


if __name__ == '__main__':
    unittest.main()
