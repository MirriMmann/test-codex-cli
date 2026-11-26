"""Opt-in real CLI check: isolated repo, new/resumed write, read-only review.

Run from the relay directory with .venv/Scripts/python tests/codex_permission_probe.py.
Uses the existing Codex login; never touches the configured bot repository/session.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading
import shutil
import uuid
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from generation import ProcessClient
from verification import TaskVerifier


class ProbeClient(ProcessClient):
    verification_commands = ()

    def send(self, *args, **params):
        result = super().send(*args, **params)
        if params.get('verification'):
            self.verification_commands = result.commands
        return result


@contextmanager
def probe_directory(runtime):
    # TemporaryDirectory uses a private Windows DACL. A sandbox-created file
    # there may inherit no ACE for the host user. Use normal workspace inheritance.
    root = (runtime.resolve() / ('permission-probe-' + uuid.uuid4().hex)).resolve()
    if root.parent != runtime.resolve():
        raise RuntimeError('Probe escaped its runtime directory')
    root.mkdir()
    try:
        yield root
    finally:
        if root.parent != runtime.resolve() or not root.name.startswith('permission-probe-'):
            raise RuntimeError('Unsafe probe cleanup target')
        shutil.rmtree(root)


def main():
    runtime = Path(__file__).resolve().parents[1] / '.runtime'
    runtime.mkdir(exist_ok=True)
    with probe_directory(runtime) as folder:
        root = Path(folder).resolve()
        subprocess.run(['git', 'init', '-q', str(root)], capture_output=True, check=True)
        (root / 'check_probe.py').write_text(
            "from pathlib import Path\nassert Path(__file__).with_name('probe.txt').read_bytes() == b'second'\n",
            encoding='utf-8')
        client = ProbeClient(workdir=str(root), session_file=str(root / '.agent-relay-probe-session.json'),
                               timeout_seconds=180)
        params = {'model': 'gpt-6-sol', 'reasoning_effort': 'medium'}
        phases = (('new', 'first'), ('resume', 'second'))
        if '--resume-read-only' in sys.argv:
            read_only = ProbeClient(workdir=str(root), session_file=str(root / '.agent-relay-probe-session.json'),
                                    sandbox='read-only', timeout_seconds=180)
            read_only.send('Initialize a read-only permission test session. List this disposable directory '
                           'using a shell command, do not edit anything, and reply READY. '
                           'Never commit, push, use the network or read private files.', **params)
            phases = (('resume_from_read_only', 'second'),)
        if '--review-only' in sys.argv:
            (root / 'probe.txt').write_text('second', encoding='utf-8')
            phases = ()
        for phase, value in phases:
            result = client.send(
                'Permission integration test in an isolated disposable repository. '
                'Use a shell command to inspect the directory, then create/overwrite ONLY probe.txt '
                f'with the exact UTF-8 contents {value!r} (no newline). '
                'Use local tools; never commit, push, access the network, or read private files. '
                'If a command is blocked, report it honestly. Finish with a brief result.', **params)
            actual = (root / 'probe.txt').read_text(encoding='utf-8-sig') if (root / 'probe.txt').exists() else ''
            print(json.dumps({'phase': phase, 'file_verified': actual == value,
                              'observed_commands': len(result.commands)}, ensure_ascii=True), flush=True)
            if actual != value:
                raise RuntimeError('CLI could not write the requested probe file')
        plan = {'goal': 'Write probe.txt with second', 'constraints': ['Inspect and run the existing check_probe.py regression test.'],
                'acceptance_criteria': ['probe.txt contains exactly second']}
        report = {'status': 'completed', 'summary': 'Probe written',
                  'changes': [{'path': 'probe.txt', 'summary': 'Probe fixture'}], 'checks': [], 'remaining': []}
        review = TaskVerifier(client).verify({'id': 0, 'repository': str(root), 'goal': plan['goal']},
                                            plan, report, cancel_event=threading.Event(), params=params)
        print(json.dumps({'phase': 'read_only_review', 'status': review['status'],
                          'checks': review['checks'], 'issues': review['issues'],
                          'actual_commands': client.verification_commands}, ensure_ascii=True), flush=True)
        (runtime / 'permission-probe-evidence.json').write_text(json.dumps({
            'reported': [check['command'] for check in review['checks']],
            'actual': [check['command'] for check in client.verification_commands],
        }), encoding='utf-8')
        if review['status'] != 'passed':
            raise RuntimeError('Real independent read-only verification did not pass')


if __name__ == '__main__':
    main()
