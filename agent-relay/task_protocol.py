"""Bounded data contracts between the planner, executor and relay."""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath, PureWindowsPath

from generation import GenerationFailed


MAX_COMMAND_CHARS = 8000
# Rendering argv may expand quotes/backslashes; evidence must retain the whole command.
MAX_COMMAND_EVENT_CHARS = 4 * MAX_COMMAND_CHARS


class OrchestrationError(GenerationFailed):
    def __init__(self, message: str, *, validation_reason: str = ''):
        super().__init__(message)
        self.validation_reason = validation_reason


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _json(text: str) -> dict:
    if not isinstance(text, str) or len(text) > 128_000:
        raise ValueError('invalid response size')
    raw = text.strip()
    # Accept only a complete known wrapper, never extract JSON from prose.
    for _ in range(3):
        wrapped = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```', raw, re.S | re.I)
        escaped = re.fullmatch(r'<escape>(.*?)</escape>', raw, re.S)
        codeblock = re.fullmatch(
            r'<codeblock(?:\s+(?:(?:language|lang)\s*=\s*(?:"json"|\'json\'|json)'
            r'|id\s*=\s*(?:"[A-Za-z0-9_-]{1,80}"|\'[A-Za-z0-9_-]{1,80}\')))*\s*>'
            r'(.*?)</codeblock>', raw, re.S | re.I)
        if not (wrapped or escaped or codeblock):
            break
        raw = (wrapped or escaped or codeblock).group(1).strip()
    result = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(result, dict):
        raise ValueError('object required')
    return result


def _text(value, limit: int, *, empty: bool = False) -> str:
    if isinstance(value, str) and len(value) > limit:
        raise ValueError(f'text length {len(value)} exceeds {limit}')
    if not isinstance(value, str) or '\x00' in value:
        raise ValueError('invalid text')
    value = value.strip()
    if not empty and not value:
        raise ValueError('empty text')
    return value


def _texts(value, count: int, limit: int, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > count or (required and not value):
        raise ValueError('invalid list')
    return [_text(item, limit) for item in value]


def parse_plan(text: str) -> dict:
    try:
        data = _json(text)
        if set(data) != {'goal', 'constraints', 'acceptance_criteria'}:
            raise ValueError('unexpected plan fields')
        return {'goal': _text(data['goal'], 4000),
                'constraints': _texts(data['constraints'], 20, 500),
                'acceptance_criteria': _texts(data['acceptance_criteria'], 20, 500, required=True)}
    except (ValueError, TypeError, RecursionError):
        raise OrchestrationError('Planning failed: ChatGPT returned an invalid task description. Codex was not started') from None


def relative_path(value) -> str:
    value = _text(value, 1000).replace('\\', '/')
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive or ':' in value
            or '..' in path.parts or value in ('.', '')):
        raise ValueError('invalid repository-relative path')
    for part in path.parts:
        name = part.casefold()
        if (name in ('.git', '.codex', '.ssh', '.aws', 'auth_data.json', 'credentials.json')
                or (name.startswith('.env') and name != '.env.example')
                or name.endswith(('.pem', '.key', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.sqlite3-journal'))):
            raise ValueError('private path')
    return path.as_posix()


def parse_report(text: str) -> dict:
    field = 'report'
    try:
        data = _json(text)
        if set(data) != {'status', 'summary', 'changes', 'checks', 'remaining'}:
            raise ValueError('unexpected report fields')
        if data['status'] not in ('completed', 'blocked', 'failed'):
            raise ValueError('invalid status')
        field = 'summary'
        summary = _text(data['summary'], 4000)
        field = 'remaining'
        remaining = _texts(data['remaining'], 30, 1000)
        field = 'changes'
        if not isinstance(data['changes'], list) or len(data['changes']) > 200:
            raise ValueError('invalid changes')
        changes = []
        for index, change in enumerate(data['changes']):
            field = f'changes[{index}]'
            if not isinstance(change, dict) or set(change) != {'path', 'summary'}:
                raise ValueError('invalid change')
            changes.append({'path': relative_path(change['path']), 'summary': _text(change['summary'], 500)})
        field = 'checks'
        if not isinstance(data['checks'], list) or len(data['checks']) > 100:
            raise ValueError('invalid checks')
        checks = []
        for index, check in enumerate(data['checks']):
            field = f'checks[{index}]'
            if not isinstance(check, dict) or set(check) != {'command', 'status', 'exit_code', 'summary'}:
                raise ValueError('invalid check')
            status, code = check['status'], check['exit_code']
            if status not in ('passed', 'failed', 'not_run'):
                raise ValueError('invalid check status')
            if code is not None and (type(code) is not int or not -(2**31) <= code < 2**32):
                raise ValueError('invalid exit code')
            if (status == 'passed' and code != 0) or (status == 'failed' and code == 0):
                raise ValueError('contradictory check result')
            if status == 'not_run' and code is not None:
                raise ValueError('unexecuted check has an exit code')
            field = f'checks[{index}].command'
            command = _text(check['command'], MAX_COMMAND_CHARS)
            field = f'checks[{index}].summary'
            check_summary = _text(check['summary'], 500)
            checks.append({'command': command, 'status': status, 'exit_code': code, 'summary': check_summary})
        return {'status': data['status'], 'summary': summary, 'changes': changes,
                'checks': checks, 'remaining': remaining}
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        # Diagnostics contain only host field names and validation metadata.
        reason = f'{field}: ' + (exc.msg if isinstance(exc, json.JSONDecodeError)
                                 else str(exc) if type(exc) is ValueError else type(exc).__name__)
        raise OrchestrationError('Codex returned an invalid structured report (' + reason + '). '
                                 'Changes may remain; no automatic retry was made', validation_reason=reason) from None


REPORT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['status', 'summary', 'changes', 'checks', 'remaining'],
    'properties': {
        'status': {'type': 'string', 'enum': ['completed', 'blocked', 'failed']},
        'summary': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'changes': {'type': 'array', 'maxItems': 200, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['path', 'summary'],
            'properties': {'path': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
                           'summary': {'type': 'string', 'minLength': 1, 'maxLength': 500}},
        }},
        'checks': {'type': 'array', 'maxItems': 100, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['command', 'status', 'exit_code', 'summary'],
            'properties': {'command': {'type': 'string', 'minLength': 1, 'maxLength': MAX_COMMAND_CHARS},
                           'status': {'type': 'string', 'enum': ['passed', 'failed', 'not_run']},
                           'exit_code': {'type': ['integer', 'null'], 'minimum': -(2**31), 'maximum': 2**32 - 1},
                           'summary': {'type': 'string', 'minLength': 1, 'maxLength': 500}},
        }},
        'remaining': {'type': 'array', 'maxItems': 30,
                      'items': {'type': 'string', 'minLength': 1, 'maxLength': 1000}},
    },
}


VERIFICATION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['summary', 'criteria', 'checks', 'issues'],
    'properties': {
        'summary': REPORT_SCHEMA['properties']['summary'],
        'criteria': {'type': 'array', 'maxItems': 20, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['index', 'status', 'summary'],
            'properties': {'index': {'type': 'integer', 'minimum': 0, 'maximum': 19},
                           'status': {'type': 'string', 'enum': ['passed', 'failed', 'not_checked']},
                           'summary': {'type': 'string', 'minLength': 1, 'maxLength': 500}},
        }},
        'checks': REPORT_SCHEMA['properties']['checks'],
        'issues': REPORT_SCHEMA['properties']['remaining'],
    },
}


def parse_verification(text: str, criterion_count: int) -> dict:
    field = 'verification'
    try:
        data = _json(text)
        if set(data) != {'summary', 'criteria', 'checks', 'issues'}:
            raise ValueError('unexpected verification fields')
        # Reuse the strict check/exit-code contract, not the executor verdict.
        checked = parse_report(json.dumps({'status': 'completed', 'summary': data['summary'],
                                          'changes': [], 'checks': data['checks'], 'remaining': data['issues']}))
        field = 'criteria'
        criteria = data['criteria']
        if not isinstance(criteria, list) or len(criteria) != criterion_count:
            raise ValueError('incomplete criteria')
        seen, normalized = set(), []
        for item in criteria:
            if not isinstance(item, dict) or set(item) != {'index', 'status', 'summary'}:
                raise ValueError('invalid criterion')
            index = item['index']
            if type(index) is not int or not 0 <= index < criterion_count or index in seen:
                raise ValueError('invalid criterion index')
            if item['status'] not in ('passed', 'failed', 'not_checked'):
                raise ValueError('invalid criterion status')
            seen.add(index)
            normalized.append({'index': index, 'status': item['status'], 'summary': _text(item['summary'], 500)})
        return {'summary': checked['summary'], 'criteria': sorted(normalized, key=lambda item: item['index']),
                'checks': checked['checks'], 'issues': checked['remaining']}
    except (ValueError, TypeError, KeyError, RecursionError, OrchestrationError) as exc:
        reason = (exc.validation_reason if isinstance(exc, OrchestrationError) else
                  f'{field}: ' + (exc.msg if isinstance(exc, json.JSONDecodeError)
                                  else str(exc) if type(exc) is ValueError else type(exc).__name__))
        raise OrchestrationError('Independent verification returned an invalid report (' + reason + '); '
                                 'result is not verified', validation_reason=reason) from None


VERIFICATION_LABELS = {
    'passed': 'Проверка пройдена', 'failed': 'Проверка выявила ошибки',
    'partial': 'Проверка неполная', 'not_run': 'Проверка не выполнена',
    'unverified': 'Проверки не подтверждены',
}


def format_verification(result: dict, *, compact: bool = False) -> str:
    from telegram_format import markdown_code
    shorten = lambda value: value[:240] if compact else value
    lines = ['**' + VERIFICATION_LABELS[result['status']] + '**', result['summary'][:1000] if compact else result['summary']]
    labels = {'passed': 'пройдена', 'failed': 'ошибка', 'not_run': 'не запускалась'}
    checks = sorted(result['checks'], key=lambda item: item['status'] == 'passed') if compact else result['checks']
    for check in checks[:8] if compact else checks:
        evidence = 'запуск подтверждён CLI' if check.get('observed') else 'запуск не подтверждён'
        code = check.get('observed_exit_code')
        if code is not None:
            evidence += f', фактический код {code}'
        lines.append(f"• {markdown_code(shorten(check['command']))} — проверяющий: {labels[check['status']]} ({evidence}): {shorten(check['summary'])}")
    criteria = sorted(result['criteria'], key=lambda item: item['status'] == 'passed') if compact else result['criteria']
    for item in criteria[:5] if compact else criteria:
        state = {'passed': 'соответствует', 'failed': 'не соответствует', 'not_checked': 'не проверен'}[item['status']]
        lines.append(f"• Критерий {item['index'] + 1}: {state} — {shorten(item['summary'])}")
    lines.extend('• ' + shorten(item) for item in (result['issues'][:8] if compact else result['issues']))
    if compact and (len(checks) > 8 or len(criteria) > 5 or len(result['issues']) > 8):
        lines.append('Показаны основные пункты; полные результаты сохранены в локальном реестре задачи.')
    return '\n'.join(lines)


def format_report(report: dict, *, reviewed: bool = False, compact: bool = False) -> str:
    from telegram_format import markdown_code
    shorten = lambda value: value[:240] if compact else value
    lines = [report['summary'][:1000] if compact else report['summary']]
    lines.append('\n**Изменения (со слов Codex):**')
    lines.extend(f"• {markdown_code(shorten(item['path']))}: {shorten(item['summary'])}" for item in (report['changes'][:10] if compact else report['changes']))
    if compact and len(report['changes']) > 10:
        lines.append(f"Ещё файлов: {len(report['changes']) - 10}; полный список сохранён в реестре задачи.")
    if not report['changes']:
        lines.append('• Изменения не заявлены.')
    lines.append('\n**Проверки (со слов Codex, без независимого подтверждения):**')
    labels = {'passed': 'пройдена', 'failed': 'ошибка', 'not_run': 'не запускалась'}
    for check in report['checks'][:8] if compact else report['checks']:
        code = f", код {check['exit_code']}" if check['exit_code'] is not None else ''
        lines.append(f"• {markdown_code(shorten(check['command']))} — {labels[check['status']]}{code}: {shorten(check['summary'])}")
    if compact and len(report['checks']) > 8:
        lines.append(f"Ещё проверок исполнителя: {len(report['checks']) - 8}.")
    if not report['checks']:
        lines.append('• Исполнитель не указал проверок.')
    lines.append('\n**Осталось:**')
    lines.extend('• ' + shorten(item) for item in (report['remaining'][:8] if compact else report['remaining']))
    if compact and len(report['remaining']) > 8:
        lines.append('Остальные нерешённые пункты сохранены в реестре задачи.')
    if not report['remaining']:
        lines.append('• Исполнитель не указал оставшихся работ.' if reviewed else
                     '• Исполнитель не указал оставшихся работ; критерии ещё не проверены оркестратором.')
    return '\n'.join(lines)
