"""Telegram project metadata controls. No model or worker is started here."""
from __future__ import annotations

from project_store import DEFAULT_LIMITS, ProjectError


LABELS = {'draft': 'Черновик', 'ready': 'Готов к следующему раунду',
          'planning': 'Планирование', 'agreed': 'План согласован', 'executing': 'Разработка',
          'reviewing': 'Ревью', 'integrating': 'Интеграция', 'awaiting_input': 'Нужно решение человека',
          'paused': 'Приостановлен', 'limited': 'Исчерпан лимит', 'completed': 'Завершён',
          'failed': 'Ошибка', 'cancelled': 'Отменён'}
HELP = ('Управление проектами — часть 1, хранилище и лимиты.\n'
        '/project new <цель>\n/project status [ID]\n'
        '/project spec ID [текст ТЗ]\n/project plan ID [текст плана]\n'
        '/project events ID\n/project limits ID [ключ=число ...]\n'
        '/project member ID <Telegram user ID>\n'
        '/project pause ID\n/project resume ID\n/project cancel ID\n'
        'Автоматическое обсуждение и запуск исполнителей будут подключены в следующих частях.')


def status_text(project):
    rows = [f'Проект #{project["id"]} · {LABELS[project["state"]]}', project['goal'],
            f'ТЗ: v{project["documents"].get("spec", 0)}. План: v{project["documents"].get("plan", 0)}.',
            f'Раундов: {len(project["rounds"])}.']
    rows += [f'{key}: {project["usage"][key]} / {project["limits"][key]}' for key in DEFAULT_LIMITS]
    if project['reason']:
        rows.append('Причина остановки: ' + project['reason'])
    return '\n'.join(rows)


def handle_project(relay, message, body):
    store, scope = relay.projects, relay._scope(message)
    parts = body.split(maxsplit=1)
    action = parts[0].lower() if parts else 'help'
    rest = parts[1] if len(parts) > 1 else ''
    try:
        if action == 'help':
            answer = HELP
        elif action == 'new':
            if relay.paused:
                raise ProjectError('Бот приостановлен. Сначала /resume.')
            identifier = store.create(scope, relay.config.codex_workdir, rest,
                                      request_key='telegram:' + str(message['message_id']))
            answer = (f'Проект #{identifier} создан как черновик.\n'
                      f'Сохраните ТЗ: /project spec {identifier} <текст>\n'
                      f'Сохраните план: /project plan {identifier} <текст>\n'
                      'Это сохранение проекта; исполнители пока не запускаются.')
        else:
            args = rest.split(maxsplit=1)
            raw_id = args[0].removeprefix('#') if args else ''
            identifier = int(raw_id) if raw_id.isascii() and raw_id.isdecimal() and len(raw_id) <= 12 else None
            if identifier is None and action == 'status' and not rest:
                identifier = store.latest(scope)
            if identifier is None:
                raise ProjectError('Укажите ID проекта. Справка: /project help')
            content = args[1] if len(args) > 1 else ''
            if action == 'status':
                answer = status_text(store.get(identifier, scope))
            elif action in ('spec', 'plan'):
                document = store.document(identifier, scope, action)
                if content:
                    version = store.save_document(identifier, scope, action, content,
                                                  expected_version=document['version'] if document else 0)
                    saved = store.document(identifier, scope, action, version)
                    answer = f'Проект #{identifier}: {action} v{version} сохранён.\nSHA-256: {saved["sha256"]}'
                else:
                    answer = (f'Проект #{identifier}: {action} v{document["version"]}\n\n' + document['content']
                              if document else 'Документ ещё не сохранён.')
            elif action == 'events':
                events = store.events(identifier, scope)
                answer = f'Проект #{identifier} · Журнал\n' + '\n'.join(f'#{e["id"]} {e["kind"]}: {e["payload_json"]}' for e in events)
            elif action == 'limits':
                if content:
                    changes = {}
                    for item in content.split():
                        name, separator, value = item.partition('=')
                        if not separator or not value.isascii() or not value.isdecimal() or len(value) > 8 or name in changes:
                            raise ProjectError('Формат лимитов: agent_calls=40 rounds=10 elapsed_seconds=86400')
                        changes[name] = int(value)
                    store.set_limits(identifier, scope, changes)
                answer = status_text(store.get(identifier, scope))
            elif action == 'member':
                if not content.isascii() or not content.isdecimal() or len(content) > 18 or int(content) not in relay.config.allowed_user_ids:
                    raise ProjectError('Участник должен быть человеком из ALLOWED_USER_IDS.')
                store.add_member(identifier, scope, int(content))
                answer = f'Участник {content} добавлен в проект #{identifier} в этой теме.'
            elif action in ('pause', 'resume', 'cancel'):
                if action == 'resume' and relay.paused:
                    raise ProjectError('Бот приостановлен. Сначала /resume.')
                store.control(identifier, scope, action)
                answer = status_text(store.get(identifier, scope))
            else:
                answer = HELP
    except ProjectError as exc:
        answer = str(exc)
    relay._reply_control(message, answer)
