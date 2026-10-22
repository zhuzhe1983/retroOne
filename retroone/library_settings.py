"""Per-player library navigation preferences; no ROM or launch state is stored here."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Literal

from .profiles import atomic_json

LibrarySort = Literal['default', 'name', 'popularity', 'release_date', 'added_at', 'play_count', 'last_played']
SortDirection = Literal['asc', 'desc']
SORT_OPTIONS = {'default': 'asc', 'name': 'asc', 'popularity': 'desc', 'release_date': 'desc',
                'added_at': 'desc', 'play_count': 'desc', 'last_played': 'desc'}
DEFAULT_LIBRARY_SETTINGS = {'pinned_platforms': [], 'sort': 'popularity', 'direction': 'desc'}


class LibrarySettingsError(Exception):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


class LibrarySettings:
    def __init__(self, config_root: Path):
        self.path = Path(config_root) / 'library-settings.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix('.lock')
        self.thread_lock = threading.RLock()

    @contextmanager
    def locked(self):
        if not self.thread_lock.acquire(timeout=5):
            raise LibrarySettingsError('游戏库设置正在更新，请稍后重试')
        handle = None
        try:
            if self.path.parent.is_symlink():
                raise ValueError('settings directory is a symbolic link')
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(descriptor, 'a+b')
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('settings lock is not a regular file')
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LibrarySettingsError('游戏库设置正在更新，请稍后重试')
                    time.sleep(.01)
            yield
        except (OSError, ValueError, TypeError) as error:
            raise LibrarySettingsError('游戏库设置无法读取或保存；原有设置已保留') from error
        finally:
            if handle:
                handle.close()
            self.thread_lock.release()

    @staticmethod
    def validate(value):
        if (type(value) is not dict or set(value) != set(DEFAULT_LIBRARY_SETTINGS) or
                type(value['pinned_platforms']) is not list or len(value['pinned_platforms']) > 128 or
                any(type(item) is not str or not re.fullmatch(r'[a-z0-9_-]{1,60}', item) for item in value['pinned_platforms']) or
                len(set(value['pinned_platforms'])) != len(value['pinned_platforms']) or
                type(value['sort']) is not str or value['sort'] not in SORT_OPTIONS or
                type(value['direction']) is not str or value['direction'] not in ('asc', 'desc')):
            raise ValueError('invalid library settings')
        return dict(value, pinned_platforms=list(value['pinned_platforms']))

    def _load(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return dict(DEFAULT_LIBRARY_SETTINGS, pinned_platforms=[])
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            raise ValueError('settings must be a small regular file')
        value = json.loads(self.path.read_text('utf-8'))
        if (type(value) is not dict or set(value) != {'version', 'settings'} or
                type(value['version']) is not int or value['version'] != 1):
            raise ValueError('unsupported settings document')
        return self.validate(value['settings'])

    def get(self):
        with self.locked():
            return self._load()

    def update(self, value):
        try:
            value = self.validate(value)
        except (KeyError, ValueError, TypeError) as error:
            raise LibrarySettingsError('请选择有效的平台和排序方式', 422) from error
        with self.locked():
            current = self._load()
            if value != current or not self.path.exists():
                atomic_json(self.path, {'version': 1, 'settings': value})
            return value

    def view(self):
        return {'settings': self.get(), 'sort_options': [dict(id=key, default_direction=value) for key, value in SORT_OPTIONS.items()]}
