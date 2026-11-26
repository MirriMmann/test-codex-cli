"""Persistent selected ChatGPT discussion; isolated planning stays separate."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit


class ChatError(RuntimeError):
    """Safe diagnostics; never raw responses, prompts, or credential contents."""


def normalize_conversation_id(value: str) -> str:
    """Accept one existing chat UUID or a full normal/Project chat link."""
    error = 'CWA_CONVERSATION_ID must be a ChatGPT chat UUID or https://chatgpt.com/c/<UUID> link'
    candidate = value.strip()
    if '://' in candidate:
        try:
            url = urlsplit(candidate)
            if (url.scheme != 'https' or url.netloc != 'chatgpt.com' or url.query or url.fragment
                    or not re.fullmatch(r'/(?:g/[A-Za-z0-9_-]+/)?c/[0-9a-fA-F-]{36}/?', url.path)):
                raise ValueError()
            candidate = url.path.rstrip('/').rsplit('/', 1)[-1]
        except ValueError:
            raise ChatError(error) from None
    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', candidate):
        raise ChatError(error)
    return str(uuid.UUID(candidate))


def ensure_auth_file(auth_file: str) -> None:
    """Check only the structure of the manually supplied web authorization."""
    target = Path(auth_file).expanduser()
    if not target.is_file():
        raise ChatError('ChatGPT authorization missing. Save your own auth_data.json or set CWA_AUTH_FILE')
    try:
        if target.stat().st_size > 1024 * 1024:
            raise ChatError('ChatGPT authorization JSON exceeds the safe size limit')
        payload = json.loads(target.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ChatError('ChatGPT authorization JSON cannot be read or parsed') from None
    if (not isinstance(payload, dict) or not isinstance(payload.get('accessToken'), str)
            or not payload['accessToken'].strip()):
        raise ChatError('ChatGPT authorization JSON must contain an accessToken')
    cookies = payload.get('cookies')
    has_cookie = isinstance(cookies, dict) and any(
        isinstance(name, str) and name.startswith('__Secure-next-auth.session-token')
        and isinstance(value, str) and bool(value.strip())
        for name, value in cookies.items()
    )
    has_session = isinstance(payload.get('sessionToken'), str) and bool(payload['sessionToken'].strip())
    if not has_cookie and not has_session:
        raise ChatError('ChatGPT authorization JSON must contain a sessionToken or ChatGPT session cookie')


def check_chat_setup(auth_file: str) -> None:
    """Offline preflight for an enabled web backend; never starts a web session."""
    ensure_auth_file(auth_file)
    try:
        found = importlib.util.find_spec('chatgpt_web_adapter') is not None
        version = importlib.metadata.version('chatgpt-web-adapter') if found else ''
    except (ImportError, ValueError, importlib.metadata.PackageNotFoundError):
        version = ''
    match = re.fullmatch(r'0\.4\.(\d+)', version)
    if not match or int(match.group(1)) < 1:
        raise ChatError('ChatGPT requires chatgpt-web-adapter>=0.4.1,<0.5; install requirements-chat.txt in .venv')
    node = shutil.which('node')
    if not node:
        raise ChatError('ChatGPT requires Node.js 24+ on PATH')
    try:
        result = subprocess.run(
            [node, '--version'], capture_output=True, text=True, check=False, timeout=10,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ChatError('Cannot run Node.js; ChatGPT requires Node.js 24+ on PATH') from None
    match = re.fullmatch(r'v(\d+)\.\d+\.\d+(?:[-+].+)?', result.stdout.strip())
    if result.returncode or not match or int(match.group(1)) < 24:
        raise ChatError('ChatGPT requires Node.js 24+ on PATH')
    if not shutil.which('curl.exe' if os.name == 'nt' else 'curl'):
        raise ChatError('ChatGPT requires curl on PATH (curl.exe on Windows)')


class ChatClient:
    def __init__(self, *, auth_file: str, conversation_id: str):
        self.auth_file = str(Path(auth_file).expanduser().resolve())
        self.conversation_id = normalize_conversation_id(conversation_id)
        self._client = None

    def send(self, prompt: str, *, model: str = '', reasoning_effort: str = '', isolated: bool = False):
        # The adapter and credentials are never loaded on the Codex code path.
        try:
            from relay_cwa import RelayHTTPClient, describe_error
        except ImportError:
            raise ChatError('ChatGPT adapter unavailable; install requirements-chat.txt in .venv') from None
        try:
            if self._client is None:
                ensure_auth_file(self.auth_file)
                self._client = RelayHTTPClient(auth_file=self.auth_file)
            # Normal discussion continues exactly the selected chat. Planning
            # uses a fresh context and cannot forward its discussion history.
            params = {'conversation': None if isolated else self.conversation_id,
                      'temporary': isolated}
            if model:
                params['model'] = model
            if reasoning_effort:
                params['reasoning_effort'] = reasoning_effort
            return self._client.send(prompt, **params)
        except ChatError:
            raise
        except Exception as exc:
            raise ChatError('ChatGPT request failed: ' + describe_error(exc)) from None
