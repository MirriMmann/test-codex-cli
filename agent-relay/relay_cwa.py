"""Small compatibility layer for CWA 0.4.x and skewed local clocks."""
from __future__ import annotations

import math
import re
import time
from email.utils import parsedate_to_datetime

from chatgpt_web_adapter import BrowserlessHTTPClient


FINALIZE_URL = 'https://chatgpt.com/backend-api/sentinel/chat-requirements/finalize'


class RelayHTTPClient(BrowserlessHTTPClient):
    """Validate Sentinel expiry relative to its HTTPS response's server clock.

    CWA compares expire_at with time.time(). A fast workstation clock makes a
    fresh token look expired. Translate only that timestamp; CWA still checks
    every required field, both expiry limits and its existing safety margin.
    """

    _finalize_clock: tuple[float, float] | None = None

    def _run_curl(self, method, url, headers, body=None, **kwargs):
        is_finalize = method.upper() == 'POST' and url == FINALIZE_URL
        if is_finalize:
            self._finalize_clock = None
        result = super()._run_curl(method, url, headers, body, **kwargs)
        if is_finalize:
            dates = re.findall(r'^date:[ \t]*([^\r\n]+)', result[2], re.MULTILINE | re.IGNORECASE)
            if dates:
                try:
                    server_date = parsedate_to_datetime(dates[-1])
                    if server_date.tzinfo is not None:
                        self._finalize_clock = (server_date.timestamp(), time.time())
                except (ValueError, TypeError, OverflowError):
                    pass  # Missing/invalid Date keeps the original strict checks.
        return result

    def _json_request(self, method, url, payload, headers):
        status, data = super()._json_request(method, url, payload, headers)
        if method.upper() == 'POST' and url == FINALIZE_URL and self._finalize_clock:
            expiry = data.get('expire_at') if isinstance(data, dict) else None
            if isinstance(expiry, (int, float)) and not isinstance(expiry, bool) and math.isfinite(expiry):
                server_time, local_time = self._finalize_clock
                # HTTP Date has second precision. Subtract a second so rounding
                # cannot extend the lifetime of a nearly expired credential.
                data = {**data, 'expire_at': expiry + local_time - server_time - 1}
        return status, data


def describe_error(exc: Exception) -> str:
    """Report useful structure, never raw bodies, URLs, prompts or credentials."""
    fields = [type(exc).__name__]
    status = getattr(exc, 'status_code', None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        fields.append(f'HTTP {status}')
    stage = getattr(exc, 'request_stage', None)
    if stage in {
        'transport', 'transport_timeout', 'web_session_bootstrap', 'session_bootstrap',
        'conversation_fetch', 'conversation_prepare', 'conversation_stream',
        'sentinel_prepare', 'sentinel_finalize', 'sentinel_bundle_provider',
        'turnstile_gate', 'chat_requirements',
    }:
        fields.append(f'stage={stage}')
    message = str(exc)
    if message.startswith('SENTINEL_FINALIZE_CONTRACT_DRIFT:'):
        fields.append('Sentinel expiry/response validation failed; check system clock and adapter compatibility')
    elif message == 'browserless public protocol download failed':
        fields.append('public protocol download failed; check network access to chatgpt.com')
    elif message in {
        'browserless protocol module boundary changed',
        'browserless protocol changed: unreviewed module hash',
    }:
        fields.append('public protocol changed; adapter update required')
    elif message == 'browserless JS runtime failed; check Node version and protocol hash':
        fields.append('JS runtime failed; Node.js 24+ and a compatible protocol module are required')
    elif message == 'HTTP bootstrap did not establish device context':
        fields.append('session bootstrap did not establish device context')
    elif status == 401:
        fields.append('session rejected; update your local auth_data.json')
    elif status == 403:
        fields.append('access denied by ChatGPT; check the signed-in session')
    return '; '.join(fields)
