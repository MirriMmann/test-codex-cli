import unittest
from concurrent.futures import Future
from types import SimpleNamespace

from telegram_format import format_messages, markdown_code
import test_routing as routing_tests
from agent_relay import GenerationTask


def units(value):
    return len(value.encode('utf-16-le')) // 2


class FormattingTests(unittest.TestCase):
    def assert_valid(self, payload):
        self.assertLessEqual(units(payload['text']), 3900)
        boundaries = {units(payload['text'][:i]) for i in range(len(payload['text']) + 1)}
        for entity in payload.get('entities', []):
            self.assertGreater(entity['length'], 0)
            self.assertIn(entity['offset'], boundaries)
            self.assertIn(entity['offset'] + entity['length'], boundaries)
        for index, first in enumerate(payload.get('entities', [])):
            for second in payload['entities'][index + 1:]:
                a, b = first['offset'], first['offset'] + first['length']
                c, d = second['offset'], second['offset'] + second['length']
                if max(a, c) < min(b, d):
                    self.assertTrue((a <= c and d <= b) or (c <= a and b <= d))
                    self.assertNotIn(first['type'], ('pre', 'code'))
                    self.assertNotIn(second['type'], ('pre', 'code'))

    def test_headings_lists_inline_styles_and_ordinary_special_characters(self):
        source = '# Итог\n\n- **Готово**: _курсив_, ~~старое~~, ||секрет||.\n1. `snake_case`\n2. a_b_c + 2 * 3 < 10 & [x]'
        parts = format_messages(source)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['text'], 'Итог\n\n• Готово: курсив, старое, секрет.\n1. snake_case\n2. a_b_c + 2 * 3 < 10 & [x]')
        self.assertTrue({'bold', 'italic', 'strikethrough', 'spoiler', 'code'} <= {x['type'] for x in parts[0]['entities']})
        self.assert_valid(parts[0])

    def test_nested_emphasis_is_valid_and_never_overlaps_code(self):
        part = format_messages('**Жирный _курсив_ и `x_y`**')[0]
        self.assertEqual(part['text'], 'Жирный курсив и x_y')
        self.assert_valid(part)

    def test_invalid_link_port_is_displayed_without_sending_link_entity(self):
        source = '[x](https://example.com:invalid/path)'
        part = format_messages(source)[0]
        self.assertEqual(part['text'], source)
        self.assertNotIn('entities', part)

    def test_emoji_offsets_and_prefix_are_utf16(self):
        part = format_messages('🚀 **да**', prefix='ChatGPT\n\n')[0]
        bold = next(x for x in part['entities'] if x['offset'] > 0)
        self.assertEqual(bold, {'type': 'bold', 'offset': units('ChatGPT\n\n🚀 '), 'length': 2})
        self.assert_valid(part)

    def test_links_are_entities_and_unsafe_protocol_is_literal(self):
        part = format_messages('[репозиторий](https://github.com/a/b) и [x](javascript:alert(1))')[0]
        self.assertEqual(part['text'], 'репозиторий и [x](javascript:alert(1))')
        self.assertEqual([x['url'] for x in part['entities'] if x['type'] == 'text_link'], ['https://github.com/a/b'])
        self.assert_valid(part)

    def test_fenced_code_preserves_every_character_and_language_across_chunks(self):
        code = ('print("🚀 _ * <b> &")\n' * 500)
        source = '```python\n' + code + '```'
        prefix = 'codex\n\n'
        parts = format_messages(source, prefix=prefix)
        self.assertGreater(len(parts), 2)
        self.assertEqual(''.join(p['text'][len(prefix):] for p in parts), code)
        for part in parts:
            self.assert_valid(part)
            pre = next(x for x in part['entities'] if x['type'] == 'pre')
            self.assertEqual(pre['language'], 'python')

    def test_broken_markdown_html_and_escapes_remain_safe_text(self):
        for source in ('**unclosed', '```python\nx = 1', '```python\n# comment\n**literal**', '<b>raw & text</b>', '```\n```'):
            with self.subTest(source=source):
                part = format_messages(source)[0]
                self.assertEqual(part['text'], source)
                self.assertNotIn('parse_mode', part)
                self.assert_valid(part)
        self.assertEqual(format_messages(r'\*literal\*')[0]['text'], '*literal*')

    def test_host_paths_and_commands_are_literal_even_with_markdown(self):
        for command in ('folder_name/test.py', 'print("**literal**")', 'echo `literal`\n```\nnext'):
            with self.subTest(command=command):
                part = format_messages(markdown_code(command))[0]
                self.assertEqual(part['text'].strip('\n'), command)
                self.assertTrue(any(x['type'] in ('code', 'pre') for x in part['entities']))

    def test_long_link_and_bold_cross_message_boundary_safely(self):
        source = '**' + '🚀' * 5000 + '**'
        parts = format_messages(source, prefix='ChatGPT\n\n')
        self.assertEqual(''.join(p['text'][9:] for p in parts), '🚀' * 5000)
        for part in parts:
            self.assert_valid(part)


class FormattingIntegrationTests(unittest.TestCase):
    setUp = routing_tests.AliasRoutingTests.setUp
    api_call = routing_tests.AliasRoutingTests.api_call
    new_app = routing_tests.AliasRoutingTests.new_app
    event = routing_tests.AliasRoutingTests.event

    def test_chat_generation_sends_rendered_text_with_entities(self):
        self.chat.send.return_value = SimpleNamespace(text='## Готово\n\n**Ответ** и `x_y`')
        self.app.process_update(self.event('чувак, ответь'))
        payloads = [call.args[1] for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']
        part = payloads[-1]
        self.assertEqual(part['text'], 'ChatGPT\n\nГотово\n\nОтвет и x_y')
        self.assertTrue(part['entities'])
        self.assertNotIn('parse_mode', part)

    def test_long_control_report_is_split_with_legal_entities_and_same_topic(self):
        message = self.event('/report')['message']
        message['message_thread_id'] = 77
        self.app._reply_control(message, 'codex\n\n**Отчёт**\n```text\n' + 'x' * 9000 + '\n```', backend='codex')
        payloads = [call.args[1] for call in self.api.call.call_args_list if call.args[0] == 'sendMessage']
        self.assertGreater(len(payloads), 2)
        for part in payloads:
            self.assertEqual(part['message_thread_id'], 77)
            self.assertTrue(part['text'].startswith('codex\n\n'))
            self.assertLessEqual(units(part['text']), 3900)

    def test_cancel_still_discards_unsent_formatted_parts(self):
        task = GenerationTask(99, self.event('чувак')['message'], '', {}, backend='chat')
        task.future = Future()
        task.future.set_result(SimpleNamespace(text='**' + 'x' * 9000 + '**'))
        self.assertFalse(self.app._finish_generation(task, one_part=True))
        before = len(self.api.call.call_args_list)
        task.cancelled.set()
        self.assertTrue(self.app._finish_generation(task, one_part=True))
        self.assertEqual(len(self.api.call.call_args_list), before)
