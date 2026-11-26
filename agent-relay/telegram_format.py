"""A bounded Markdown subset rendered as Telegram text/entities, without parse_mode.

Raw HTML and incomplete Markdown remain literal text. Offsets are computed only
after rendering and splitting, in Telegram's UTF-16 units. No network/dependencies.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


FORMAT_INSTRUCTIONS = (
    'Format user-visible answers for Telegram using ordinary Markdown: short paragraphs, '
    'brief **bold headings**, bullet/numbered lists, `inline code` for commands and paths, '
    'and fenced code blocks with a language for multiline code. Use blank lines between sections. '
    'Avoid tables, raw HTML, complex nested formatting and MarkdownV2 escaping. '
    'Keep structured routing/work-report JSON contracts unchanged; these instructions apply '
    'only to conversational answer text and human-readable summaries. '
)

_INLINE = re.compile(
    r'(?P<escape>\\[\\`*_{}\[\]()#+.!|~>\-])'
    r'|(?P<code>``[^`\n]+``|`[^`\n]+`)'
    r'|(?P<link>\[[^\]\n]+\]\([^\s()]+(?:\([^\s()]*\)[^\s()]*)*\))'
    r'|(?P<triple>\*\*\*(?=\S).+?(?<=\S)\*\*\*|(?<!\w)___(?=\S).+?(?<=\S)___(?!\w))'
    r'|(?P<bold>\*\*(?=\S).+?(?<=\S)\*\*|(?<!\w)__(?=\S).+?(?<=\S)__(?!\w))'
    r'|(?P<strikethrough>~~(?=\S).+?(?<=\S)~~)'
    r'|(?P<spoiler>\|\|(?=\S).+?(?<=\S)\|\|)'
    r'|(?P<italic>(?<![\w*])\*(?![\s*]).+?(?<!\s)\*(?!\*)|(?<!\w)_(?![\s_]).+?(?<!\s)_(?!\w))'
)
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})([^\n]*)$')


def markdown_code(value: str) -> str:
    """Quote host-generated paths/commands so their punctuation stays literal."""
    if '\n' not in value and '`' not in value:
        return '`' + value + '`'
    width = max([3] + [len(run) + 1 for run in re.findall(r'`+', value)])
    fence = '`' * width
    return '\n' + fence + 'text\n' + value + ('' if value.endswith('\n') else '\n') + fence + '\n'


class _Text:
    def __init__(self):
        self.parts = []
        self.spans = []
        self.length = 0

    def add(self, text, spans=()):
        for span in spans:
            self.spans.append({**span, 'start': self.length + span['start'], 'end': self.length + span['end']})
        self.parts.append(text)
        self.length += len(text)

    def mark(self, kind, start, end, **extra):
        if end > start:
            self.spans.append({'type': kind, 'start': start, 'end': end, **extra})

    def result(self):
        return ''.join(self.parts), self.spans


def _inline(text: str, depth: int = 0):
    if depth >= 8:
        return text, []
    out, previous = _Text(), 0
    for match in _INLINE.finditer(text):
        out.add(text[previous:match.start()])
        value, kind = match.group(), match.lastgroup
        start = out.length
        if kind == 'escape':
            out.add(value[1:])
        elif kind == 'code':
            width = 2 if value.startswith('``') else 1
            out.add(value[width:-width])
            out.mark('code', start, out.length)
        elif kind == 'link':
            label, url = value[1:].split('](', 1)
            url = url[:-1]
            try:
                parsed = urlsplit(url)
                safe = (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                        and parsed.username is None and parsed.password is None
                        and (parsed.port is None or 1 <= parsed.port <= 65535) and len(url) <= 2048)
            except ValueError:
                safe = False
            if safe:
                out.add(*_inline(label, depth + 1))
                # Telegram links cannot contain code or other links.
                out.spans = [span for span in out.spans
                             if span['start'] < start or span['type'] not in ('code', 'text_link')]
                out.mark('text_link', start, out.length, url=url)
            else:
                out.add(value)
        else:
            width = 3 if kind == 'triple' else 1 if kind == 'italic' else 2
            out.add(*_inline(value[width:-width], depth + 1))
            for style in ('bold', 'italic') if kind == 'triple' else (kind,):
                out.mark(style, start, out.length)
        previous = match.end()
    out.add(text[previous:])
    return out.result()


def render(markdown: str):
    out = _Text()
    lines = markdown.replace('\r\n', '\n').splitlines(keepends=True)
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.fullmatch(line.rstrip('\n'))
        if fence:
            marker, info = fence.groups()
            end = index + 1
            closing = re.compile(r'^ {0,3}' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}\s*$')
            while end < len(lines) and not closing.fullmatch(lines[end].rstrip('\n')):
                end += 1
            # An unfinished fence is displayed literally, without swallowing text.
            if end < len(lines):
                code = ''.join(lines[index + 1:end])
                start = out.length
                out.add(code)
                language = info.strip().split()[0] if info.strip() else ''
                extra = {'language': language} if re.fullmatch(r'[\w+-]{1,32}', language) else {}
                out.mark('pre', start, out.length, **extra)
                if lines[end].endswith('\n') and code and not code.endswith('\n'):
                    out.add('\n')
                index = end + 1
                continue
            out.add(''.join(lines[index:]))
            break
        heading = re.match(r'^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?(\n?)$', line)
        if heading:
            start = out.length
            out.add(*_inline(heading[1]))
            out.mark('bold', start, out.length)
            out.add(heading[2])
        else:
            line = re.sub(r'^(\s*)[-+*]\s+', r'\1• ', line)
            line = re.sub(r'^(\s*)>\s?', r'\1│ ', line)
            out.add(*_inline(line))
        index += 1
    text, spans = out.result()
    if not text.strip() and markdown.strip():
        return markdown, []
    # Telegram forbids any styling inside pre/code. Split enclosing emphasis
    # around code rather than sending intersecting, invalid entities.
    protected = [span for span in spans if span['type'] in ('code', 'pre')]
    normalized = []
    for span in spans:
        ranges = [(span['start'], span['end'])]
        if span['type'] not in ('code', 'pre'):
            for code in protected:
                ranges = [(a, b) for start, end in ranges
                          for a, b in ((start, min(end, code['start'])), (max(start, code['end']), end)) if a < b]
        normalized.extend({**span, 'start': start, 'end': end} for start, end in ranges if text[start:end].strip())
    # Nested headings/emphasis can produce identical bold ranges.
    unique = {tuple(sorted(span.items())): span for span in normalized}
    return text, list(unique.values())


def _units(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def format_messages(markdown: str, *, prefix: str = '', limit: int = 3900) -> list[dict]:
    text, spans = render(markdown)
    prefix_units = _units(prefix)
    budget = limit - prefix_units
    if budget < 2:
        raise ValueError('Telegram message prefix exceeds the chunk limit')
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1] + (2 if ord(char) > 0xFFFF else 1))
    result, start = [], 0
    while start < len(text):
        end, used = start, 0
        while end < len(text) and used + (2 if ord(text[end]) > 0xFFFF else 1) <= budget:
            used += 2 if ord(text[end]) > 0xFFFF else 1
            end += 1
        if end < len(text):
            boundary = text.rfind('\n', start, end)
            if boundary > start + (end - start) // 2:
                end = boundary + 1
        chunk = text[start:end]
        entities = []
        prefix_offset = 0
        for line in prefix.splitlines(keepends=True):
            if line.strip():
                entities.append({'type': 'bold', 'offset': prefix_offset, 'length': _units(line.rstrip('\n'))})
            prefix_offset += _units(line)
        for span in spans:
            left, right = max(start, span['start']), min(end, span['end'])
            if left < right and text[left:right].strip():
                entities.append({key: value for key, value in span.items() if key not in ('start', 'end')} | {
                    'offset': prefix_units + offsets[left] - offsets[start], 'length': offsets[right] - offsets[left]})
        entities = list({tuple(sorted(item.items())): item for item in entities}.values())
        entities.sort(key=lambda item: (item['offset'], -item['length']))
        payload = {'text': prefix + chunk}
        if entities:
            payload['entities'] = entities[:100]
        result.append(payload)
        start = end
    return result
