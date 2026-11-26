"""Host-owned manifests and explicit publication; no model authorization."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from git_publish_mcp import GitPublisher, PublishError
import verification


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf-8')).hexdigest()


def source_digest(snapshot: dict) -> str:
    return digest({path: value for path, value in snapshot['files'].items() if value != 'deleted'})


def baseline(repository: str, cancelled) -> dict | None:
    snapshot = verification.repository_snapshot(repository, cancelled)
    if snapshot is None:
        return None
    try:
        publisher = GitPublisher(repository)
        branch, remote = publisher.destination()
        head = publisher.head()
        dirty = (publisher.names('diff', '--name-only', '--no-renames', '-z')
                 | publisher.names('diff', '--cached', '--name-only', '--no-renames', '-z')
                 | publisher.names('ls-files', '--others', '--exclude-standard', '-z'))
        return {'snapshot': snapshot, 'dirty': sorted(dirty & snapshot['files'].keys()),
                'branch': branch, 'remote': remote, 'head': head}
    except PublishError:
        return None


def seal(repository: str, before: dict | None, report: dict, review: dict, cancelled) -> dict:
    def blocked(reason):
        return {'eligible': False, 'reason': reason}
    if not before:
        return blocked('Нет исходного снимка репозитория и origin. Нужна новая задача с настроенным Git.')
    after = verification.repository_snapshot(repository, cancelled)
    if after is None or digest(after) != review.get('snapshot'):
        return blocked('Файлы изменились после проверки или снимок недоступен. Нужна новая проверка.')
    try:
        publisher = GitPublisher(repository)
        if publisher.destination() != (before['branch'], before['remote']) or publisher.head() != before['head']:
            return blocked('Ветка, origin или HEAD изменились во время задачи. Публикация заблокирована.')
        previous = before['snapshot']['files']
        files = after['files']
        changed = {path for path in previous.keys() | files.keys() if previous.get(path) != files.get(path)}
        reported = {item['path'] for item in report['changes']}
        if not changed or changed != reported:
            return blocked('Состав изменений не совпадает с отчётом либо изменений нет. Публикация заблокирована.')
        if changed & set(before['dirty']):
            return blocked('В изменённых файлах были правки до начала задачи. Их нельзя автоматически включить в коммит.')
        paths = sorted(publisher.safe_path(path) for path in changed)
        return {'eligible': True, 'root': str(Path(repository).resolve()), 'branch': before['branch'],
                'remote': before['remote'], 'head': before['head'], 'paths': paths,
                'snapshot': digest(after), 'sources': source_digest(after)}
    except PublishError:
        return blocked('Не удалось подтвердить репозиторий, origin или состав изменений.')


def format_publication(result: dict) -> str:
    if result.get('status') == 'published' and result.get('verified') is True:
        text = (f"Публикация подтверждена GitHub.\nВетка: {result['branch']}\n"
                f"Коммит: {result['commit']}\nРепозиторий: {result['remote']}")
        if result.get('already_published'):
            text += '\nТекущий коммит уже опубликован; новый коммит не создавался.'
        if result.get('technical_checks_only'):
            text += '\nПроверены файлы и состояние Git. Функциональные тесты в этой операции не запускались.'
        return text
    return ('Публикация не подтверждена.\n' + result.get('error',
            'Проверьте локальный Git и целевую ветку перед новым запросом.'))
