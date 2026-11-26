"""Local task metadata and per-person/topic preferences; never replay work."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


ACTIVE = ('queued', 'running', 'checking')
MAX_CORRECTIONS = 2
TRANSITIONS = {
    'queued': {'running', 'cancelled', 'failed', 'interrupted'},
    'running': {'checking', 'completed', 'failed', 'cancelled', 'interrupted'},
    'checking': {'running', 'completed', 'failed', 'cancelled', 'interrupted'},
}
STATUS_LABELS = {
    'queued': 'В очереди', 'running': 'Выполняется', 'checking': 'Проверяется',
    'completed': 'Завершена', 'failed': 'Ошибка', 'cancelled': 'Отменена',
    'interrupted': 'Прервана после остановки или перезапуска',
}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def correction_limit(record: dict) -> int:
    value = record.get('max_corrections', MAX_CORRECTIONS)
    return min(MAX_CORRECTIONS, max(0, value)) if type(value) is int else 0


class TaskStore:
    def __init__(self, filename: str):
        self.path = Path(filename)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise ValueError('Unsupported task database version')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS modes (
                    chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL, backend TEXT NOT NULL,
                    PRIMARY KEY (chat_id, topic_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    topic_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
                    owner_is_bot INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    repository TEXT NOT NULL, goal TEXT NOT NULL,
                    constraints_json TEXT NOT NULL DEFAULT '[]',
                    criteria_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_corrections INTEGER NOT NULL DEFAULT 2,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    verification TEXT NOT NULL DEFAULT 'not_run',
                    changes_json TEXT NOT NULL DEFAULT '[]',
                    checks_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE (chat_id, update_id)
                );
                CREATE INDEX IF NOT EXISTS tasks_owner
                    ON tasks(chat_id, topic_id, owner_id, id);
                CREATE TABLE IF NOT EXISTS task_attempts (
                    task_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
                    report_json TEXT NOT NULL DEFAULT '',
                    verification_json TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, attempt)
                );
                CREATE TABLE IF NOT EXISTS publications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
                    update_id INTEGER NOT NULL UNIQUE, plan_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
            for name in ('stage', 'plan_json', 'report_json', 'verification_json', 'stop_reason',
                         'baseline_json', 'manifest_json', 'final_report', 'publication_status', 'publication_json',
                         'publish_request_json'):
                if name not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            # Preserve the last available report from pre-history databases.
            db.execute("INSERT OR IGNORE INTO task_attempts "
                       "(task_id,attempt,report_json,verification_json,started_at,updated_at) "
                       "SELECT id,attempts,report_json,verification_json,created_at,updated_at FROM tasks "
                       "WHERE attempts>0 AND (report_json<>'' OR verification_json<>'')")
            db.execute('PRAGMA user_version = 6')

    @contextmanager
    def connection(self):
        with self._lock, closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def recover(self) -> None:
        """An interrupted relay must not execute previously accepted work again."""
        with self.connection() as db:
            db.execute("UPDATE tasks SET status='interrupted', updated_at=? "
                       "WHERE status IN ('queued','running','checking')", (timestamp(),))
            for state, replacement in (('queued', 'cancelled'), ('running', 'unknown')):
                db.execute('UPDATE tasks SET publication_status=? WHERE id IN '
                           '(SELECT task_id FROM publications WHERE status=?)', (replacement, state))
                db.execute('UPDATE publications SET status=?,updated_at=? WHERE status=?',
                           (replacement, timestamp(), state))

    def mode(self, chat_id: int, topic_id: int, user_id: int, *, default: str) -> str:
        with self.connection() as db:
            row = db.execute('SELECT backend FROM modes WHERE chat_id=? AND topic_id=? AND user_id=?',
                             (chat_id, topic_id, user_id)).fetchone()
        return row['backend'] if row else default

    def set_mode(self, chat_id: int, topic_id: int, user_id: int, backend: str) -> None:
        if backend not in ('chat', 'codex'):
            raise ValueError('Unknown mode')
        with self.connection() as db:
            db.execute('INSERT INTO modes VALUES(?,?,?,?) '
                       'ON CONFLICT(chat_id,topic_id,user_id) DO UPDATE SET backend=excluded.backend',
                       (chat_id, topic_id, user_id, backend))

    def create(self, *, update_id: int, chat_id: int, topic_id: int, owner_id: int,
               owner_is_bot: bool, message_id: int, repository: str, goal: str,
               constraints: tuple[str, ...] = (), criteria: tuple[str, ...] = ()) -> int | None:
        now = timestamp()
        with self.connection() as db:
            cursor = db.execute('''INSERT INTO tasks
                (update_id,chat_id,topic_id,owner_id,owner_is_bot,message_id,
                 repository,goal,constraints_json,criteria_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id,update_id) DO NOTHING''',
                (update_id, chat_id, topic_id, owner_id, int(owner_is_bot), message_id,
                 repository, goal, json.dumps(constraints, ensure_ascii=False),
                 json.dumps(criteria, ensure_ascii=False), now, now))
            return cursor.lastrowid if cursor.rowcount else None

    def get(self, task_id: int) -> dict | None:
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        return dict(row) if row else None

    def owned(self, chat_id: int, topic_id: int, owner_id: int, *, active: bool = False) -> list[dict]:
        sql = 'SELECT * FROM tasks WHERE chat_id=? AND topic_id=? AND owner_id=? AND owner_is_bot=0'
        if active:
            sql += " AND status IN ('queued','running','checking')"
        sql += ' ORDER BY id DESC'
        with self.connection() as db:
            rows = db.execute(sql, (chat_id, topic_id, owner_id)).fetchall()
        return [dict(row) for row in rows]

    def set_status(self, task_id: int, status: str, *, error_code: str = '', count_attempt: bool = True) -> bool:
        with self.connection() as db:
            row = db.execute('SELECT status,cancel_requested FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or status not in TRANSITIONS.get(row['status'], set()):
                return False
            if row['cancel_requested'] and status not in ('cancelled', 'interrupted'):
                return False
            db.execute('UPDATE tasks SET status=?, attempts=attempts+?, error_code=?, updated_at=? WHERE id=?',
                       (status, int(status == 'running' and count_attempt), error_code, timestamp(), task_id))
            return True

    def request_cancel(self, task_id: int, chat_id: int, topic_id: int, owner_id: int) -> str | None:
        with self.connection() as db:
            row = db.execute('SELECT status FROM tasks WHERE id=? AND chat_id=? AND topic_id=? '
                             'AND owner_id=? AND owner_is_bot=0',
                             (task_id, chat_id, topic_id, owner_id)).fetchone()
            if not row or row['status'] not in ACTIVE:
                return None
            status = 'cancelled' if row['status'] == 'queued' else row['status']
            db.execute('UPDATE tasks SET cancel_requested=1,status=?,updated_at=? WHERE id=?',
                       (status, timestamp(), task_id))
            return status

    def mark_cancelling(self, task_id: int) -> None:
        """Internal emergency stop for runtime tasks, including peer turns."""
        with self.connection() as db:
            db.execute("UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=? "
                       "AND status IN ('queued','running','checking')", (timestamp(), task_id))

    def set_stage(self, task_id: int, stage: str) -> bool:
        with self.connection() as db:
            return bool(db.execute("UPDATE tasks SET stage=?,updated_at=? WHERE id=? "
                                   "AND status IN ('running','checking') AND cancel_requested=0",
                                   (stage, timestamp(), task_id)).rowcount)

    def begin_execution(self, task_id: int) -> bool:
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['cancel_requested']:
                return False
            attempts = row['attempts']
            if ((attempts == 0 and row['status'] != 'running')
                    or (attempts > 0 and (row['status'] != 'checking' or row['verification'] != 'failed'))
                    or attempts >= 1 + correction_limit(dict(row))):
                return False
            now = timestamp()
            changed = db.execute("UPDATE tasks SET stage=?,status='running',attempts=attempts+1,"
                                 "verification='not_run',verification_json='',report_json='',"
                                 "changes_json='[]',checks_json='[]',stop_reason='',updated_at=? "
                                 "WHERE id=? AND status=? AND attempts=? AND cancel_requested=0",
                                 ('executing' if attempts == 0 else 'correcting', now,
                                  task_id, row['status'], attempts)).rowcount
            if changed:
                db.execute('INSERT INTO task_attempts(task_id,attempt,started_at,updated_at) VALUES(?,?,?,?)',
                           (task_id, attempts + 1, now, now))
            return bool(changed)

    def attempt_history(self, task_id: int) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt', (task_id,))]

    def _attempt_report(self, db, task_id: int, field: str, value: str) -> None:
        # Only called with host-owned field names; model output is a bound value.
        db.execute(f"INSERT INTO task_attempts(task_id,attempt,{field},started_at,updated_at) "
                   f"SELECT id,attempts,?,updated_at,updated_at FROM tasks WHERE id=? AND attempts>0 "
                   f"ON CONFLICT(task_id,attempt) DO UPDATE SET {field}=excluded.{field},updated_at=excluded.updated_at",
                   (value, task_id))

    def finish_cycle(self, task_id: int, reason: str, *, error_code: str = '') -> bool:
        if reason not in ('passed', 'corrections_exhausted', 'verification_incomplete',
                          'verification_unavailable', 'verification_rejected', 'executor_error', 'executor_blocked'):
            raise ValueError('Unknown cycle stop reason')
        with self.connection() as db:
            changed = db.execute("UPDATE tasks SET stop_reason=?,updated_at=? WHERE id=? "
                                 "AND status IN ('running','checking') AND cancel_requested=0",
                                 (reason, timestamp(), task_id)).rowcount
            if changed and error_code:
                self._attempt_report(db, task_id, 'error_code', error_code)
            return bool(changed)

    def save_plan(self, task_id: int, plan: dict) -> bool:
        with self.connection() as db:
            return bool(db.execute("UPDATE tasks SET plan_json=?,constraints_json=?,criteria_json=?,updated_at=? "
                                   "WHERE id=? AND status='running' AND cancel_requested=0",
                                   (json.dumps(plan, ensure_ascii=False),
                                    json.dumps(plan['constraints'], ensure_ascii=False),
                                    json.dumps(plan['acceptance_criteria'], ensure_ascii=False),
                                    timestamp(), task_id)).rowcount)

    def save_report(self, task_id: int, report: dict) -> bool:
        with self.connection() as db:
            encoded = json.dumps(report, ensure_ascii=False)
            changed = db.execute("UPDATE tasks SET report_json=?,changes_json=?,checks_json=?,"
                                   "verification='unverified',stage='result_received',updated_at=? "
                                   "WHERE id=? AND status='running' AND cancel_requested=0",
                                   (encoded,
                                    json.dumps(report['changes'], ensure_ascii=False),
                                    json.dumps(report['checks'], ensure_ascii=False), timestamp(), task_id)).rowcount
            if changed:
                self._attempt_report(db, task_id, 'report_json', encoded)
            return bool(changed)

    def save_verification(self, task_id: int, result: dict) -> bool:
        if result['status'] not in ('passed', 'failed', 'partial', 'not_run'):
            raise ValueError('Unknown verification status')
        with self.connection() as db:
            encoded = json.dumps(result, ensure_ascii=False)
            changed = db.execute("UPDATE tasks SET verification_json=?,verification=?,"
                                   "stage='verification_finished',updated_at=? "
                                   "WHERE id=? AND status='checking' AND cancel_requested=0",
                                   (encoded, result['status'], timestamp(), task_id)).rowcount
            if changed:
                self._attempt_report(db, task_id, 'verification_json', encoded)
            return bool(changed)

    def save_baseline(self, task_id: int, value: dict | None) -> bool:
        with self.connection() as db:
            return bool(db.execute("UPDATE tasks SET baseline_json=? WHERE id=? AND attempts=0 "
                                   "AND status='running' AND cancel_requested=0",
                                   (json.dumps(value, ensure_ascii=False), task_id)).rowcount)

    def save_manifest(self, task_id: int, value: dict) -> bool:
        with self.connection() as db:
            return bool(db.execute("UPDATE tasks SET manifest_json=? WHERE id=? AND status='checking' "
                                   "AND verification='passed' AND cancel_requested=0",
                                   (json.dumps(value, ensure_ascii=False), task_id)).rowcount)

    def save_final_report(self, task_id: int, text: str) -> None:
        with self.connection() as db:
            db.execute('UPDATE tasks SET final_report=? WHERE id=?', (text, task_id))

    def publication(self, identifier: int) -> dict | None:
        with self.connection() as db:
            row = db.execute('SELECT * FROM publications WHERE id=?', (identifier,)).fetchone()
        return dict(row) if row else None

    def request_publication(self, task_id: int, scope: tuple, update_id: int, text: str, *, mode: str = 'reviewed') -> bool:
        """Host calls only after authenticating an explicit current human request."""
        with self.connection() as db:
            return bool(db.execute("UPDATE tasks SET publish_request_json=? WHERE id=? AND chat_id=? "
                                   "AND topic_id=? AND owner_id=? AND owner_is_bot=0 AND cancel_requested=0 "
                                   "AND status IN ('queued','running','checking')",
                                   (json.dumps({'update_id': update_id, 'text': text, 'mode': mode}), task_id, *scope)).rowcount)

    def create_current_publication(self, task_id: int, scope: tuple, update_id: int, plan: dict) -> int:
        """Host-only explicit publication of current Git state, independent of model reports."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=? AND chat_id=? AND topic_id=? '
                             'AND owner_id=? AND owner_is_bot=0', (task_id, *scope)).fetchone()
            if (not row or row['status'] in ACTIVE or plan.get('mode') != 'current'
                    or plan.get('root') != row['repository']):
                raise ValueError('Публикация недоступна: неверный владелец, репозиторий или задача ещё выполняется.')
            if row['publication_status'] in ('queued', 'running'):
                raise ValueError('Публикация уже запрошена. Используйте /status.')
            now = timestamp()
            cursor = db.execute('INSERT INTO publications(task_id,update_id,plan_json,created_at,updated_at) VALUES(?,?,?,?,?)',
                                (task_id, update_id, json.dumps(plan), now, now))
            db.execute("UPDATE tasks SET publication_status='queued',publication_json='' WHERE id=?", (task_id,))
            return cursor.lastrowid

    def prepare_recheck(self, task_id: int, scope: tuple, update_id: int, text: str) -> bool:
        """Explicit human follow-up can verify saved work; never rerun development."""
        with self.connection() as db:
            return bool(db.execute("UPDATE tasks SET status='queued',stage='recheck_queued',"
                "publish_request_json=?,updated_at=? WHERE id=? AND chat_id=? AND topic_id=? AND owner_id=? "
                "AND owner_is_bot=0 AND status='failed' AND cancel_requested=0 AND report_json<>'' "
                "AND plan_json<>'' AND baseline_json NOT IN ('','null') "
                "AND publication_status NOT IN ('queued','running','published','unknown')",
                (json.dumps({'update_id': update_id, 'text': text}), timestamp(), task_id, *scope)).rowcount)

    def create_publication(self, task_id: int, scope: tuple, update_id: int, branch: str) -> int:
        with self.connection() as db:
            # Serialize requests even if two local connections race.
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=? AND chat_id=? AND topic_id=? '
                             'AND owner_id=? AND owner_is_bot=0', (task_id, *scope)).fetchone()
            if not row or row['status'] != 'completed' or row['verification'] != 'passed':
                raise ValueError('Публикация доступна только для вашей успешно проверенной задачи в этой теме.')
            if row['publication_status'] in ('queued', 'running', 'published'):
                raise ValueError('Публикация этой задачи уже запрошена или подтверждена. Используйте /status.')
            manifest = json.loads(row['manifest_json'] or '{}')
            if not manifest.get('eligible'):
                raise ValueError(manifest.get('reason', 'У задачи нет проверенного состава изменений для публикации. Нужна новая задача.'))
            if manifest['branch'] != branch:
                raise ValueError('Указанная ветка отличается от проверенной ветки задачи. Публикация заблокирована.')
            now = timestamp()
            cursor = db.execute('INSERT INTO publications(task_id,update_id,plan_json,created_at,updated_at) VALUES(?,?,?,?,?)',
                                (task_id, update_id, row['manifest_json'], now, now))
            db.execute("UPDATE tasks SET publication_status='queued',publication_json='' WHERE id=?", (task_id,))
            return cursor.lastrowid

    def start_publication(self, identifier: int) -> bool:
        with self.connection() as db:
            changed = db.execute("UPDATE publications SET status='running',updated_at=? WHERE id=? "
                                 "AND status='queued' AND cancel_requested=0", (timestamp(), identifier)).rowcount
            if changed:
                db.execute("UPDATE tasks SET publication_status='running' WHERE id=(SELECT task_id FROM publications WHERE id=?)",
                           (identifier,))
            return bool(changed)

    def finish_publication(self, identifier: int, result: dict) -> None:
        if result['status'] not in ('published', 'failed', 'unknown', 'cancelled'):
            raise ValueError('Unknown publication result')
        with self.connection() as db:
            row = db.execute('SELECT * FROM publications WHERE id=?', (identifier,)).fetchone()
            if not row or row['status'] not in ('queued', 'running'):
                return
            encoded = json.dumps(result, ensure_ascii=False)
            db.execute('UPDATE publications SET status=?,result_json=?,updated_at=? WHERE id=?',
                       (result['status'], encoded, timestamp(), identifier))
            db.execute('UPDATE tasks SET publication_status=?,publication_json=? WHERE id=?',
                       (result['status'], encoded, row['task_id']))

    def cancel_publication(self, task_id: int) -> bool:
        with self.connection() as db:
            rows = db.execute("SELECT id,status FROM publications WHERE task_id=? AND status IN ('queued','running')", (task_id,)).fetchall()
            for row in rows:
                db.execute('UPDATE publications SET cancel_requested=1 WHERE id=?', (row['id'],))
                if row['status'] == 'queued':
                    encoded = json.dumps({'status': 'cancelled', 'error': 'Публикация отменена до запуска.'}, ensure_ascii=False)
                    db.execute("UPDATE publications SET status='cancelled',result_json=?,updated_at=? WHERE id=?",
                               (encoded, timestamp(), row['id']))
                    db.execute("UPDATE tasks SET publication_status='cancelled',publication_json=? WHERE id=?", (encoded, task_id))
            return bool(rows)


def task_status(record: dict) -> str:
    text = f"Задача #{record['id']} · {STATUS_LABELS[record['status']]}"
    if record['cancel_requested'] and record['status'] in ACTIVE:
        text += ' · отмена запрошена'
    text += f"\nЗапусков Codex: {record['attempts']}."
    text += f" Исправлений: {max(0, record['attempts'] - 1)} из {correction_limit(record)}."
    stage = record.get('stage', '')
    if stage:
        labels = {'planning': 'Постановка задания', 'executing': 'Работа Codex',
                  'result_received': 'Отчёт исполнителя получен', 'planning_failed': 'Ошибка постановки задания',
                  'executing_failed': 'Ошибка выполнения', 'report_parsing_failed': 'Некорректный отчёт исполнителя',
                  'verifying': 'Независимая проверка', 'verifying_failed': 'Ошибка независимой проверки',
                  'correcting': 'Исправление по замечаниям', 'correcting_failed': 'Ошибка исправления',
                  'verification_finished': 'Независимая проверка завершена'}
        text += '\nЭтап: ' + labels.get(stage, 'Обработка задания') + '.'
    if record['verification'] == 'not_run':
        text += '\nПроверки не подтверждены оркестратором.'
    elif record['verification'] == 'unverified':
        text += '\nПроверки не подтверждены оркестратором; сохранён отчёт Codex.'
    else:
        from task_protocol import VERIFICATION_LABELS
        text += '\n' + VERIFICATION_LABELS.get(record['verification'], 'Проверки не подтверждены') + '.'
    if record.get('stop_reason') == 'corrections_exhausted':
        text += '\nЛимит исправлений исчерпан; остались нерешённые замечания.'
    elif record.get('stop_reason') == 'executor_error':
        text += '\nПоследний запуск остановлен с ошибкой; автоматического повтора нет.'
    elif record.get('stop_reason') == 'executor_blocked':
        text += '\nCodex сообщил о блокировке; автоисправления остановлены. Причина — в /report.'
    publication = record.get('publication_status', '')
    if publication:
        labels = {'queued': 'в очереди', 'running': 'выполняется', 'published': 'подтверждена',
                  'failed': 'ошибка, успех не подтверждён', 'unknown': 'результат неизвестен, успех не подтверждён',
                  'cancelled': 'отменена до запуска'}
        text += '\nПубликация: ' + labels[publication] + '.'
        if publication == 'published':
            result = json.loads(record.get('publication_json') or '{}')
            if result.get('verified') is True:
                text += f"\nВетка: {result['branch']}\nКоммит: {result['commit']}"
    return text
