"""Per-worker display preferences. Saving never reconfigures a running session."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import threading
import time
from typing import Literal

from .profiles import atomic_json

StreamResolution = Literal['720p', '1080p']
DISPLAY_RESOLUTIONS = {
    '720p': {'width': 1280, 'height': 720},
    '1080p': {'width': 1920, 'height': 1080},
}
DISPLAY_DEFAULTS = {'stream_resolution': '720p'}


class DisplaySettingsError(Exception):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


class DisplaySettings:
    def __init__(self, config_root: Path):
        self.path = Path(config_root) / 'display-settings.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix('.lock')
        self.thread_lock = threading.RLock()

    @contextmanager
    def locked(self):
        if not self.thread_lock.acquire(timeout=5):
            raise DisplaySettingsError('画面设置正在更新，请稍后重试')
        handle = None
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(descriptor, 'a+b')
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise DisplaySettingsError('画面设置正在更新，请稍后重试')
                    time.sleep(.01)
            yield
        except (OSError, ValueError, TypeError) as error:
            raise DisplaySettingsError('画面设置无法读取或保存，请检查配置文件；原有设置未重置') from error
        finally:
            if handle:
                handle.close()  # Releases the advisory file lock too.
            self.thread_lock.release()

    @staticmethod
    def validate(value):
        if (not isinstance(value, dict) or set(value) != {'stream_resolution'}
                or not isinstance(value['stream_resolution'], str)
                or value['stream_resolution'] not in DISPLAY_RESOLUTIONS):
            raise ValueError('invalid display settings')
        return dict(value)

    def load(self):
        if self.path.is_symlink():
            raise ValueError('display settings cannot be a symbolic link')
        if not self.path.exists():
            return dict(DISPLAY_DEFAULTS)
        if self.path.stat().st_size > 4096:
            raise ValueError('oversized display settings')
        value = json.loads(self.path.read_text('utf-8'))
        if not isinstance(value, dict) or set(value) != {'version', 'settings'} or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('unknown display settings format')
        return self.validate(value['settings'])

    def get(self):
        with self.locked():
            return self.load()

    def update(self, changes):
        try:
            changes = self.validate(changes)
        except ValueError as error:
            raise DisplaySettingsError('请选择支持的画面分辨率', 422) from error
        with self.locked():
            current = self.load()  # A corrupt existing document must never be silently overwritten.
            current.update(changes)
            atomic_json(self.path, {'version': 1, 'settings': current})
            return current

    def launch_snapshot(self):
        return self.get()


def display_view(settings, session, capabilities):
    """Only a verified runner snapshot can describe an active resolution."""
    active = None
    if session:
        snapshot = session.get('display')
        if isinstance(snapshot, dict):
            try:
                applied = DisplaySettings.validate(snapshot.get('settings'))
                dimensions = DISPLAY_RESOLUTIONS[applied['stream_resolution']]
                if all(type(snapshot.get(key)) is int and snapshot[key] == expected for key, expected in dimensions.items()):
                    active = {'session_id': session.get('id') or session.get('session_id'), 'settings': applied, **dimensions}
            except (ValueError, TypeError):
                pass
    available = capabilities.get('available') is True
    return {
        'settings': dict(settings),
        'capabilities': {'available': available,
                         'reason': None if available else capabilities.get('reason') or '云端分辨率设置尚未就绪',
                         'stream_resolution': [{'value': key, **value} for key, value in DISPLAY_RESOLUTIONS.items()]},
        'applies_on': 'next_launch',
        'active': active,
        'active_unknown': bool(session and not active),
        'pending': bool(session and (not active or active['settings'] != settings)),
    }
