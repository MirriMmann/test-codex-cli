"""Private host publication worker, cancellable together with its Git children."""
import json
import sys

from git_publish_mcp import GitPublisher, PublishError, PublishUncertain
from publication import format_publication


def main():
    payload = json.load(sys.stdin)
    try:
        publisher = GitPublisher(payload['plan']['root'])
        publish = publisher.publish_current if payload['plan'].get('mode') == 'current' else publisher.publish_reviewed
        result = publish(payload['plan'], payload['message'])
    except PublishError as exc:
        result = {'status': 'unknown' if isinstance(exc, PublishUncertain) else 'failed',
                  'verified': False, 'error': str(exc) + ' Локальный коммит/индекс могли сохраниться; автоматического повтора нет.'}
    except Exception:
        result = {'status': 'unknown', 'verified': False,
                  'error': 'Процесс публикации остановлен с ошибкой. Проверьте Git перед новым запросом.'}
    print(json.dumps({'text': format_publication(result), 'publication': result}))


if __name__ == '__main__':
    main()
