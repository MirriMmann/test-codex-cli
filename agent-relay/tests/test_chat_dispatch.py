import json
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from chat_dispatch import parse_dispatch
from task_protocol import OrchestrationError
import test_routing as routing_tests
import test_orchestration as orchestration_tests

PLAN = orchestration_tests.PLAN


PLAN_DATA = {'goal': 'Add four calculator operations', 'constraints': ['No dependencies'],
             'acceptance_criteria': ['Four operations work and existing tests pass']}


class DispatchProtocolTests(unittest.TestCase):
    def test_strict_task_and_reply_contracts(self):
        self.assertEqual(parse_dispatch(json.dumps({'action': 'task', 'plan': PLAN_DATA})), ('', PLAN_DATA))
        self.assertEqual(parse_dispatch('{"action":"reply","reply":"Привет"}'), ('Привет', None))
        self.assertEqual(parse_dispatch('ordinary prose'), ('ordinary prose', None))
        self.assertEqual(parse_dispatch('[[handoff:Codex]] do this'), ('[[handoff:Codex]] do this', None))
        self.assertEqual(parse_dispatch('{"action":"publish"}'), ('', {'publication_task_id': None}))
        self.assertEqual(parse_dispatch('{"action":"publish","task_id":12}'), ('', {'publication_task_id': 12}))

    def test_malformed_or_authority_expanding_envelope_never_launches(self):
        for value in (
            '{"action":"task"',
            '{"action":"reply","action":"task","reply":"hello"}',
            '{"action":"publish","task_id":true}',
            '{"action":"publish","task_id":-1}',
            '{"action":"publish","branch":"main"}',
            json.dumps({'action': 'task', 'plan': {**PLAN_DATA, 'repository': 'another'}}),
            json.dumps({'action': 'task', 'plan': PLAN_DATA, 'allow_push': True}),
            json.dumps({'action': 'task', 'plan': {**PLAN_DATA, 'acceptance_criteria': []}}),
        ):
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                parse_dispatch(value)


class DispatchIntegrationTests(unittest.TestCase):
    setUp = routing_tests.AliasRoutingTests.setUp
    api_call = routing_tests.AliasRoutingTests.api_call
    new_app = routing_tests.AliasRoutingTests.new_app
    event = routing_tests.AliasRoutingTests.event

    def answer(self, payload, *, sender=None):
        self.chat.send.return_value = SimpleNamespace(text=json.dumps(payload))
        self.app._executor, self.app._chat_executor = Mock(), Mock()
        update = self.event('чувак, добавь калькулятор', sender=sender)
        update['message']['message_thread_id'] = 77
        self.app.process_update(update)
        chat_task = self.app._chat_pending.popleft()
        chat_task.future = Future()
        try:
            chat_task.future.set_result(self.app._send_generation(chat_task))
        except Exception as exc:
            chat_task.future.set_exception(exc)
        self.app._finish_generation(chat_task)
        return update, chat_task

    def test_human_task_keeps_owner_topic_repo_and_codex_settings_without_second_chat_call(self):
        update, chat_task = self.answer({'action': 'task', 'plan': PLAN_DATA})
        self.chat.send.assert_called_once()
        self.codex.send.assert_not_called()
        child = self.app._pending[0]
        row = self.app.tasks.get(child.work_id)
        self.assertEqual((row['owner_id'], row['topic_id'], row['owner_is_bot']), (10, 77, 0))
        self.assertEqual(row['goal'], update['message']['text'])
        self.assertEqual(child.prepared_plan, PLAN_DATA)
        self.assertEqual(child.backend, 'codex')
        self.assertNotIn('allow_publish', child.params)
        self.app._finish_generation(chat_task)
        self.app.process_update(update)
        self.assertEqual(len(self.app._pending), 1)
        self.assertIsNone(self.app._dispatch_coding_task(chat_task, PLAN_DATA))
        texts = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
        self.assertTrue(any('Передана Codex' in text for text in texts))
        self.assertFalse(any('acceptance_criteria' in text for text in texts))

    def test_discussion_does_not_queue_and_sends_only_reply(self):
        self.answer({'action': 'reply', 'reply': 'Вот объяснение кода.'})
        self.assertFalse(self.app._pending)
        texts = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
        self.assertEqual(texts[-1], 'ChatGPT\n\nВот объяснение кода.')

    def test_model_publication_action_without_human_request_cannot_authorize_push(self):
        self.answer({'action': 'publish', 'task_id': 99})
        self.assertFalse(self.app._pending)
        texts = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
        self.assertTrue(any('нужна явная просьба человека' in text for text in texts))

    def test_invalid_response_and_secret_never_queue(self):
        for payload in ({'action': 'task', 'plan': {**PLAN_DATA, 'owner': 11}},
                        {'action': 'task', 'plan': {**PLAN_DATA, 'goal': 'ghp_' + 'a' * 30}}):
            self.answer(payload)
            self.assertFalse(self.app._pending)

    def test_peer_cannot_request_internal_handoff(self):
        self.answer({'action': 'task', 'plan': PLAN_DATA},
                    sender={'id': 20, 'username': 'PeerBot', 'is_bot': True})
        self.assertFalse(self.app._pending)
        self.assertNotIn('[AgentRelay / internal task routing]', self.chat.send.call_args.args[0])

    def test_direct_task_is_immediate_and_does_not_use_chat(self):
        self.app._executor = Mock()
        self.app.process_update(self.event('/task добавь калькулятор'))
        self.chat.send.assert_not_called()
        child = self.app._pending[0]
        self.assertEqual(child.prepared_plan['goal'], 'добавь калькулятор')

    def test_cancelled_chat_does_not_dispatch(self):
        self.app._executor = Mock()
        update = self.event('чувак, добавь калькулятор')
        self.app.process_update(update)
        task = self.app._pending.popleft()
        task.future = Future()
        task.future.set_result(SimpleNamespace(text=json.dumps({'action': 'task', 'plan': PLAN_DATA})))
        task.cancelled.set()
        self.app._finish_generation(task)
        self.assertFalse(self.app._pending)


class PreparedPlanTests(unittest.TestCase):
    setUp = orchestration_tests.OrchestratorTests.setUp

    def test_handoff_reuses_plan_but_still_executes_and_verifies(self):
        self.task.prepared_plan = PLAN
        self.runner.run(self.task)
        self.planner.assert_not_called()
        self.assertGreaterEqual(self.executor.send.call_count, 2)
        self.assertEqual(json.loads(self.store.get(self.task_id)['plan_json'])['planner'], 'router')

    def test_invalid_prepared_plan_stops_before_executor(self):
        self.task.prepared_plan = {**PLAN, 'allow_push': True}
        with self.assertRaises(OrchestrationError):
            self.runner.run(self.task)
        self.executor.send.assert_not_called()
        self.planner.assert_not_called()


class HandoffLifecycleTests(unittest.TestCase):
    setUp = orchestration_tests.PipelineIntegrationTests.setUp
    event = orchestration_tests.PipelineIntegrationTests.event

    def test_discussion_to_execution_verification_and_report_in_one_pipeline(self):
        self.chat.send.return_value = SimpleNamespace(text=json.dumps({'action': 'task', 'plan': PLAN_DATA}))
        self.app.process_update(self.event('/discuss добавь калькулятор'))
        self.chat.send.assert_called_once()
        self.assertEqual(self.executor.send.call_count, 2)  # execution plus independent verification
        record = self.app.tasks.get(1)
        self.assertEqual((record['status'], record['verification'], record['attempts']),
                         ('completed', 'passed', 1))
        self.assertEqual(json.loads(record['plan_json'])['goal'], PLAN_DATA['goal'])
        self.assertEqual(record['publication_status'], '')
        texts = [c.args[1]['text'] for c in self.api.call.call_args_list if c.args[0] == 'sendMessage']
        self.assertTrue(any(t.startswith('codex\n\nЗадача #1') for t in texts))
        self.assertFalse(any('"action"' in t for t in texts))


if __name__ == '__main__':
    unittest.main()
