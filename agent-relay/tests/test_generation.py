import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from generation import ChatProcessClient, GenerationCancelled, GenerationFailed, ProcessClient, PublicationProcessClient


class ProcessClientTests(unittest.TestCase):
    def test_publication_worker_carries_only_host_manifest_and_message(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Path(folder) / 'pub.py'
            worker.write_text('import json,sys\np=json.load(sys.stdin)\n'
                              'assert set(p)=={"plan","message"}\n'
                              'print(json.dumps({"text":"checked","publication":{"status":"failed","error":"offline"}}))\n',
                              encoding='utf-8')
            result = PublicationProcessClient(workdir=folder, worker_path=worker).send('Task #1', plan={'root': folder})
            self.assertEqual(result.publication['status'], 'failed')

    def test_wrong_publication_root_is_rejected_before_worker_launch(self):
        with tempfile.TemporaryDirectory() as folder, patch('generation.subprocess.Popen') as spawn:
            with self.assertRaises(GenerationFailed):
                PublicationProcessClient(workdir=folder).send('Task #1', plan={'root': str(Path(folder) / 'other')})
            spawn.assert_not_called()
    def test_verification_flags_and_cli_evidence_cross_process_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Path(folder) / 'worker.py'
            worker.write_text(
                'import json,sys\np=json.load(sys.stdin)\n'
                'assert p["params"]["verification"] and "allow_publish" not in p["params"]\n'
                'print(json.dumps({"text":"review","commands":[{"command":"npm test","exit_code":1}],"tools_used":True}))\n',
                encoding='utf-8')
            result = ProcessClient(workdir=folder, session_file=str(Path(folder) / 'session.json'),
                                   worker_path=worker).send('check', verification=True)
            self.assertEqual(result.commands, [{'command': 'npm test', 'exit_code': 1}])
            self.assertTrue(result.tools_used)
    def test_chat_worker_protocol_contains_only_chat_config(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Path(folder) / 'worker.py'
            worker.write_text(
                'import json,sys\np=json.load(sys.stdin)\n'
                'assert p["backend"]=="chat" and "codex" not in p\n'
                'print(json.dumps({"text":p["chat"]["conversation_id"]+":"+p["prompt"]}))\n',
                encoding='utf-8',
            )
            result = ChatProcessClient(auth_file='unused.json', conversation_id='chat-id', worker_path=worker).send('hello')
            self.assertEqual(result.text, 'chat-id:hello')

    def test_worker_receives_request_and_returns_text(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Path(folder) / 'worker.py'
            worker.write_text(
                'import json,sys\np=json.load(sys.stdin)\n'
                'print(json.dumps({"text":p["prompt"]+p["params"]["conversation"]}))\n',
                encoding='utf-8',
            )
            result = ProcessClient(workdir=folder, session_file=str(Path(folder) / 'session.json'), worker_path=worker).send('Hello ', conversation='world')
            self.assertEqual(result.text, 'Hello world')

    def test_cancellation_terminates_blocked_worker_before_any_text(self):
        with tempfile.TemporaryDirectory() as folder:
            worker, marker = Path(folder) / 'worker.py', Path(folder) / 'started'
            worker.write_text(
                'import json,sys,time,subprocess\nfrom pathlib import Path\np=json.load(sys.stdin)\n'
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                'Path(p["params"]["marker"]).write_text("started")\ntime.sleep(60)\n'
                'print(json.dumps({"text":"must not be sent"}))\n', encoding='utf-8',
            )
            cancel = threading.Event()
            client = ProcessClient(workdir=folder, session_file=str(Path(folder) / 'session.json'), worker_path=worker)
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(client.send, 'hello', marker=str(marker), cancel_event=cancel)
                deadline = time.monotonic() + 5
                try:
                    while not marker.exists() and time.monotonic() < deadline and not future.done():
                        time.sleep(0.01)
                    self.assertTrue(marker.exists(), 'Worker did not start')
                finally:
                    cancel.set()
                with self.assertRaises(GenerationCancelled):
                    future.result(timeout=10)

    def test_cancelled_request_does_not_launch_worker(self):
        cancel = threading.Event()
        cancel.set()
        with patch('generation.subprocess.Popen') as popen:
            with self.assertRaises(GenerationCancelled):
                ProcessClient(workdir='.', session_file='unused-session.json').send('hello', cancel_event=cancel)
        popen.assert_not_called()

    def test_bad_worker_output_does_not_leak_raw_output(self):
        with tempfile.TemporaryDirectory() as folder:
            worker = Path(folder) / 'worker.py'
            worker.write_text('print("private raw response")\n', encoding='utf-8')
            with self.assertRaises(GenerationFailed) as caught:
                ProcessClient(workdir=folder, session_file=str(Path(folder) / 'session.json'), worker_path=worker).send('hello')
            self.assertNotIn('private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
