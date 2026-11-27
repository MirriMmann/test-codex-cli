"""Official Codex CLI transport with an explicitly scoped, resumable session."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from relay_events import record_event
from task_protocol import MAX_COMMAND_EVENT_CHARS


class CodexError(RuntimeError):
    """Actionable CLI diagnostics without exposing raw logs or credentials."""


def process_options() -> dict:
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def resolve_codex_binary(value: str = '') -> str:
    if value:
        candidate = Path(value).expanduser()
        found = str(candidate.resolve()) if candidate.is_file() else shutil.which(value)
        if not found:
            raise CodexError('CODEX_BIN does not point to an installed Codex CLI')
        return found
    found = shutil.which('codex.exe') or shutil.which('codex')
    if found:
        return found
    # The VS Code extension also ships the official native CLI on Windows.
    if os.name == 'nt':
        candidates = list((Path.home() / '.vscode/extensions').glob('openai.chatgpt-*/bin/windows-*/codex.exe'))
        if candidates:
            return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise CodexError('Codex CLI is missing. Install it, or set CODEX_BIN to codex.exe')


def codex_command(binary: str) -> list[str]:
    path = Path(binary)
    if os.name == 'nt' and path.suffix.lower() in ('.cmd', '.ps1'):
        # Run npm's JS entry point directly: never put a Telegram prompt in cmd.exe.
        script = path.parent / 'node_modules/@openai/codex/bin/codex.js'
        node = shutil.which('node.exe') or shutil.which('node')
        if not script.is_file() or not node:
            raise CodexError('Set CODEX_BIN to the native codex.exe or install the official npm Codex CLI and Node.js')
        return [node, str(script)]
    return [binary]


def validate_workspace(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise CodexError('CODEX_WORKDIR must point to an existing Git checkout')
    try:
        result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                                capture_output=True, timeout=10, **process_options())
    except (OSError, subprocess.TimeoutExpired):
        raise CodexError('Git is unavailable; check the Git installation and CODEX_WORKDIR') from None
    if result.returncode:
        raise CodexError('CODEX_WORKDIR is not a Git working tree; clone your repository first')
    return path


def check_login(binary: str) -> None:
    try:
        result = subprocess.run(codex_command(binary) + ['login', 'status'], capture_output=True,
                                timeout=15, **process_options())
    except (OSError, subprocess.TimeoutExpired):
        raise CodexError('Cannot check Codex login. Run codex login status in PowerShell') from None
    if result.returncode:
        raise CodexError('Codex CLI is not signed in. Run codex login in PowerShell, then restart the bot')


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    # The coding process has no need for the Telegram or legacy web credentials.
    for name in list(env):
        if name.upper() in ('TELEGRAM_BOT_TOKEN', 'CODEX_THREAD_ID') or name.upper().startswith('CWA_'):
            env.pop(name)
    return env


class SessionStore:
    def __init__(self, filename: str, workspace: Path):
        self.path = Path(filename).resolve()
        self.workspace = str(workspace.resolve())

    def read_state(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('workspace') != self.workspace:
                return {}
            if data.get('context_version') != 2:
                # Do not resume history created before public-context isolation.
                return {}
            return {'thread_id': str(uuid.UUID(data['thread_id'])),
                    'interrupted': bool(data.get('interrupted', False))}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise CodexError('Codex session state is unreadable; choose a new CODEX_SESSION_FILE') from None

    def read(self) -> str | None:
        return self.read_state().get('thread_id')

    def write(self, thread_id: str, *, interrupted: bool = False) -> None:
        identity = str(uuid.UUID(thread_id))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump({'workspace': self.workspace, 'thread_id': identity,
                       'interrupted': interrupted, 'context_version': 2}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)


def validate_parameters(model: str, effort: str) -> None:
    if model and not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*', model):
        raise CodexError('CODEX_MODEL must be an exact lowercase model ID, e.g. gpt-6.1-terra; display names with spaces are invalid')
    if effort and effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise CodexError('CODEX_REASONING_EFFORT is invalid; use an effort supported by the model, e.g. medium')


def cli_failure(logs: str, code: int, terminal_error: str = '') -> CodexError:
    # Prefer the terminal JSONL error. Stderr also contains unrelated telemetry.
    source = terminal_error or logs
    text = source.lower()
    codes, statuses = set(), set()

    def inspect(value):
        if not isinstance(value, dict):
            return
        for key in ('status', 'status_code', 'http_status'):
            if type(value.get(key)) is int:
                statuses.add(value[key])
        if isinstance(value.get('code'), str):
            codes.add(value['code'].lower())
        for key in ('error', 'response'):
            inspect(value.get(key))
        message = value.get('message')
        if isinstance(message, str) and message.lstrip().startswith('{'):
            try:
                inspect(json.loads(message))
            except ValueError:
                pass

    for line in source.splitlines():
        start = line.find('{')
        if start >= 0:
            try:
                inspect(json.loads(line[start:]))
            except ValueError:
                pass
    # Never match bare 401/429: they can occur in timestamps and request IDs.
    statuses.update(int(value) for value in re.findall(
        r'\b(?:http(?:/\d(?:\.\d)?)?\s+|status(?:\s+code)?\s*[:=]?\s*)(401|429)\b', text))
    model_failure = 'Codex rejected the model. Check CODEX_MODEL: use the exact model ID available to your account, e.g. gpt-6.1-terra'
    if codes & {'model_not_found', 'unsupported_model'}:
        return CodexError(model_failure)
    if any(value in text for value in ('reasoning.effort', 'reasoning_effort')) and any(
            value in text for value in ('invalid', 'unsupported', 'not supported')):
        return CodexError('Codex rejected CODEX_REASONING_EFFORT. Choose an effort supported by CODEX_MODEL')
    if re.search(r'\bmodel\b[^\n]{0,150}\b(?:not supported|not found|does not exist)', text):
        return CodexError(model_failure)
    if 429 in statuses or codes & {'usage_limit_reached', 'rate_limit_exceeded', 'insufficient_quota'} or any(
            value in text for value in ('usage limit', 'rate limit', 'quota exceeded')):
        return CodexError('Codex usage limit reached. Check your account limits and try a new message later')
    if 401 in statuses or codes & {'authentication_error', 'invalid_api_key'} or any(
            value in text for value in ('not logged in', 'login required', 'authentication failed', 'unauthorized')):
        return CodexError('Codex authentication failed. Run codex login and restart the bot')
    if 'sandbox' in text and any(value in text for value in ('failed', 'setup', 'not supported')):
        return CodexError('Codex sandbox could not start. Open codex in CODEX_WORKDIR and complete its Windows setup')
    return CodexError(f'Codex CLI failed (exit code {code}). Check the model, reasoning effort and CLI configuration')


class CodexCLIClient:
    def __init__(self, *, workdir: str, session_file: str, binary: str = '', sandbox: str = 'workspace-write',
                 network_access: bool = False):
        self.workdir = validate_workspace(workdir)
        self.binary = resolve_codex_binary(binary)
        if sandbox not in ('read-only', 'workspace-write'):
            raise CodexError('CODEX_SANDBOX must be read-only or workspace-write')
        self.sandbox = sandbox
        self.network_access = network_access
        self.sessions = SessionStore(session_file, self.workdir)

    def send(self, prompt: str, *, model: str = '', reasoning_effort: str = '', allow_publish: bool = False,
             structured_report: bool = False, verification: bool = False):
        validate_parameters(model, reasoning_effort)
        if allow_publish:
            raise CodexError('Direct Codex publication is disabled. The relay publishes after verification when explicitly requested by the human')
        state = {} if verification else self.sessions.read_state()
        session = state.get('thread_id')
        if state.get('interrupted'):
            prompt = ('[AgentRelay: the previous turn was interrupted or failed. Do not continue '
                      'its unfinished task automatically. Follow only the new message below; '
                      'inspect any partial changes before performing related work.]\n\n' + prompt)
        # A hard process-tree cancellation must leave an unfinished marker on disk.
        if session:
            self.sessions.write(session, interrupted=True)
        with tempfile.TemporaryDirectory(prefix='agentrelay-codex-') as folder:
            answer = Path(folder) / 'answer.txt'
            command = codex_command(self.binary) + [
                '--no-daemon', '-a', 'never',
            ]
            # Publication is host-owned, authorized by the current human request.
            # Disable legacy MCP capabilities even on resumed coding sessions.
            settings = {
                'command': sys.executable,
                'args': [str(Path(__file__).with_name('git_publish_mcp.py')), '--workdir', str(self.workdir)],
                'enabled': False,
                'enabled_tools': [],
                'required': False,
                'startup_timeout_sec': 15,
                'tool_timeout_sec': 180,
            }
            # Explicitly disable the server on peer turns, including after resume.
            overrides = []
            for key, value in settings.items():
                overrides += ['-c', 'mcp_servers.agent_relay_git.' + key + '=' + json.dumps(value)]
            # exec has its own sandbox/cwd options; root CLI values are not
            # reliably inherited by exec/resume in the installed CLI builds.
            command += ['exec', '-C', str(self.workdir), '-s', 'read-only' if verification else self.sandbox]
            if verification:
                command += ['--ephemeral']
            if session:
                command += ['resume', session]
            # All model turns have only the declared local coding sandbox.
            # User/project MCP tools or execpolicy rules must not grant a second
            # publication path. Authentication still uses the CLI credential store.
            command += ['--ignore-user-config', '--ignore-rules',
                        '-c', 'sandbox_mode=' + json.dumps('read-only' if verification else self.sandbox),
                        '-c', 'approval_policy="never"',
                        '-c', 'projects.' + json.dumps(str(self.workdir)) + '.trust_level="untrusted"',
                        '-c', 'sandbox_workspace_write.network_access=' + str(
                            self.network_access and not verification and self.sandbox == 'workspace-write').lower(),
                        '-c', 'sandbox_workspace_write.writable_roots=[]']
            if os.name == 'nt':
                # Ignoring user config also drops Windows sandbox selection.
                # Without an enabled Windows sandbox, never-approval automation
                # can reject even read-only shell commands before execution.
                command += ['-c', 'windows.sandbox="elevated"']
            # Keep all -c overrides in the exec/resume scope. Mixing root and
            # subcommand -c options loses the root overrides in some CLI builds.
            command += overrides
            command += ['--json', '--output-last-message', str(answer)]
            if structured_report or verification:
                from task_protocol import REPORT_SCHEMA, VERIFICATION_SCHEMA
                schema_file = Path(folder) / 'report-schema.json'
                schema_file.write_text(json.dumps(VERIFICATION_SCHEMA if verification else REPORT_SCHEMA), encoding='utf-8')
                command += ['--output-schema', str(schema_file)]
            if model:
                command += ['--model', model]
            if reasoning_effort:
                command += ['-c', 'model_reasoning_effort=' + json.dumps(reasoning_effort)]
            command += ['-']
            messages = []
            failed = False
            completed = False
            event_error = ''
            commands = []
            tools_used = False
            with tempfile.TemporaryFile() as errors:
                record_event('codex_started', model=model or 'CLI default', effort=reasoning_effort or 'CLI default', resumed=bool(session))
                process = subprocess.Popen(command, cwd=self.workdir, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=errors,
                                           env=child_environment(), **process_options())
                try:
                    process.stdin.write(prompt.encode('utf-8'))
                    process.stdin.close()
                    for raw in process.stdout:
                        try:
                            event = json.loads(raw)
                        except (ValueError, UnicodeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        if event.get('type') == 'thread.started':
                            identity = event.get('thread_id')
                            try:
                                uuid.UUID(identity)
                            except (ValueError, TypeError, AttributeError):
                                raise CodexError('Codex returned an invalid session ID') from None
                            session = identity
                            if not verification:
                                self.sessions.write(identity, interrupted=True)
                        if event.get('type') == 'turn.completed':
                            completed = True
                        if event.get('type') == 'turn.failed':
                            failed = True
                        if event.get('type') in ('error', 'turn.failed'):
                            error = event.get('error') or event
                            if isinstance(error, dict) and any(error.get(key) is not None for key in
                                    ('message', 'code', 'status', 'status_code', 'http_status')):
                                event_error = json.dumps(error)[:16384]
                        item = event.get('item')
                        if event.get('type') == 'item.completed' and isinstance(item, dict):
                            if item.get('type') in ('file_change', 'mcp_tool_call'):
                                tools_used = True
                            if item.get('type') == 'command_execution':
                                command_text, exit_code = item.get('command'), item.get('exit_code')
                                if (len(commands) < 500 and isinstance(command_text, str)
                                        and len(command_text) <= MAX_COMMAND_EVENT_CHARS and type(exit_code) is int):
                                    commands.append({'command': command_text, 'exit_code': exit_code})
                        if (event.get('type') in ('item.started', 'item.completed') and isinstance(item, dict)
                                and item.get('type') in ('command_execution', 'file_change', 'mcp_tool_call')):
                            record_event('codex_progress', kind=item['type'], phase=event['type'],
                                         exit_code=item.get('exit_code'))
                        if (event.get('type') == 'item.completed' and isinstance(item, dict)
                                and item.get('type') == 'agent_message' and isinstance(item.get('text'), str)):
                            messages.append(item['text'])
                    code = process.wait()
                    if code or failed or not completed:
                        errors.seek(0)
                        logs = errors.read(128 * 1024).decode('utf-8', errors='replace')
                        failure = cli_failure(logs, code, event_error)
                        record_event('codex_failed', exit_code=code, reason=str(failure))
                        raise failure
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    process.stdout.close()
                    process.stdin.close()
            text = answer.read_text(encoding='utf-8').strip() if answer.is_file() else (messages[-1].strip() if messages else '')
            if not text:
                raise CodexError('Codex completed without a final answer')
            if session and not verification:
                self.sessions.write(session)
            record_event('codex_completed', characters=len(text))
            return SimpleNamespace(text=text, commands=commands, tools_used=tools_used)
