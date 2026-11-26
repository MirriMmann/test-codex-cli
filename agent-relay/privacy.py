"""Boundary checks, not a semantic classifier for arbitrary personal information."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

from generation import GenerationFailed


_PATTERNS = tuple(re.compile(pattern, re.I) for pattern in (
    r'\b\d{6,12}:[A-Za-z0-9_-]{30,}\b',  # Telegram bot token
    r'\b(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{20,}\b',
    r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b',
    r'\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*',
    r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z]+ )?PRIVATE KEY-----',
))
_SECRET_NAME = re.compile(r'token|secret|password|cookie|api[_-]?key|authorization', re.I)


class PrivacyViolation(GenerationFailed):
    pass


class PrivacyGuard:
    def __init__(self, values=()):
        # Short fixture/config values must not redact ordinary words.
        self._values = tuple(sorted({value for value in values
                                     if isinstance(value, str) and len(value) >= 12}, key=len, reverse=True))

    @classmethod
    def configured(cls, token: str, auth_file: str = ''):
        values = [token] + [value for name, value in os.environ.items() if _SECRET_NAME.search(name)]
        if auth_file:
            try:
                path = Path(auth_file)
                if path.stat().st_size <= 1024 * 1024:
                    payload = json.loads(path.read_text(encoding='utf-8-sig'))
                    def collect(value, sensitive=False):
                        if isinstance(value, dict):
                            for key, item in value.items():
                                collect(item, sensitive or bool(_SECRET_NAME.search(key)))
                        elif isinstance(value, list):
                            for item in value:
                                collect(item, sensitive)
                        elif sensitive and isinstance(value, str):
                            values.append(value)
                    collect(payload)
            except (OSError, ValueError, UnicodeError):
                # Actual authentication preflight reports a safe setup error.
                pass
        return cls(values)

    def redact(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, '[REDACTED]')
        for pattern in _PATTERNS:
            text = pattern.sub('[REDACTED]', text)
        return text

    def require_clean(self, text: str) -> str:
        decoded = ''
        try:
            decoded = json.dumps(json.loads(text), ensure_ascii=False)
        except (ValueError, TypeError):
            pass
        if self.redact(text) != text or self.redact(decoded) != decoded:
            raise PrivacyViolation('Response blocked: possible credential disclosure. No content forwarded')
        return text

    def message(self, message: dict) -> dict:
        # Copy the input: neither callers nor Telegram fixtures are mutated.
        result = copy.deepcopy(message)
        def scrub(value):
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return self.redact(value) if isinstance(value, str) else value
        return scrub(result)


class GuardedClient:
    """Check responses before parsing, persistence, correction or cross-agent use."""
    def __init__(self, client, guard: PrivacyGuard):
        self.client, self.guard = client, guard

    def send(self, prompt, **params):
        result = self.client.send(self.guard.redact(prompt), **params)
        self.guard.require_clean(str(result.text or ''))
        # Verifier evidence can also contain inline credentials in command arguments.
        commands = getattr(result, 'commands', ())
        for item in commands if isinstance(commands, (tuple, list)) else ():
            if isinstance(item, dict):
                self.guard.require_clean(str(item.get('command', '')))
        return result
