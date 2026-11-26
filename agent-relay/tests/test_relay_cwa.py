import json
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch

from chatgpt_web_adapter import BrowserlessHTTPClient
from chatgpt_web_adapter.exceptions import RequestError
from chatgpt_web_adapter.sentinel_transaction import _validate_finalize_response
from relay_cwa import FINALIZE_URL, RelayHTTPClient, describe_error


class ClockTests(unittest.TestCase):
    def request(self, offset=0, lifetime=540, date=True, url=FINALIZE_URL):
        client = object.__new__(RelayHTTPClient)
        now = time.time()
        server_time = int(now - offset)
        payload = {'persona': 'test', 'token': 'private-token',
                   'expire_after': 540, 'expire_at': server_time + lifetime}
        headers = 'HTTP/1.1 200 OK\r\n'
        if date:
            stamp = format_datetime(datetime.fromtimestamp(server_time, timezone.utc), usegmt=True)
            headers += 'Date: ' + stamp + '\r\n'
        response = (200, json.dumps(payload).encode(), headers)
        with patch.object(BrowserlessHTTPClient, '_run_curl', return_value=response) as curl:
            status, data = client._json_request('POST', url, {}, {})
        curl.assert_called_once()
        return now, status, data, payload

    def test_fresh_token_with_clock_ahead_or_behind(self):
        for offset in (0, 10800, -10800):
            with self.subTest(offset=offset):
                now, status, data, original = self.request(offset)
                token, deadline = _validate_finalize_response(
                    status, data, acquired_monotonic=100, acquired_wallclock=now)
                self.assertEqual(token, original['token'])
                self.assertGreater(deadline, 630)
                self.assertLessEqual(deadline, 635)

    def test_expired_token_still_rejected(self):
        now, status, data, _ = self.request(offset=-10800, lifetime=-60)
        with self.assertRaisesRegex(RequestError, 'expiry is too small'):
            _validate_finalize_response(status, data, acquired_monotonic=100, acquired_wallclock=now)

    def test_missing_date_keeps_original_validation(self):
        now, status, data, original = self.request(offset=10800, date=False)
        self.assertEqual(data, original)
        with self.assertRaises(RequestError):
            _validate_finalize_response(status, data, acquired_monotonic=100, acquired_wallclock=now)

    def test_other_endpoints_are_unchanged(self):
        _, _, data, original = self.request(offset=10800, url='https://chatgpt.com/other')
        self.assertEqual(data, original)

    def test_required_fields_still_validated(self):
        now, status, data, _ = self.request(offset=10800)
        del data['token']
        with self.assertRaises(RequestError):
            _validate_finalize_response(status, data, acquired_monotonic=100, acquired_wallclock=now)

    def test_error_diagnostics_do_not_expose_credentials(self):
        error = RequestError('conversation status=401: secret-body',
                             endpoint='https://example.test/private-token',
                             body_preview='secret-body')
        message = describe_error(error)
        self.assertIn('HTTP 401', message)
        self.assertIn('stage=conversation_fetch', message)
        self.assertNotIn('secret-body', message)
        self.assertNotIn('private-token', message)


if __name__ == '__main__':
    unittest.main()
