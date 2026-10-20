"""Per-player defaults and device preferences; changes apply to new sessions only."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import threading
import time
from typing import Literal
from uuid import UUID

from .profiles import atomic_json

StreamResolution = Literal['720p', '1080p']
DISPLAY_RESOLUTIONS = {
    '720p': {'width': 1280, 'height': 720},
    '1080p': {'width': 1920, 'height': 1080},
}
DISPLAY_DEFAULTS = {'stream_resolution': '720p'}
DISPLAY_DEVICE_LIMIT = 128
MAX_DEVICE_DOCUMENT = 64 * 1024


class DisplaySettingsError(Exception):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


class DisplaySettings:
    def __init__(self, config_root: Path):
        self.path = Path(config_root) / 'display-settings.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix('.lock')
        self.devices_path = self.path.with_name('display-devices.json')
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

    @staticmethod
    def device_id(value):
        if value is None:
            return None
        try:
            if not isinstance(value, (str, UUID)):
                raise ValueError('device id must be a UUID')
            return str(UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as error:
            raise DisplaySettingsError('设备标识无效，请使用浏览器生成的 UUID', 422) from error

    @staticmethod
    def read_document(path, limit):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError('display settings must be a small regular file')
        return json.loads(path.read_text('utf-8'))

    def load(self):
        value = self.read_document(self.path, 4096)
        if value is None:
            # JSON null is invalid, whereas a missing file uses legacy defaults.
            if self.path.exists():
                raise ValueError('invalid display settings format')
            return dict(DISPLAY_DEFAULTS)
        if not isinstance(value, dict) or set(value) != {'version', 'settings'} or type(value['version']) is not int or value['version'] != 1:
            raise ValueError('unknown display settings format')
        return self.validate(value['settings'])

    def load_devices(self):
        try:
            value = self.read_document(self.devices_path, MAX_DEVICE_DOCUMENT)
            if value is None and not self.devices_path.exists():
                return {}
            if (not isinstance(value, dict) or set(value) != {'version', 'devices'} or
                    type(value['version']) is not int or value['version'] != 1 or
                    not isinstance(value['devices'], dict) or len(value['devices']) > DISPLAY_DEVICE_LIMIT):
                raise ValueError('invalid display devices format')
            devices = value['devices']
            for identifier, settings in devices.items():
                if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
                    raise ValueError('device key is not a canonical UUID')
                self.validate(settings)
            return devices
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise DisplaySettingsError('设备画面设置无法读取，请检查 display-devices.json；已有设置未重置') from error

    def _selection(self, identifier):
        defaults = self.load()
        devices = self.load_devices() if identifier is not None else {}
        overridden = identifier in devices
        return {'settings': dict(devices[identifier] if overridden else defaults),
                'default_settings': dict(defaults), 'device_id': identifier,
                'scope': 'device' if identifier is not None else 'profile',
                'source': 'device' if overridden else 'profile_default'}

    def selection(self, device_id=None):
        identifier = self.device_id(device_id)
        with self.locked():
            return self._selection(identifier)

    def get(self, device_id=None):
        return self.selection(device_id)['settings']

    def update(self, changes, device_id=None):
        identifier = self.device_id(device_id)
        try:
            changes = self.validate(changes)
        except ValueError as error:
            raise DisplaySettingsError('请选择支持的画面分辨率', 422) from error
        with self.locked():
            self.load()  # Corrupt defaults must never be silently overwritten.
            if identifier is None:
                atomic_json(self.path, {'version': 1, 'settings': changes})
            else:
                devices = self.load_devices()
                if identifier not in devices and len(devices) >= DISPLAY_DEVICE_LIMIT:
                    raise DisplaySettingsError('此玩家已保存 128 台设备的画面设置；请先将不再使用的设备恢复为继承默认设置', 409)
                # Saving the current default is an explicit device preference too.
                if devices.get(identifier) != changes:
                    devices[identifier] = changes
                    atomic_json(self.devices_path, {'version': 1, 'devices': devices})
            return dict(changes)

    def reset_device(self, device_id):
        identifier = self.device_id(device_id)
        if identifier is None:
            raise DisplaySettingsError('恢复默认需要指定当前设备，不能删除玩家默认设置', 422)
        with self.locked():
            defaults = self.load()
            devices = self.load_devices()
            if identifier in devices:
                del devices[identifier]
                atomic_json(self.devices_path, {'version': 1, 'devices': devices})
            return dict(defaults)

    def launch_snapshot(self, device_id=None):
        return self.get(device_id)


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
