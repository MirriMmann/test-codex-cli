import copy
import json
import unittest

from task_protocol import (MAX_COMMAND_CHARS, OrchestrationError, REPORT_SCHEMA,
                           VERIFICATION_SCHEMA, parse_report, parse_verification)


class ReportLimitTests(unittest.TestCase):
    def report(self, command):
        return {'status': 'completed', 'summary': 'Checked', 'changes': [], 'remaining': [],
                'checks': [{'command': command, 'status': 'passed', 'exit_code': 0, 'summary': 'Passed'}]}

    def test_multiline_test_longer_than_old_limit_is_preserved(self):
        command = "@'\nconst assert = require('assert/strict');\n" + '// regression fixture\n' * 100 + "assert.equal(2+2,4);\n'@ | node"
        self.assertGreater(len(command), 1913)
        report = self.report(command)
        self.assertEqual(parse_report(json.dumps(report)), report)
        review = {'summary': 'Checked', 'issues': [], 'checks': report['checks'],
                  'criteria': [{'index': i, 'status': 'passed', 'summary': 'Confirmed'} for i in range(7)]}
        self.assertEqual(parse_verification(json.dumps(review), 7)['checks'][0]['command'], command)

    def test_limit_is_shared_by_schema_executor_and_verifier(self):
        checks = REPORT_SCHEMA['properties']['checks']
        self.assertEqual(checks['items']['properties']['command']['maxLength'], MAX_COMMAND_CHARS)
        self.assertEqual(VERIFICATION_SCHEMA['properties']['checks'], checks)
        self.assertEqual(len(parse_report(json.dumps(self.report('x' * MAX_COMMAND_CHARS)))['checks'][0]['command']),
                         MAX_COMMAND_CHARS)

    def test_oversize_diagnostic_names_field_and_lengths_without_echoing_payload(self):
        private_payload = 'PRIVATE-CONTENT-MUST-NOT-BE-IN-DIAGNOSTICS-' + 'x' * MAX_COMMAND_CHARS
        with self.assertRaises(OrchestrationError) as caught:
            parse_report(json.dumps(self.report(private_payload)))
        reason = caught.exception.validation_reason
        self.assertIn('checks[0].command', reason)
        self.assertIn(str(MAX_COMMAND_CHARS), reason)
        self.assertNotIn('PRIVATE-CONTENT', reason)
        self.assertNotIn('PRIVATE-CONTENT', str(caught.exception))

    def test_invalid_review_preserves_safe_reason_and_still_rejects_missing_criteria(self):
        review = {'summary': 'Checked', 'issues': [], 'checks': self.report('x' * (MAX_COMMAND_CHARS + 1))['checks'],
                  'criteria': [{'index': 0, 'status': 'passed', 'summary': 'Confirmed'}]}
        with self.assertRaises(OrchestrationError) as caught:
            parse_verification(json.dumps(review), 1)
        self.assertIn('checks[0].command', caught.exception.validation_reason)
        review['checks'] = []
        with self.assertRaises(OrchestrationError) as caught:
            parse_verification(json.dumps(review), 7)
        self.assertIn('criteria', caught.exception.validation_reason)

    def test_long_command_does_not_relax_exit_code_or_field_validation(self):
        report = self.report('x' * 1913)
        for update in ({'exit_code': 1}, {'status': 'not_run', 'exit_code': 0}, {'allow_publish': True}):
            invalid = copy.deepcopy(report)
            invalid['checks'][0].update(update)
            with self.subTest(update=update), self.assertRaises(OrchestrationError):
                parse_report(json.dumps(invalid))

if __name__ == '__main__':
    unittest.main()
