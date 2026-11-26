"""One local polling process per bot; OS locks are released even after a crash."""
from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class BotInstance:
    def __init__(self, token: str, directory: Path | None = None):
        identity = hashlib.sha256(token.encode('utf-8')).hexdigest()
        self.path = (directory or Path(tempfile.gettempdir())) / f'agentrelay-{identity}.lock'
        self._stream = None

    def __enter__(self):
        stream = self.path.open('a+b')
        try:
            if self.path.stat().st_size == 0:
                stream.write(b'\0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise AlreadyRunningError(
                    'This Telegram bot is already running on this computer. '
                    'Stop its other terminal/process before starting another instance.'
                ) from None
            raise
        self._stream = stream
        return self

    def __exit__(self, *args):
        # Keep the file: deleting it could let contenders lock different files.
        # Closing the handle releases the OS lock; no stale-PID cleanup needed.
        if self._stream is not None:
            self._stream.close()
            self._stream = None
