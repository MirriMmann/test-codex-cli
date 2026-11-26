"""Local AgentRelay setup: Python standard library and official Codex CLI."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path

from codex_client import check_login, resolve_codex_binary, validate_workspace
from generation import terminate_worker

ROOT = Path(__file__).resolve().parent
VENV = ROOT / '.venv'


def supported_python(version: tuple[int, int]) -> bool:
    return (3, 10) <= version <= (3, 15)


def validate_python() -> None:
    if not supported_python(sys.version_info[:2]):
        raise RuntimeError('Python 3.10–3.15 required')


def venv_python() -> Path:
    return VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def setup_venv() -> Path:
    target = venv_python()
    if not target.exists():
        print('[AgentRelay] Creating isolated .venv ...', flush=True)
        venv.EnvBuilder(with_pip=False).create(VENV)
    command = [str(target), '-c', "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"]
    check = subprocess.run(command, capture_output=True, text=True, check=False)
    if check.returncode:
        print('[AgentRelay] Repairing .venv with the current Python ...', flush=True)
        venv.EnvBuilder(with_pip=False).create(VENV)
        check = subprocess.run(command, capture_output=True, text=True, check=False)
    if check.returncode:
        raise RuntimeError('Virtual environment repair failed; check the Python installation')
    if not supported_python(tuple(map(int, check.stdout.strip().split('.')))):
        raise RuntimeError('Existing .venv uses unsupported Python')
    return target


def read_env_values(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if re.fullmatch(r'[A-Z][A-Z0-9_]*', key.strip()):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def validate_local_config() -> dict[str, str]:
    path = ROOT / '.env'
    if not path.is_file():
        shutil.copyfile(ROOT / '.env.example', path)
        raise RuntimeError('Created .env from template; fill the Telegram settings and CODEX_WORKDIR')
    values = read_env_values(path)
    required = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'ALLOWED_USER_IDS', 'CODEX_WORKDIR')
    missing = [key for key in required if not (os.getenv(key) or values.get(key))
               or 'REPLACE' in (os.getenv(key) or values.get(key, '')).upper()]
    if missing:
        raise RuntimeError('Configure these .env fields: ' + ', '.join(missing))
    workspace = Path(os.getenv('CODEX_WORKDIR') or values['CODEX_WORKDIR']).expanduser()
    validate_workspace(workspace if workspace.is_absolute() else ROOT / workspace)
    return values


def setup_chat_backend(python: Path, local: dict[str, str]) -> None:
    def value(key: str, default: str = '') -> str:
        return os.getenv(key, local.get(key, default)).strip()

    if not (value('CWA_CONVERSATION_ID') or value('CHAT_ALIASES') or value('DEFAULT_BACKEND') == 'chat'):
        return
    check = subprocess.run([
        str(python), '-c',
        "from importlib.metadata import version; import re,sys; "
        "m=re.fullmatch(r'0\\.4\\.(\\d+)',version('chatgpt-web-adapter')); "
        "sys.exit(not(m and int(m.group(1))>=1))",
    ], capture_output=True)
    if check.returncode:
        pip = subprocess.run([str(python), '-m', 'pip', '--version'], capture_output=True)
        if pip.returncode:
            subprocess.run([str(python), '-m', 'ensurepip', '--upgrade'], check=True)
        print('[AgentRelay] Installing the optional ChatGPT adapter ...', flush=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements-chat.txt')], check=True)
    auth = Path(value('CWA_AUTH_FILE', 'auth_data.json')).expanduser()
    if not auth.is_absolute():
        auth = ROOT / auth
    script = ('import sys\nfrom chat_client import check_chat_setup, ChatError\n'
              'try:\n check_chat_setup(sys.argv[1])\n'
              'except ChatError as exc:\n print(str(exc)); sys.exit(1)\n')
    checked = subprocess.run([str(python), '-c', script, str(auth)], cwd=ROOT, capture_output=True, text=True)
    if checked.returncode:
        raise RuntimeError(checked.stdout.strip() or 'ChatGPT backend setup failed; check requirements-chat.txt and CWA_AUTH_FILE')
    print('[AgentRelay] ChatGPT chat backend ready.', flush=True)


def run_relay(python: Path, env: dict[str, str]) -> int:
    process = subprocess.Popen([str(python), str(ROOT / 'agent_relay.py')], cwd=ROOT, env=env)
    try:
        return process.wait()
    except KeyboardInterrupt:
        # The console sends Ctrl+C to the relay too. Let it cancel its worker
        # and release its instance lock before the launcher exits.
        try:
            process.wait(timeout=12)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            if os.name == 'nt':
                terminate_worker(process)
            else:
                process.kill()
                process.wait(timeout=5)
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Set up and run the Telegram-to-Codex CLI relay')
    parser.add_argument('--setup-only', action='store_true', help='Check Python and Codex without starting Telegram')
    parser.add_argument('--no-node-download', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        validate_python()
        python = setup_venv()
        local = read_env_values(ROOT / '.env') if (ROOT / '.env').is_file() else {}
        binary = resolve_codex_binary(os.getenv('CODEX_BIN') or local.get('CODEX_BIN', ''))
        print('[AgentRelay] Python and Codex CLI ready.', flush=True)
        if args.setup_only:
            return 0
        validate_local_config()
        check_login(binary)
        setup_chat_backend(python, local)
        env = os.environ.copy()
        env['CODEX_BIN'] = binary
        print('[AgentRelay] Starting Telegram relay (Ctrl+C to stop)...', flush=True)
        return run_relay(python, env)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'[AgentRelay] Setup stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
