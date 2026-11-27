"""Project commands use real SQLite, the Telegram boundary and no model calls."""
import unittest

from agent_relay import AgentRelay
from privacy import PrivacyGuard
import test_task_control as control


class ProjectControlTests(unittest.TestCase):
    setUp = control.TaskControlTests.setUp
    api_call = control.TaskControlTests.api_call
    event = control.TaskControlTests.event
    send = control.TaskControlTests.send
    texts = control.TaskControlTests.texts

    def test_create_documents_status_and_restart_never_start_models(self):
        self.send('/project new Guitar adapter')
        self.send('/project spec 1 Import and export GP7')
        self.send('/project plan 1 Build adapter and test round trip')
        self.app = AgentRelay(self.config, self.api, self.codex, self.chat)
        self.send('/project status')
        self.assertIn('Проект #1', self.texts()[-1])
        self.assertIn('ТЗ: v1. План: v1.', self.texts()[-1])
        self.send('/project spec 1')
        self.assertIn('Import and export GP7', self.texts()[-1])
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()

    def test_member_and_topic_access_are_enforced(self):
        self.send('/project new Shared project')
        self.send('/project status 1', user=11)
        self.assertIn('не найден', self.texts()[-1])
        self.send('/project member 1 11')
        self.send('/project status 1', user=11)
        self.assertIn('Проект #1', self.texts()[-1])
        self.send('/project status 1', user=11, topic=9)
        self.assertIn('не найден', self.texts()[-1])
        self.send('/project member 1 99')
        self.assertIn('ALLOWED_USER_IDS', self.texts()[-1])

    def test_peer_bot_cannot_create_project(self):
        self.send('/project new Sneaky', user=20, bot=True)
        self.assertIsNone(self.app.projects.latest((-123, 7, 10)))
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()

    def test_pause_limits_cancel_and_global_stop(self):
        self.send('/project new Project')
        self.send('/project limits 1 agent_calls=3 rounds=2')
        self.send('/project pause 1')
        self.send('/project resume 1')
        self.assertEqual(self.app.projects.get(1, (-123, 7, 10))['limits']['agent_calls'], 3)
        self.send('/stop')
        self.send('/project new Cannot start')
        self.assertIn('приостановлен', self.texts()[-1])
        self.send('/project pause 1')
        self.send('/project resume 1')
        self.assertIn('Сначала /resume', self.texts()[-1])
        self.send('/project cancel 1')
        self.assertEqual(self.app.projects.get(1, (-123, 7, 10))['state'], 'cancelled')

    def test_private_secret_is_scrubbed_before_storage(self):
        canary = 'private-project-credential-123'
        self.app.privacy = PrivacyGuard((canary,))
        self.app.projects.privacy = self.app.privacy
        self.send('/project new ' + canary)
        goal = self.app.projects.get(1, (-123, 7, 10))['goal']
        self.assertNotIn(canary, goal)
        self.assertIn('[REDACTED]', goal)

    def test_malformed_commands_return_diagnostics_without_model_call(self):
        for command in ('/project', '/project new', '/project spec nope',
                        '/project limits 1 rounds=ten', '/project status ' + '9' * 100):
            self.send(command)
        self.assertTrue(self.texts())
        self.codex.send.assert_not_called()
        self.chat.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
