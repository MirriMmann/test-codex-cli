"""Opt-in live regression for long commands and a seven-criterion review."""
import json
from pathlib import Path
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_permission_probe import ProbeClient, probe_directory
from task_protocol import parse_report
from verification import TaskVerifier


def main():
    runtime = Path(__file__).resolve().parents[1] / '.runtime'
    runtime.mkdir(exist_ok=True)
    with probe_directory(runtime) as root:
        subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
        (root / 'probe.txt').write_bytes(b'second')
        script = ("from pathlib import Path; assert Path('probe.txt').read_bytes() == b'second'; "
                  + '# ' + 'transport length regression ' * 80)
        command = 'python -B -c ' + json.dumps(script)
        # Reproduce the previously rejected executor report without executing work twice.
        report = parse_report(json.dumps({'status': 'completed', 'summary': 'Fixture prepared',
            'changes': [], 'remaining': [], 'checks': [
                {'command': command, 'status': 'not_run', 'exit_code': None, 'summary': 'For independent review'}]}))
        criteria = [
            'probe.txt exists as a regular file.',
            'Its bytes are exactly the six UTF-8 bytes of second.',
            'The original requested Python command tests those exact file bytes.',
            'The exact original inline command is longer than 1000 characters.',
            'Run that exact inline command and include it in checks without truncation.',
            'The inline command exits successfully with code 0.',
            'Verification leaves source files and Git state unchanged.',
        ]
        plan = {'goal': 'Verify long-command report transport', 'constraints': [], 'acceptance_criteria': criteria}
        goal = ('In this isolated protocol test, inspect and run the following exact local Python '
                'assertion. The comment padding intentionally tests command transport; preserve '
                'the full command in your final checks. Never commit, push or change files.\n' + command)
        client = ProbeClient(workdir=str(root), session_file=str(root / '.agent-relay-probe-session.json'),
                             timeout_seconds=180)
        result = TaskVerifier(client).verify({'id': 0, 'repository': str(root), 'goal': goal}, plan, report,
            cancel_event=threading.Event(), params={'model': 'gpt-6-sol', 'reasoning_effort': 'medium'})
        long_observed = any(check['command'] == command and check.get('observed')
                            for check in result['checks'])
        print(json.dumps({'status': result['status'], 'criteria': len(result['criteria']),
                          'command_length': len(command), 'long_command_observed': long_observed,
                          'issues': result['issues']}, ensure_ascii=True), flush=True)
        if result['status'] != 'passed' or not long_observed or len(result['criteria']) != 7:
            raise RuntimeError('Live long-command report regression failed')


if __name__ == '__main__':
    main()
