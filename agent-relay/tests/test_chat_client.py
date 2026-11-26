import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chat_client import ChatClient, ChatError, check_chat_setup, ensure_auth_file, normalize_conversation_id

CHAT = '6b0737f7-7054-83ed-a188-1ad6012e48e1'


class ChatClientTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.auth = Path(self.folder.name) / 'auth.json'
        self.write_auth({'accessToken': 'private-token', 'sessionToken': 'private-session'})

    def write_auth(self, value):
        self.auth.write_text(json.dumps(value), encoding='utf-8')

    def test_auth_accepts_raw_session_and_chunked_cookie(self):
        ensure_auth_file(str(self.auth))
        self.write_auth({'accessToken': 'private-token',
                         'cookies': {'__Secure-next-auth.session-token.0': 'private-session'}})
        ensure_auth_file(str(self.auth))

    def test_auth_requires_access_and_session_without_echoing_data(self):
        for value in ({'private-token': 'private-session'},
                      {'accessToken': 'private-token'},
                      {'accessToken': 'private-token', 'sessionToken': ' '},
                      {'accessToken': 'private-token', 'cookies': {'unrelated': 'private-session'}},
                      {'accessToken': 'private-token', 'cookies': {'__Secure-next-auth.session-token': []}}):
            with self.subTest(value=value):
                self.write_auth(value)
                with self.assertRaises(ChatError) as caught:
                    ensure_auth_file(str(self.auth))
                self.assertNotIn('private-token', str(caught.exception))
                self.assertNotIn('private-session', str(caught.exception))

    def test_auth_bad_json_size_missing_and_bom(self):
        self.auth.write_text('\ufeff' + json.dumps({'accessToken': 'x', 'sessionToken': 'y'}), encoding='utf-8')
        ensure_auth_file(str(self.auth))
        self.auth.write_text('private-invalid-json', encoding='utf-8')
        with self.assertRaisesRegex(ChatError, 'cannot be read or parsed'):
            ensure_auth_file(str(self.auth))
        self.auth.write_bytes(b' ' * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ChatError, 'size limit'):
            ensure_auth_file(str(self.auth))
        with self.assertRaisesRegex(ChatError, 'missing'):
            ensure_auth_file(str(self.auth.with_name('missing.json')))

    def setup_patches(self, *, version='0.4.1', node_version='v26.13.1', curl=True):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('chat_client.importlib.util.find_spec', return_value=Mock()))
        stack.enter_context(patch('chat_client.importlib.metadata.version', return_value=version))
        stack.enter_context(patch('chat_client.shutil.which', side_effect=lambda name: name if name == 'node' or curl else None))
        run = stack.enter_context(patch('chat_client.subprocess.run', return_value=SimpleNamespace(stdout=node_version, returncode=0)))
        return run

    def test_preflight_is_offline_and_only_checks_node_version(self):
        run = self.setup_patches()
        check_chat_setup(str(self.auth))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ['node', '--version'])

    def test_preflight_requires_matching_adapter_version(self):
        for version in ('0.4.0', '0.5.0', '0.4.1-unsafe'):
            with self.subTest(version=version):
                self.setup_patches(version=version)
                with self.assertRaisesRegex(ChatError, 'adapter>=0.4.1'):
                    check_chat_setup(str(self.auth))

    def test_preflight_rejects_old_node_and_missing_curl(self):
        self.setup_patches(node_version='v22.9.0')
        with self.assertRaisesRegex(ChatError, 'Node.js 24'):
            check_chat_setup(str(self.auth))
        self.setup_patches(curl=False)
        with self.assertRaisesRegex(ChatError, 'curl'):
            check_chat_setup(str(self.auth))

    def test_preflight_sanitizes_process_errors(self):
        run = self.setup_patches()
        run.side_effect = subprocess.TimeoutExpired(['node', '--version'], 10)
        with self.assertRaisesRegex(ChatError, 'Cannot run Node'):
            check_chat_setup(str(self.auth))

    def test_discussion_continues_exact_selected_chat_with_model_parameters(self):
        with patch('relay_cwa.RelayHTTPClient') as adapter:
            adapter.return_value.send.return_value = SimpleNamespace(text='hello')
            client = ChatClient(auth_file=str(self.auth), conversation_id='https://chatgpt.com/c/' + CHAT)
            self.assertEqual(client.send('plain question').text, 'hello')
            adapter.assert_called_once_with(auth_file=str(self.auth.resolve()))
            adapter.return_value.send.assert_called_once_with('plain question', conversation=CHAT, temporary=False)
            client.send('second question', model='gpt-6-instant', reasoning_effort='medium')
            adapter.assert_called_once()
            adapter.return_value.send.assert_called_with('second question', conversation=CHAT, temporary=False,
                                                       model='gpt-6-instant', reasoning_effort='medium')

    def test_planning_is_explicitly_isolated_from_persistent_discussion(self):
        with patch('relay_cwa.RelayHTTPClient') as adapter:
            client = ChatClient(auth_file=str(self.auth), conversation_id=CHAT)
            client.send('plan this', isolated=True)
            adapter.return_value.send.assert_called_once_with('plan this', conversation=None, temporary=True)
            client.send('ordinary follow-up')
            adapter.return_value.send.assert_called_with('ordinary follow-up', conversation=CHAT, temporary=False)

    def test_chat_reference_validation_rejects_unrelated_urls_without_echoing_input(self):
        for valid in (CHAT, CHAT.upper(), 'https://chatgpt.com/c/' + CHAT + '/',
                      'https://chatgpt.com/g/g-p-project/c/' + CHAT):
            self.assertEqual(normalize_conversation_id(valid), CHAT)
        for bad in ('isolated', 'not-an-id', 'https://evil.test/c/' + CHAT,
                    'https://chatgpt.com.evil.test/c/' + CHAT, 'http://chatgpt.com/c/' + CHAT,
                    'https://private-token@chatgpt.com/c/' + CHAT, 'https://chatgpt.com/share/' + CHAT,
                    'https://chatgpt.com/c/' + CHAT + '?token=private-token'):
            with self.subTest(value=bad), self.assertRaises(ChatError) as caught:
                normalize_conversation_id(bad)
            self.assertNotIn('private-token', str(caught.exception))

    def test_send_failure_does_not_disclose_raw_error(self):
        with patch('relay_cwa.RelayHTTPClient') as adapter:
            adapter.return_value.send.side_effect = RuntimeError('private-token raw body')
            with self.assertRaises(ChatError) as caught:
                ChatClient(auth_file=str(self.auth), conversation_id=CHAT).send('private prompt')
            self.assertIn('RuntimeError', str(caught.exception))
            self.assertNotIn('private', str(caught.exception))

    def test_empty_conversation_refused_without_loading_adapter(self):
        with self.assertRaisesRegex(ChatError, 'CWA_CONVERSATION_ID'):
            ChatClient(auth_file=str(self.auth), conversation_id=' ')


if __name__ == '__main__':
    unittest.main()
