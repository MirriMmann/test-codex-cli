"""Private, bounded Telegram context snapshots; never a queue of executable work."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


MAX_TOPICS = 32
MAX_MESSAGES = 101  # configured maximum of 100 previous messages plus current
MAX_TEXT = 600
MAX_NAME = 64
_MAX_SNAPSHOT = 16_000_000  # includes JSON escaping at the maximum bounded sizes


class GroupHistoryError(ValueError):
    """Safe local-storage diagnostic, without database contents or credentials."""


def validate_snapshot(value: object) -> list:
    if not isinstance(value, list) or len(value) > MAX_TOPICS:
        raise ValueError()
    topics = set()
    for topic in value:
        if (not isinstance(topic, dict) or set(topic) != {'topic_id', 'entries'}
                or type(topic['topic_id']) is not int or not 0 <= topic['topic_id'] < 2**63
                or topic['topic_id'] in topics or not isinstance(topic['entries'], list)
                or not 1 <= len(topic['entries']) <= MAX_MESSAGES):
            raise ValueError()
        topics.add(topic['topic_id'])
        for entry in topic['entries']:
            if not isinstance(entry, dict) or set(entry) != {
                    'message_id', 'sender_kind', 'author', 'text', 'sender', 'own'}:
                raise ValueError()
            sender = entry['sender']
            if (not isinstance(sender, dict) or set(sender) != {'id', 'username', 'is_bot'}
                    or type(sender['id']) is not int or not 0 < sender['id'] < 2**63
                    or not isinstance(sender['username'], str) or len(sender['username']) > 32
                    or type(sender['is_bot']) is not bool or type(entry['own']) is not bool
                    or entry['sender_kind'] != ('bot' if sender['is_bot'] else 'human')
                    or (entry['own'] and not sender['is_bot'])
                    or not isinstance(entry['author'], str) or len(entry['author']) > MAX_NAME
                    or not isinstance(entry['text'], str) or not entry['text'].strip()
                    or len(entry['text']) > MAX_TEXT
                    or (entry['message_id'] is not None and (type(entry['message_id']) is not int
                        or not 0 < entry['message_id'] < 2**63))):
                raise ValueError()
    return value


class GroupHistory:
    def __init__(self, filename: str, *, bot_id: int, chat_id: int):
        self.path = Path(filename)
        if '\x00' in filename or self.path.suffix.lower() != '.sqlite3':
            raise GroupHistoryError('GROUP_CONTEXT_FILE must use a .sqlite3 extension')
        self.bot_id, self.chat_id = bot_id, chat_id
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS telegram_context ('
                           'bot_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, '
                           'version INTEGER NOT NULL, snapshot TEXT NOT NULL, '
                           'PRIMARY KEY(bot_id,chat_id))')
        except (OSError, sqlite3.Error):
            raise GroupHistoryError('Telegram history cannot be opened; check GROUP_CONTEXT_FILE') from None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=1)
        try:
            db.execute('PRAGMA secure_delete=ON')
            with db:
                yield db
        finally:
            db.close()

    def load(self) -> list:
        try:
            with self.connect() as db:
                row = db.execute('SELECT version,CASE WHEN length(snapshot)<=? THEN snapshot END '
                                 'FROM telegram_context WHERE bot_id=? AND chat_id=?',
                                 (_MAX_SNAPSHOT, self.bot_id, self.chat_id)).fetchone()
                if row is None:
                    return []
                if row[0] != 1 or not isinstance(row[1], str):
                    raise ValueError()
            return validate_snapshot(json.loads(row[1]))
        except (sqlite3.Error, ValueError, TypeError, RecursionError):
            raise GroupHistoryError('Telegram history is unreadable; check GROUP_CONTEXT_FILE. '
                                    'Existing history was not reset') from None

    def save(self, snapshot: list) -> None:
        try:
            text = json.dumps(validate_snapshot(snapshot), ensure_ascii=False)
            if len(text) > _MAX_SNAPSHOT:
                raise ValueError()
            with self.connect() as db:
                db.execute('INSERT INTO telegram_context(bot_id,chat_id,version,snapshot) VALUES(?,?,1,?) '
                           'ON CONFLICT(bot_id,chat_id) DO UPDATE SET version=1,snapshot=excluded.snapshot',
                           (self.bot_id, self.chat_id, text))
        except (sqlite3.Error, ValueError, TypeError, RecursionError):
            raise GroupHistoryError('Telegram history could not be saved; check GROUP_CONTEXT_FILE') from None
