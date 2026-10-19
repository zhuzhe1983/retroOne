"""Personal interface preferences; independent of display settings and saves."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import threading
import time
from typing import Literal

from .profiles import atomic_json

InterfaceLocale = Literal['auto', 'zh-CN', 'en']
SUPPORTED_LOCALES = ('zh-CN', 'en')
DEFAULT_INTERFACE_SETTINGS = {'locale': 'zh-CN'}


class InterfaceSettingsError(Exception):
    def __init__(self, message, status_code=503, code='interface_settings_unavailable'):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class InterfaceSettings:
    def __init__(self, config_root: Path):
        self.path = Path(config_root) / 'interface-settings.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix('.lock')
        self.thread_lock = threading.RLock()

    @contextmanager
    def locked(self):
        if not self.thread_lock.acquire(timeout=5):
            raise InterfaceSettingsError('界面设置正在更新，请稍后重试', code='interface_settings_busy')
        handle = None
        try:
            if self.path.parent.is_symlink():
                raise ValueError('interface settings directory cannot be a symbolic link')
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(descriptor, 'a+b')
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('interface settings lock must be a regular file')
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise InterfaceSettingsError('界面设置正在更新，请稍后重试', code='interface_settings_busy')
                    time.sleep(.01)
            yield
        except (OSError, ValueError, TypeError) as error:
            raise InterfaceSettingsError('界面设置无法读取或保存，请检查配置文件；原有设置未重置') from error
        finally:
            if handle:
                handle.close()
            self.thread_lock.release()

    @staticmethod
    def validate(value):
        if (not isinstance(value, dict) or set(value) != {'locale'} or
                type(value['locale']) is not str or value['locale'] not in (*SUPPORTED_LOCALES, 'auto')):
            raise ValueError('invalid interface settings')
        return dict(value)

    def _load(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return dict(DEFAULT_INTERFACE_SETTINGS)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError('interface settings must be a small regular file')
        value = json.loads(self.path.read_text('utf-8'))
        if (not isinstance(value, dict) or set(value) != {'version', 'settings'} or
                type(value['version']) is not int or value['version'] != 1):
            raise ValueError('unknown interface settings format')
        return self.validate(value['settings'])

    def get(self):
        with self.locked():
            return self._load()

    def update(self, value):
        try:
            value = self.validate(value)
        except ValueError as error:
            raise InterfaceSettingsError('请选择自动、简体中文或 English', 422, 'invalid_interface_settings') from error
        with self.locked():
            current = self._load()  # A corrupt document must not be overwritten.
            if value != current or not self.path.exists():
                atomic_json(self.path, {'version': 1, 'settings': value})
            return value

    def view(self):
        return {'settings': self.get(), 'supported_locales': list(SUPPORTED_LOCALES)}
