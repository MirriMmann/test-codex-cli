"""Offline bootstrap checks; no installation, credentials, or network."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_supported_python(self):
        self.assertTrue(bootstrap.supported_python((3, 10)))
        self.assertTrue(bootstrap.supported_python((3, 15)))
        self.assertFalse(bootstrap.supported_python((3, 9)))
        self.assertFalse(bootstrap.supported_python((3, 16)))

    def test_broken_venv_is_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'python.exe'
            executable.touch()
            with patch.object(bootstrap, 'venv_python', return_value=executable), \
                 patch.object(bootstrap.subprocess, 'run', side_effect=[Mock(returncode=1), Mock(returncode=0, stdout='3.15\n')]), \
                 patch.object(bootstrap.venv, 'EnvBuilder') as builder:
                self.assertEqual(bootstrap.setup_venv(), executable)
                builder.return_value.create.assert_called_once_with(bootstrap.VENV)
                builder.assert_called_once_with(with_pip=False)

    def test_parse_env_without_executing_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text("# comment\nTELEGRAM_CHAT_ID=-100123\nMALFORMED\nCODEX_WORKDIR='../project'\n", encoding='utf-8')
            self.assertEqual(bootstrap.read_env_values(path), {
                'TELEGRAM_CHAT_ID': '-100123', 'CODEX_WORKDIR': '../project',
            })

    def test_missing_config_is_created_without_auth_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.env.example').write_text('CODEX_WORKDIR=../project\n', encoding='utf-8')
            with patch.object(bootstrap, 'ROOT', root):
                with self.assertRaisesRegex(RuntimeError, 'Created .env'):
                    bootstrap.validate_local_config()
            self.assertTrue((root / '.env').is_file())

    def test_config_requires_workspace_instead_of_chatgpt_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.env').write_text('TELEGRAM_BOT_TOKEN=test\nTELEGRAM_CHAT_ID=-123\nALLOWED_USER_IDS=10\nCODEX_WORKDIR=../project\n', encoding='utf-8')
            with patch.object(bootstrap, 'ROOT', root), patch.dict(os.environ, {}, clear=True), \
                 patch.object(bootstrap, 'validate_workspace') as validate:
                result = bootstrap.validate_local_config()
            self.assertEqual(result['CODEX_WORKDIR'], '../project')
            validate.assert_called_once_with(root / '../project')

    def test_setup_only_needs_no_login_and_does_not_start_bot(self):
        with patch.object(bootstrap, 'validate_python'), \
             patch.object(bootstrap, 'setup_venv', return_value=Path('python.exe')), \
             patch.object(bootstrap, 'resolve_codex_binary', return_value='codex.exe'), \
             patch.object(bootstrap, 'check_login') as login, \
             patch.object(bootstrap, 'validate_local_config') as validate, \
             patch.object(bootstrap, 'run_relay') as launch:
            self.assertEqual(bootstrap.main(['--setup-only']), 0)
        login.assert_not_called()
        validate.assert_not_called()
        launch.assert_not_called()

    def test_login_failure_prevents_bot_start(self):
        with patch.object(bootstrap, 'validate_python'), \
             patch.object(bootstrap, 'setup_venv', return_value=Path('python.exe')), \
             patch.object(bootstrap, 'resolve_codex_binary', return_value='codex.exe'), \
             patch.object(bootstrap, 'validate_local_config'), \
             patch.object(bootstrap, 'check_login', side_effect=RuntimeError('Run codex login')), \
             patch.object(bootstrap, 'run_relay') as launch:
            self.assertEqual(bootstrap.main([]), 1)
        launch.assert_not_called()

    def test_ctrl_c_waits_for_relay_cleanup_and_returns_success_without_prompt(self):
        process = Mock()
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch.object(bootstrap.subprocess, 'Popen', return_value=process), \
             patch.object(bootstrap, 'terminate_worker') as terminate:
            self.assertEqual(bootstrap.run_relay(Path('python.exe'), {}), 0)
        self.assertEqual(process.wait.call_count, 2)
        process.wait.assert_called_with(timeout=12)
        terminate.assert_not_called()

    def test_second_ctrl_c_terminates_only_relay_tree(self):
        process = Mock()
        process.wait.side_effect = [KeyboardInterrupt(), KeyboardInterrupt()]
        with patch.object(bootstrap.subprocess, 'Popen', return_value=process), \
             patch.object(bootstrap.os, 'name', 'nt'), \
             patch.object(bootstrap, 'terminate_worker') as terminate:
            self.assertEqual(bootstrap.run_relay(Path('python.exe'), {}), 0)
        terminate.assert_called_once_with(process)

    def test_chat_setup_disabled_does_not_inspect_credentials_or_install(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bootstrap.subprocess, 'run') as run:
            bootstrap.setup_chat_backend(Path('python.exe'), {})
        run.assert_not_called()

    def test_chat_setup_uses_venv_and_does_not_reinstall_present_adapter(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(bootstrap.subprocess, 'run', return_value=Mock(returncode=0)) as run, \
             patch('builtins.print'):
            bootstrap.setup_chat_backend(Path('venv-python.exe'), {'CWA_CONVERSATION_ID': 'test-id'})
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[0][0] == 'venv-python.exe' for call in run.call_args_list))
        self.assertNotIn('pip', str(run.call_args_list))


if __name__ == '__main__':
    unittest.main()
