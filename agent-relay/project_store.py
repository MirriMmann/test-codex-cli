"""Durable project documents, rounds, budgets and events; no agent execution."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

from privacy import PrivacyGuard


DEFAULT_LIMITS = {'rounds': 10, 'agent_calls': 40, 'corrections': 20,
                  'discussion_turns': 30, 'elapsed_seconds': 86400}
LIMIT_CAPS = {'rounds': 100, 'agent_calls': 1000, 'corrections': 200,
              'discussion_turns': 500, 'elapsed_seconds': 604800}
TERMINAL = {'completed', 'failed', 'cancelled'}
HALTED = TERMINAL | {'paused', 'awaiting_input', 'limited'}
ROUND_TRANSITIONS = {'planning': {'agreed', 'failed'}, 'agreed': {'executing', 'failed'},
                     'executing': {'reviewing', 'failed'},
                     'reviewing': {'executing', 'integrating', 'failed'},
                     'integrating': {'reviewing', 'completed', 'failed'}}


class ProjectError(ValueError):
    """Safe project-control diagnostic."""


class BudgetExceeded(ProjectError):
    pass


def text(value: str, limit: int = 40000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ProjectError('Текст отсутствует или превышает допустимую длину.')
    return value.replace('\r\n', '\n').strip()


def limits(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_LIMITS):
        raise ProjectError('Нужно указать все известные лимиты проекта.')
    for key, amount in value.items():
        if type(amount) is not int or not 1 <= amount <= LIMIT_CAPS[key]:
            raise ProjectError(f'Лимит {key}: целое число от 1 до {LIMIT_CAPS[key]}.')
    return dict(value)


class ProjectStore:
    def __init__(self, filename: str, *, privacy=None, clock=time.time):
        self.path = Path(filename)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.privacy = privacy or PrivacyGuard()
        self.clock = clock
        with self.connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] not in (0, 1):
                raise ProjectError('Unsupported project database version')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL, request_key TEXT NOT NULL, goal TEXT NOT NULL,
                    repository TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'draft',
                    previous_state TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                    limits_json TEXT NOT NULL, activated_at REAL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, UNIQUE(chat_id,request_key)
                );
                CREATE TABLE IF NOT EXISTS project_members (
                    project_id INTEGER NOT NULL REFERENCES projects(id), user_id INTEGER NOT NULL,
                    PRIMARY KEY(project_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS project_documents (
                    project_id INTEGER NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
                    version INTEGER NOT NULL, content TEXT NOT NULL, sha256 TEXT NOT NULL,
                    spec_version INTEGER, created_at REAL NOT NULL,
                    PRIMARY KEY(project_id,kind,version)
                );
                CREATE TABLE IF NOT EXISTS project_rounds (
                    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                    number INTEGER NOT NULL, request_key TEXT NOT NULL, state TEXT NOT NULL,
                    spec_version INTEGER NOT NULL, plan_version INTEGER NOT NULL,
                    plan_sha256 TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(project_id,number), UNIQUE(project_id,request_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS project_one_open_round ON project_rounds(project_id)
                    WHERE state NOT IN ('completed','failed','cancelled');
                CREATE TABLE IF NOT EXISTS project_usage (
                    project_id INTEGER NOT NULL REFERENCES projects(id), request_key TEXT NOT NULL,
                    resource TEXT NOT NULL, amount INTEGER NOT NULL, granted INTEGER NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY(project_id,request_key)
                );
                CREATE TABLE IF NOT EXISTS project_events (
                    id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
                    kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS project_events_project ON project_events(project_id,id);
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            with db:
                yield db

    def _event(self, db, project_id, kind, payload):
        encoded = json.dumps(payload, ensure_ascii=False)
        self.privacy.require_clean(encoded)
        db.execute('INSERT INTO project_events(project_id,kind,payload_json,created_at) VALUES(?,?,?,?)',
                   (project_id, kind, encoded, self.clock()))

    def _get(self, db, project_id, scope):
        row = db.execute('SELECT p.* FROM projects p JOIN project_members m ON m.project_id=p.id '
                         'WHERE p.id=? AND p.chat_id=? AND p.topic_id=? AND m.user_id=?',
                         (project_id, *scope)).fetchone()
        if row is None:
            raise ProjectError('Проект не найден в этой теме или доступ не предоставлен.')
        return dict(row)

    def _editable(self, db, project):
        if project['state'] not in ('draft', 'ready', 'awaiting_input'):
            raise ProjectError('Изменять документы можно между раундами проекта.')
        if db.execute("SELECT 1 FROM project_rounds WHERE project_id=? AND state NOT IN "
                      "('completed','failed','cancelled')", (project['id'],)).fetchone():
            raise ProjectError('Сначала завершите текущий раунд.')

    def create(self, scope, repository, goal, *, request_key, budget=None):
        goal = self.privacy.require_clean(text(goal, 12000))
        request_key = text(request_key, 120)
        budget = limits(DEFAULT_LIMITS if budget is None else budget)
        repository = str(Path(repository).resolve())
        with self.connection() as db:
            old = db.execute('SELECT * FROM projects WHERE chat_id=? AND request_key=?',
                             (scope[0], request_key)).fetchone()
            if old:
                if (old['topic_id'], old['owner_id'], old['goal'], old['repository']) != (
                        scope[1], scope[2], goal, repository):
                    raise ProjectError('Ключ запроса уже относится к другому проекту.')
                return old['id']
            now = self.clock()
            project_id = db.execute('INSERT INTO projects(chat_id,topic_id,owner_id,request_key,goal,'
                'repository,limits_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (*scope, request_key, goal, repository, json.dumps(budget), now, now)).lastrowid
            db.execute('INSERT INTO project_members VALUES(?,?)', (project_id, scope[2]))
            self._event(db, project_id, 'project_created', {'owner_id': scope[2], 'limits': budget})
            return project_id

    def latest(self, scope):
        with self.connection() as db:
            row = db.execute('SELECT p.id FROM projects p JOIN project_members m ON m.project_id=p.id '
                'WHERE p.chat_id=? AND p.topic_id=? AND m.user_id=? ORDER BY p.id DESC LIMIT 1', scope).fetchone()
            return row['id'] if row else None

    def get(self, project_id, scope):
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            project['limits'] = json.loads(project.pop('limits_json'))
            project['usage'] = {key: 0 for key in DEFAULT_LIMITS if key != 'elapsed_seconds'}
            for row in db.execute('SELECT resource,SUM(amount) total FROM project_usage '
                                  'WHERE project_id=? AND granted=1 GROUP BY resource', (project_id,)):
                project['usage'][row['resource']] = row['total']
            project['usage']['elapsed_seconds'] = max(0, int(self.clock() - project['activated_at'])) if project['activated_at'] is not None else 0
            project['documents'] = {r['kind']: r['version'] for r in db.execute(
                'SELECT kind,MAX(version) version FROM project_documents WHERE project_id=? GROUP BY kind', (project_id,))}
            project['rounds'] = [dict(r) for r in db.execute('SELECT * FROM project_rounds WHERE project_id=? ORDER BY number', (project_id,))]
            return project

    def add_member(self, project_id, scope, user_id):
        if type(user_id) is not int or user_id <= 0:
            raise ProjectError('Нужен числовой ID участника.')
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            if project['owner_id'] != scope[2]:
                raise ProjectError('Участников добавляет владелец проекта.')
            if db.execute('INSERT OR IGNORE INTO project_members VALUES(?,?)', (project_id, user_id)).rowcount:
                self._event(db, project_id, 'member_added', {'user_id': user_id})

    def save_document(self, project_id, scope, kind, content, *, expected_version):
        if kind not in ('spec', 'plan') or type(expected_version) is not int or expected_version < 0:
            raise ProjectError('Некорректный тип или версия документа.')
        content = self.privacy.require_clean(text(content))
        sha = hashlib.sha256(content.encode('utf-8')).hexdigest()
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            self._editable(db, project)
            old = db.execute('SELECT * FROM project_documents WHERE project_id=? AND kind=? ORDER BY version DESC LIMIT 1', (project_id, kind)).fetchone()
            current = old['version'] if old else 0
            if current != expected_version:
                raise ProjectError('Документ уже обновлён. Прочитайте последнюю версию.')
            spec = db.execute("SELECT MAX(version) FROM project_documents WHERE project_id=? AND kind='spec'", (project_id,)).fetchone()[0]
            if kind == 'plan' and spec is None:
                raise ProjectError('Сначала сохраните ТЗ проекта.')
            if old and old['sha256'] == sha and (kind == 'spec' or old['spec_version'] == spec):
                return current
            version = current + 1
            db.execute('INSERT INTO project_documents VALUES(?,?,?,?,?,?,?)',
                       (project_id, kind, version, content, sha, spec if kind == 'plan' else None, self.clock()))
            db.execute("UPDATE projects SET state='draft',updated_at=? WHERE id=?", (self.clock(), project_id))
            self._event(db, project_id, 'document_saved', {'kind': kind, 'version': version, 'sha256': sha, 'spec_version': spec})
            return version

    def document(self, project_id, scope, kind, version=None):
        if kind not in ('spec', 'plan'):
            raise ProjectError('Неизвестный документ.')
        with self.connection() as db:
            self._get(db, project_id, scope)
            row = db.execute('SELECT * FROM project_documents WHERE project_id=? AND kind=? '
                             'AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1',
                             (project_id, kind, version, version)).fetchone()
            return dict(row) if row else None

    def _consume(self, db, project, resource, amount, request_key):
        old = db.execute('SELECT * FROM project_usage WHERE project_id=? AND request_key=?',
                         (project['id'], request_key)).fetchone()
        if old:
            if (old['resource'], old['amount']) != (resource, amount):
                raise ProjectError('Ключ расхода уже использован для другой операции.')
            return bool(old['granted'])
        if project['state'] in HALTED:
            raise ProjectError('Проект остановлен или ожидает решения человека.')
        budget = json.loads(project['limits_json'])
        used = db.execute('SELECT COALESCE(SUM(amount),0) FROM project_usage WHERE project_id=? '
                          'AND resource=? AND granted=1', (project['id'], resource)).fetchone()[0]
        now = self.clock()
        expired = project['activated_at'] is not None and now - project['activated_at'] >= budget['elapsed_seconds']
        granted = not expired and used + amount <= budget[resource]
        db.execute('INSERT INTO project_usage VALUES(?,?,?,?,?,?)',
                   (project['id'], request_key, resource, amount, int(granted), now))
        if granted:
            db.execute('UPDATE projects SET activated_at=COALESCE(activated_at,?),updated_at=? WHERE id=?', (now, now, project['id']))
            self._event(db, project['id'], 'budget_reserved', {'resource': resource, 'amount': amount, 'request_key': request_key})
        else:
            reason = 'elapsed_seconds' if expired else resource
            db.execute("UPDATE projects SET previous_state=state,state='limited',reason=?,updated_at=? WHERE id=?", (reason, now, project['id']))
            self._event(db, project['id'], 'budget_exhausted', {'resource': reason, 'request_key': request_key})
        return granted

    def consume(self, project_id, scope, resource, *, request_key, amount=1):
        if resource not in DEFAULT_LIMITS or resource == 'elapsed_seconds' or type(amount) is not int or amount < 1:
            raise ProjectError('Некорректный расход бюджета.')
        request_key = text(request_key, 120)
        with self.connection() as db:
            granted = self._consume(db, self._get(db, project_id, scope), resource, amount, request_key)
        if not granted:
            raise BudgetExceeded('Лимит проекта исчерпан. Нужно решение человека.')

    def begin_round(self, project_id, scope, *, request_key):
        request_key = text(request_key, 120)
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            old = db.execute('SELECT id FROM project_rounds WHERE project_id=? AND request_key=?', (project_id, request_key)).fetchone()
            if old:
                return old['id']
            self._editable(db, project)
            spec = db.execute("SELECT MAX(version) FROM project_documents WHERE project_id=? AND kind='spec'", (project_id,)).fetchone()[0]
            plan = db.execute("SELECT * FROM project_documents WHERE project_id=? AND kind='plan' ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
            if not plan or plan['spec_version'] != spec:
                raise ProjectError('Нужен план, составленный для текущей версии ТЗ.')
            granted = self._consume(db, project, 'rounds', 1, 'round:' + request_key)
            if granted:
                number = db.execute('SELECT COUNT(*)+1 FROM project_rounds WHERE project_id=?', (project_id,)).fetchone()[0]
                now = self.clock()
                result = db.execute('INSERT INTO project_rounds(project_id,number,request_key,state,spec_version,plan_version,plan_sha256,created_at,updated_at) VALUES(?,?,?,\'planning\',?,?,?,?,?)',
                    (project_id, number, request_key, spec, plan['version'], plan['sha256'], now, now)).lastrowid
                db.execute("UPDATE projects SET state='planning',updated_at=? WHERE id=?", (now, project_id))
                self._event(db, project_id, 'round_started', {'round_id': result, 'number': number, 'spec_version': spec, 'plan_version': plan['version'], 'plan_sha256': plan['sha256']})
        if not granted:
            raise BudgetExceeded('Лимит раундов или времени проекта исчерпан.')
        return result

    def advance_round(self, project_id, scope, round_id, *, expected_state, state):
        if state not in ROUND_TRANSITIONS.get(expected_state, set()):
            raise ProjectError('Недопустимый переход раунда.')
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            if project['state'] in HALTED:
                raise ProjectError('Проект остановлен или ожидает решения человека.')
            row = db.execute('SELECT * FROM project_rounds WHERE id=? AND project_id=?', (round_id, project_id)).fetchone()
            if not row or row['state'] != expected_state:
                raise ProjectError('Состояние раунда уже изменилось.')
            db.execute('UPDATE project_rounds SET state=?,updated_at=? WHERE id=?', (state, self.clock(), round_id))
            next_state = 'ready' if state == 'completed' else 'awaiting_input' if state == 'failed' else state
            db.execute('UPDATE projects SET state=?,updated_at=? WHERE id=?', (next_state, self.clock(), project_id))
            self._event(db, project_id, 'round_state_changed', {'round_id': round_id, 'from': expected_state, 'to': state})

    def control(self, project_id, scope, action):
        if action not in ('pause', 'resume', 'cancel'):
            raise ProjectError('Неизвестное действие проекта.')
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            old = project['state']
            if old in TERMINAL:
                raise ProjectError('Проект уже завершён.')
            if action == 'pause':
                if old in ('paused', 'limited', 'awaiting_input'):
                    return
                state, previous = 'paused', old
            elif action == 'resume':
                if old not in ('paused', 'limited', 'awaiting_input'):
                    raise ProjectError('Проект не ожидает возобновления.')
                state, previous = project['previous_state'] or 'draft', ''
                if state in HALTED:
                    raise ProjectError('Сначала устраните причину остановки проекта.')
                if old == 'limited':
                    budget = json.loads(project['limits_json'])
                    resource = project['reason']
                    expired = (project['activated_at'] is not None and
                               self.clock() - project['activated_at'] >= budget['elapsed_seconds'])
                    used = db.execute('SELECT COALESCE(SUM(amount),0) FROM project_usage '
                                      'WHERE project_id=? AND resource=? AND granted=1', (project_id, resource)).fetchone()[0]
                    if expired or (resource != 'elapsed_seconds' and used >= budget.get(resource, 0)):
                        raise ProjectError('Сначала увеличьте исчерпанный лимит проекта.')
            else:
                state, previous = 'cancelled', ''
                db.execute("UPDATE project_rounds SET state='cancelled',updated_at=? WHERE project_id=? AND state NOT IN ('completed','failed','cancelled')", (self.clock(), project_id))
            db.execute('UPDATE projects SET state=?,previous_state=?,reason=\'\',updated_at=? WHERE id=?', (state, previous, self.clock(), project_id))
            self._event(db, project_id, 'project_state_changed', {'from': old, 'to': state, 'actor_id': scope[2]})

    def wait_for_input(self, project_id, scope, reason):
        reason = self.privacy.require_clean(text(reason, 2000))
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            if project['state'] in HALTED:
                raise ProjectError('Проект уже остановлен или ожидает решения.')
            db.execute("UPDATE projects SET previous_state=state,state='awaiting_input',reason=?,updated_at=? WHERE id=?",
                       (reason, self.clock(), project_id))
            self._event(db, project_id, 'input_requested', {'reason': reason})

    def complete(self, project_id, scope):
        """Host completion gate; actual acceptance evidence belongs to later stages."""
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            last = db.execute('SELECT * FROM project_rounds WHERE project_id=? ORDER BY number DESC LIMIT 1', (project_id,)).fetchone()
            latest = db.execute("SELECT MAX(version) FROM project_documents WHERE project_id=? AND kind='spec'", (project_id,)).fetchone()[0]
            if project['state'] != 'ready' or not last or last['state'] != 'completed' or last['spec_version'] != latest:
                raise ProjectError('Завершение возможно после принятого раунда текущего ТЗ.')
            db.execute("UPDATE projects SET state='completed',updated_at=? WHERE id=?", (self.clock(), project_id))
            self._event(db, project_id, 'project_completed', {'round_id': last['id'], 'spec_version': latest})

    def set_limits(self, project_id, scope, changes):
        if not isinstance(changes, dict) or not changes or set(changes) - set(DEFAULT_LIMITS):
            raise ProjectError('Неизвестный лимит проекта.')
        with self.connection() as db:
            project = self._get(db, project_id, scope)
            if project['state'] in TERMINAL:
                raise ProjectError('Проект уже завершён.')
            budget = limits({**json.loads(project['limits_json']), **changes})
            db.execute('UPDATE projects SET limits_json=?,updated_at=? WHERE id=?', (json.dumps(budget), self.clock(), project_id))
            self._event(db, project_id, 'limits_changed', budget)

    def events(self, project_id, scope, *, after=0, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProjectError('Лимит событий: от 1 до 100.')
        with self.connection() as db:
            self._get(db, project_id, scope)
            return [dict(r) for r in db.execute('SELECT * FROM project_events WHERE project_id=? AND id>? ORDER BY id LIMIT ?', (project_id, after, limit))]
