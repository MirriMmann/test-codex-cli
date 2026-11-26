"""Bounded group context and explicit peer handoff for AgentRelay.

Nothing in this module calls ChatGPT. Observed chat text is untrusted context,
not a command or instruction. An optional store persists bounded snapshots.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict, deque
from typing import Any

from group_history import MAX_NAME, MAX_TEXT, MAX_TOPICS

_HANDOFF = re.compile(r'\[\[handoff:([A-Za-z][A-Za-z0-9_]{4,31})\]\]', re.IGNORECASE)


def trusted_sender(message: dict[str, Any], allowed_users: frozenset[int],
                   peers: frozenset[str], peer_ids: frozenset[int], own_id: int) -> bool:
    """Require a numeric identity, and both numeric ID and username for bots."""
    if message.get('sender_chat'):
        return False
    sender = message.get('from') or {}
    identity = sender.get('id')
    if type(identity) is not int or identity <= 0 or identity == own_id:
        return False
    if sender.get('is_bot'):
        return identity in peer_ids and str(sender.get('username') or '').lower() in peers
    return identity in allowed_users


def addressed_by_alias(text: str, aliases: tuple[str, ...]) -> bool:
    """Match a distinct name, not a substring of a word or @username."""
    for alias in aliases:
        alias = alias.strip()
        if len(alias) < 3 or len(alias) > 40:
            continue
        if re.search(r'(?<![\w@])' + re.escape(alias) + r'(?!\w)', text, re.IGNORECASE):
            return True
    return False


class GroupAwareness:
    def __init__(self, limit: int = 30, *, store=None, restore_entry=None, own_id: int = 0) -> None:
        if not 0 <= limit <= 100:
            raise ValueError('GROUP_CONTEXT_LIMIT must be between 0 and 100')
        self.entries: OrderedDict[int, deque[dict[str, Any]]] = OrderedDict()
        self.limit = limit
        self.store = store if limit else None
        self.own_id = own_id
        if self.store is not None:
            if restore_entry is None:
                raise ValueError('Persistent group context requires current sender access checks')
            saved = self.store.load()
            for topic in saved:
                rows = [restored for entry in topic['entries']
                        if (restored := restore_entry(entry)) is not None]
                if rows:
                    self.entries[topic['topic_id']] = deque(rows[-(limit + 1):], maxlen=limit + 1)
            normalized = self._snapshot(self.entries)
            if normalized != saved:
                # Persist current allowlists, redaction and reduced limits too.
                self.store.save(normalized)

    @staticmethod
    def _snapshot(entries) -> list:
        return [{'topic_id': topic_id, 'entries': list(rows)} for topic_id, rows in entries.items()]

    def _append(self, topic_id: int, entry: dict[str, Any]) -> None:
        if not self.limit:
            return
        updated = OrderedDict((topic, deque(rows, maxlen=self.limit + 1))
                              for topic, rows in self.entries.items())
        if topic_id not in updated:
            # Keep the invocation itself plus N previous messages.
            updated[topic_id] = deque(maxlen=self.limit + 1)
        elif entry['message_id'] is not None:
            updated[topic_id] = deque((row for row in updated[topic_id]
                                      if row['message_id'] != entry['message_id']), maxlen=self.limit + 1)
        updated.move_to_end(topic_id)
        while len(updated) > MAX_TOPICS:
            updated.popitem(last=False)
        updated[topic_id].append(entry)
        if self.store is not None:
            self.store.save(self._snapshot(updated))
        self.entries = updated

    def remember(self, message: dict[str, Any]) -> None:
        if not self.limit:
            return
        text = message.get('text')
        if not isinstance(text, str) or not text.strip():
            return
        sender = message.get('from') or {}
        name = str(sender.get('first_name') or sender.get('username') or sender.get('id'))[:MAX_NAME]
        kind = 'bot' if sender.get('is_bot') else 'human'
        message_id = message.get('message_id')
        identity = message_id if type(message_id) is int else None
        self._append(message.get('message_thread_id') or 0,
                     {'message_id': identity, 'sender_kind': kind,
                      'author': name, 'text': self._short(text), 'own': False,
                      'sender': {'id': sender.get('id'), 'is_bot': bool(sender.get('is_bot')),
                                 'username': str(sender.get('username') or '').lower()[:32]}})

    def remember_own_reply(self, username: str, text: str, *, topic_id: int = 0) -> None:
        if self.limit and text.strip():
            self._append(topic_id, {'message_id': None, 'sender_kind': 'bot',
                                    'author': username[:MAX_NAME], 'text': self._short(text), 'own': True,
                                    'sender': {'id': self.own_id, 'is_bot': True,
                                               'username': username.lower()[:32]}})

    def recent_text(self, *, exclude_message_id: int | None = None, topic_id: int = 0) -> str:
        if not self.limit:
            return ''
        items = [entry for entry in self.entries.get(topic_id, ())
                 if exclude_message_id is None or entry['message_id'] != exclude_message_id]
        # Numeric actor IDs and internal provenance are only for access checks.
        return '\n'.join(json.dumps({key: entry[key] for key in
                          ('message_id', 'sender_kind', 'author', 'text')}, ensure_ascii=False)
                         for entry in items[-self.limit:])

    @staticmethod
    def _short(text: str) -> str:
        return ' '.join(text.split())[:MAX_TEXT]


def explicit_handoff(text: str, peers: frozenset[str]) -> str:
    """Route an explicit handoff to one configured peer using a command mention.

    Handoff markers or a leading @Peer route through /relay@Peer, including
    responses to bots. Each response can select one peer for the next turn.
    """
    chosen = None
    for match in _HANDOFF.finditer(text):
        if chosen is None and match.group(1).lower() in peers:
            chosen = match.group(1)
    clean = _HANDOFF.sub('', text).strip()
    leading_peer = re.match(r'^@([A-Za-z][A-Za-z0-9_]{4,31})\b(?:\s+|$)', clean, re.I)
    if leading_peer and leading_peer.group(1).lower() in peers:
        chosen = chosen or leading_peer.group(1)
        # Avoid repeating the username after /relay@Peer.
        if chosen.lower() == leading_peer.group(1).lower():
            clean = clean[leading_peer.end():].strip()
    if chosen:
        return f'/relay@{chosen} {clean}'.strip()
    return clean
