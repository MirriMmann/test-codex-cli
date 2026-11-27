"""Run one backend turn in an isolated process that can be stopped."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace


class GenerationCancelled(Exception):
    pass


class GenerationFailed(RuntimeError):
    """A worker error already sanitized before crossing the process boundary."""


def terminate_worker(process: subprocess.Popen) -> None:
    """Terminate our worker and its Codex/tool children, never other bots."""
    if process.poll() is not None:
        return
    if os.name == 'nt':
        try:
            subprocess.run(
                ['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


class ProcessClient:
    backend = 'codex'

    def __init__(self, *, workdir: str, session_file: str, binary: str = '',
                 sandbox: str = 'workspace-write', timeout_seconds: float = 1800,
                 worker_path: Path | None = None, network_access: bool = False):
        self.config = {'workdir': str(Path(workdir).resolve()),
                       'session_file': str(Path(session_file).resolve()),
                       'binary': binary, 'sandbox': sandbox, 'network_access': network_access}
        self.timeout_seconds = timeout_seconds
        self.worker_path = worker_path or Path(__file__).with_name('generation_worker.py')

    def request_payload(self, prompt: str, params: dict) -> dict:
        return {'backend': self.backend, self.backend: self.config, 'prompt': prompt, 'params': params}

    def worker_environment(self):
        from codex_client import child_environment
        return child_environment()

    def send(self, prompt: str, *, cancel_event: threading.Event | None = None, **params):
        cancelled = cancel_event or threading.Event()
        if cancelled.is_set():
            raise GenerationCancelled()
        payload = json.dumps(self.request_payload(prompt, params)).encode('utf-8')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        process = subprocess.Popen(
            [sys.executable, str(self.worker_path)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.worker_environment(), **options,
        )
        deadline = time.monotonic() + self.timeout_seconds
        try:
            first = True
            while True:
                if cancelled.is_set():
                    raise GenerationCancelled()
                if time.monotonic() >= deadline:
                    raise GenerationFailed(f'{self.backend} request timed out. Send a new message when ready to retry')
                try:
                    output, _ = process.communicate(input=payload if first else None, timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if cancelled.is_set():
                raise GenerationCancelled()
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError):
                raise GenerationFailed('Generation worker returned no usable response') from None
            if process.returncode or not isinstance(result, dict):
                raise GenerationFailed('Generation worker failed')
            if result.get('error'):
                # The worker emits only safe CodexError diagnostics, never raw logs.
                raise GenerationFailed(result['error'])
            return SimpleNamespace(text=result.get('text', ''), commands=result.get('commands', []),
                                   tools_used=result.get('tools_used', False), publication=result.get('publication'))
        finally:
            terminate_worker(process)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


class ChatProcessClient(ProcessClient):
    backend = 'chat'

    def __init__(self, *, auth_file: str, conversation_id: str, timeout_seconds: float = 180,
                 worker_path: Path | None = None):
        self.config = {'auth_file': str(Path(auth_file).expanduser().resolve()),
                       'conversation_id': conversation_id}
        self.timeout_seconds = timeout_seconds
        self.worker_path = worker_path or Path(__file__).with_name('generation_worker.py')


class PublicationProcessClient(ProcessClient):
    backend = 'publish'

    def __init__(self, *, workdir: str, worker_path: Path | None = None):
        super().__init__(workdir=workdir, session_file='unused', timeout_seconds=360,
                         worker_path=worker_path or Path(__file__).with_name('publication_worker.py'))

    def request_payload(self, prompt: str, params: dict) -> dict:
        if Path(params['plan']['root']).resolve() != Path(self.config['workdir']):
            raise GenerationFailed('Publication repository differs from the configured checkout')
        return {'message': prompt, 'plan': params['plan']}

    def worker_environment(self):
        from codex_client import child_environment
        return child_environment()
