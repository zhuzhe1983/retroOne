"""Server-owned per-game launch configuration, separate from catalog metadata.

Pass RETROONE_LIBRARY_DATA_ROOT/runtime as storage_root. The directory can be
shared by workers: flock protects read/merge/atomic-write across processes.
ROMs are read-only; titles, covers and member preferences are not persisted here.

Integration:
  generate(game, review=None) during catalog discovery; preserves overrides.
  summary(game, review=None) for the public launch_config view (never regenerates).
  update_overrides(game, patch) for an explicit settings change; None removes a key.
  resolve(game, requested_mode=None, review=None) before stopping an old session.
The resolved copy retains original IDs/paths and has a server-owned _game_config
snapshot. API/Runner still validate actual installed cores, BIOS, resources and
browser delivery limits. Configuration generation never proves playability.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time

from .platforms import PLATFORMS

VERSION = 1
ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
MAX_DOCUMENT = 1024 * 1024
BROWSER_LIMIT = 20 * 1024**2
ARCADE_PLATFORMS = {'mame', 'lightgun', 'fbneo'}
ARCADE_NATIVE = {'fbneo', 'fbalpha2012', 'mame2003_plus', 'mame2003', 'mame2010'}
ARCADE_BROWSER = {'fbneo', 'fbalpha2012', 'mame2003_plus', 'mame2003'}
OVERRIDE_FIELDS = {'mode', 'native_core', 'browser_core', 'engine_hint', 'input_profile', 'disabled', 'notes'}
INPUT_PROFILES = {'default'}  # Only the existing, implemented keyboard/gamepad mapping.


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def within(path, root):
    try: path.relative_to(root); return True
    except ValueError: return False


class GameConfigError(RuntimeError):
    def __init__(self, code, message, status_code=409, summary=None):
        super().__init__(message)
        self.code, self.status_code, self.summary = code, status_code, summary


class GameConfigStore:
    def __init__(self, storage_root: Path, rom_root: Path, definitions=None, runtime_build=None):
        self.storage_root = Path(storage_root).resolve()
        self.rom_root = Path(rom_root).resolve()
        if within(self.storage_root, self.rom_root):
            raise ValueError('Launch configuration must be outside the read-only ROM root')
        self.root = self.storage_root / 'game-configs'
        if self.root.is_symlink():
            raise ValueError('Launch configuration directory cannot be a symlink')
        self.root.mkdir(parents=True, exist_ok=True)
        self.definitions = definitions if definitions is not None else PLATFORMS
        self.runtime_build = runtime_build
        self.thread_lock = threading.RLock()

    @contextmanager
    def locked(self):
        if not self.thread_lock.acquire(timeout=10):
            raise GameConfigError('config_busy', '游戏配置正在更新，请稍后重试', 503)
        handle = None
        try:
            if self.root.is_symlink():
                raise GameConfigError('unsafe_config_path', '游戏配置目录不能是符号链接', 503)
            fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            handle = os.fdopen(fd, 'a+b')
            deadline = time.monotonic() + 10
            while True:
                try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GameConfigError('config_busy', '游戏配置正在更新，请稍后重试', 503)
                    time.sleep(.02)
            yield
        except OSError as error:
            raise GameConfigError('config_io_error', '游戏配置无法读取或保存；原有配置已保留', 503) from error
        finally:
            if handle: handle.close()
            self.thread_lock.release()

    def _path(self, game):
        identifier = game.get('id')
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise GameConfigError('invalid_game_id', '无效的游戏 ID', 422)
        return self.root / (identifier + '.json')

    def _read(self, game):
        path = self._path(game)
        if path.is_symlink():
            raise GameConfigError('unsafe_config_path', '游戏配置不能是符号链接', 503)
        if not path.exists(): return None
        try:
            if path.stat().st_size > MAX_DOCUMENT: raise ValueError('oversized')
            value = json.loads(path.read_text('utf-8'))
            if (not isinstance(value, dict) or value.get('schema_version') != VERSION or
                    value.get('game_id') != game['id'] or not isinstance(value.get('generated'), dict) or
                    not isinstance(value.get('overrides'), dict) or
                    not isinstance(value['generated'].get('source_signature'), str) or
                    not isinstance(value['generated'].get('defaults'), dict)):
                raise ValueError('invalid document')
            defaults = value['generated']['defaults']
            if (set(defaults) != {'mode', 'native_core', 'browser_core', 'engine_hint', 'input_profile', 'disabled'} or
                    defaults['mode'] != 'auto' or defaults['input_profile'] not in INPUT_PROFILES or
                    defaults['disabled'] is not False or
                    any(item is not None and not isinstance(item, str) for item in
                        (defaults['native_core'], defaults['browser_core'], defaults['engine_hint']))):
                raise ValueError('invalid generated defaults')
            return value
        except (ValueError, TypeError, UnicodeError) as error:
            raise GameConfigError('corrupt_config', '游戏配置损坏或版本不支持；原文件已保留，请先检查', 503) from error

    def _write(self, game, document):
        path = self._path(game)
        if path.is_symlink():
            raise GameConfigError('unsafe_config_path', '游戏配置不能是符号链接', 503)
        payload = encoded(document).encode()
        if len(payload) > MAX_DOCUMENT:
            raise GameConfigError('config_too_large', '游戏配置超过大小限制', 422)
        with tempfile.NamedTemporaryFile(dir=self.root, prefix='.config-', delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(payload); output.flush(); os.fsync(output.fileno())
                temporary.chmod(0o600)
                os.replace(temporary, path)
            finally: temporary.unlink(missing_ok=True)

    def _asset(self, value):
        if not isinstance(value, str) or not value or '\x00' in value or '\\' in value:
            return {'relative_path': None, 'error': 'invalid_source_path'}
        path = Path(value)
        if not path.is_absolute(): path = self.rom_root / path
        if '..' in path.parts or not within(path, self.rom_root):
            return {'relative_path': None, 'error': 'outside_rom_root'}
        relative = path.relative_to(self.rom_root).as_posix()
        try:
            resolved = path.resolve(strict=True)
            if not within(resolved, self.rom_root):
                return {'relative_path': relative, 'error': 'outside_rom_root'}
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
                return {'relative_path': relative, 'error': 'not_regular_source'}
            return {'relative_path': relative, 'resolved_relative_path': resolved.relative_to(self.rom_root).as_posix(),
                    'kind': 'directory' if stat.S_ISDIR(info.st_mode) else 'file',
                    'size': info.st_size, 'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns,
                    'dev': info.st_dev, 'ino': info.st_ino}
        except (OSError, RuntimeError):
            return {'relative_path': relative, 'error': 'missing_or_unreadable_source'}

    def _snapshot(self, game):
        self._path(game)
        files = game.get('files') or [game.get('rom_path')]
        launches = game.get('launch_files') or [game.get('rom_path')]
        if not isinstance(files, list) or not isinstance(launches, list) or len(files) + len(launches) > 8192:
            raise GameConfigError('invalid_sources', '游戏资源清单无效或过大', 422)
        paths = [game.get('rom_path')] + files + launches
        if any(not isinstance(path, str) for path in paths):
            raise GameConfigError('invalid_sources', '游戏资源路径无效', 422)
        unique = list(dict.fromkeys(paths))
        assets = [self._asset(path) for path in unique]
        by_path = dict(zip(unique, assets))
        identity = {'platform': game.get('platform'), 'primary': by_path[game['rom_path']],
                    'sources': sorted(assets, key=lambda item: encoded(item)),
                    'launch_files': [by_path[path] for path in launches],
                    'native_core': game.get('native_core'), 'browser_core': game.get('browser_core'),
                    'engine_hint': game.get('engine_hint'), 'browser_eligible': game.get('browser_eligible') is True,
                    'unpacked_size': game.get('unpacked_size'), 'blocked_reason': game.get('blocked_reason'),
                    'effective_defaults': self._defaults(game)}
        return identity, digest(identity)

    def _allowed_cores(self, game, kind):
        definition = self.definitions.get(game.get('platform'), {})
        allowed = {definition.get(kind)} - {None}
        if game.get('platform') in ARCADE_PLATFORMS:
            allowed |= ARCADE_NATIVE if kind == 'native_core' else ARCADE_BROWSER
        return allowed

    def _validate_overrides(self, game, patch):
        if not isinstance(patch, dict) or set(patch) - OVERRIDE_FIELDS:
            raise GameConfigError('invalid_override', '启动配置含不支持的字段；不能指定命令、路径或环境变量', 422)
        for key, value in patch.items():
            if value is None: continue
            good = False
            if key == 'mode': good = isinstance(value, str) and value in {'auto', 'browser', 'stream'}
            elif key in {'native_core', 'browser_core'}:
                good = isinstance(value, str) and value in self._allowed_cores(game, key)
            elif key == 'engine_hint':
                # Runner has only exercised these exact detected Windows layouts.
                good = isinstance(value, str) and game.get('platform') == 'windows' and value in {'wine', 'sdlpal'} and value == game.get('engine_hint')
            elif key == 'input_profile': good = isinstance(value, str) and value in INPUT_PROFILES
            elif key == 'disabled': good = type(value) is bool
            elif key == 'notes': good = isinstance(value, str) and len(value) <= 2000 and '\x00' not in value
            if not good:
                raise GameConfigError('invalid_override', '不支持的启动配置值：' + key, 422)
        return dict(patch)

    def _defaults(self, game):
        # Runner only honors per-game native-core hints for these arcade systems.
        # Pegasus hints on other systems are historical metadata, not active config.
        native = (game.get('native_core') if game.get('platform') in ARCADE_PLATFORMS else
                  self.definitions.get(game.get('platform'), {}).get('native_core'))
        return {'mode': 'auto', 'native_core': native, 'browser_core': game.get('browser_core'),
                'engine_hint': game.get('engine_hint'), 'input_profile': 'default', 'disabled': False}

    @staticmethod
    def _mode(game, settings, requested):
        mode = requested or settings.get('mode')
        if mode == 'auto' or mode is None:
            mode = game.get('mode') if game.get('mode') in {'browser', 'stream'} else ('browser' if game.get('browser_eligible') else 'stream')
        if mode not in {'browser', 'stream'}:
            raise GameConfigError('invalid_mode', '请选择浏览器或云端运行方式', 422)
        return mode

    def _context(self, game, settings, mode):
        core = settings.get('browser_core' if mode == 'browser' else 'native_core')
        platform = game.get('platform')
        if mode == 'browser': engine = 'EmulatorJS'
        elif settings.get('engine_hint') in {'sdlpal', 'wine'}:
            engine = {'sdlpal': 'SDLPAL', 'wine': 'Wine'}[settings['engine_hint']]
        elif platform in {'mame', 'lightgun'} and core not in ARCADE_NATIVE: engine = 'MAME'
        else:
            engine = {'dos': 'DOSBox', 'ps2': 'PCSX2', 'switch': 'Eden', 'gamecube': 'Dolphin', 'wii': 'Dolphin'}.get(platform)
            if not engine: engine = 'RetroArch' if core else 'Unconfigured'
        if engine not in {'RetroArch', 'EmulatorJS'}:
            core = None  # Standalone engines do not load the catalog's Libretro hint.
        return {'mode': mode, 'engine': engine, 'core': core, 'input_profile': settings['input_profile']}

    def _issues(self, game, identity, settings, context, review=None, *, browser_limit=None):
        issues = []
        def add(code, message, blocking=False): issues.append({'code': code, 'message': message, 'blocking': blocking})
        for asset in identity['sources']:
            if asset.get('error'):
                add(asset['error'], '游戏资源缺失或路径不安全：' + (asset.get('relative_path') or '无效路径'), True)
        if identity['primary'].get('kind') == 'directory':
            add('directory_launch_unsupported', '此入口是目录，尚未确定可执行的游戏启动文件', True)
        if game.get('blocked_reason'):
            add('catalog_blocked', str(game['blocked_reason']).replace(str(self.rom_root), '<rom_root>'), True)
        if settings.get('disabled'): add('manually_disabled', '此游戏已在启动配置中停用', True)
        if context['engine'] == 'Unconfigured': add('engine_not_configured', '尚未配置适合此游戏的模拟器或原生引擎', True)
        for kind in ('native_core', 'browser_core'):
            value = settings.get(kind)
            if value is not None and value not in self._allowed_cores(game, kind):
                add('unsupported_core', '配置中的核心不在该平台允许的列表中：' + kind, True)
        if settings.get('engine_hint') is not None and not (
                game.get('platform') == 'windows' and settings['engine_hint'] in {'wine', 'sdlpal'} and settings['engine_hint'] == game.get('engine_hint')):
            add('unsupported_engine_hint', '游戏布局不符合此引擎的已验证启动条件', True)
        if context['mode'] == 'browser':
            unpacked = identity.get('unpacked_size')
            limit = BROWSER_LIMIT if browser_limit is None else browser_limit
            if ((browser_limit is None and not identity['browser_eligible']) or len(identity['sources']) != 1 or
                    type(unpacked) is not int or (limit and unpacked > limit) or unpacked < 0 or
                    (limit and sum(asset.get('size', 0) for asset in identity['sources']) > limit) or
                    not settings.get('browser_core')):
                add('browser_not_eligible', '此游戏不符合浏览器载入条件，请使用云端运行', True)
        if self.definitions.get(game.get('platform'), {}).get('arcade'):
            add('arcade_dependencies_unverified', '街机父包、BIOS 与核心版本仍需独立核验')
        verification = (review or {}).get('verification') or {}
        status = verification.get('status', 'unverified')
        if status in {'dependency_error', 'load_failed', 'stale', 'runtime_unavailable'}:
            same_context = (verification.get('mode') == context['mode'] and
                            str(verification.get('engine', '')).casefold() == context['engine'].casefold() and
                            verification.get('core') == context['core'])
            same_build = bool(self.runtime_build and verification.get('runtime_build') == self.runtime_build)
            block = (status == 'dependency_error' and same_context) or (status == 'load_failed' and same_context and same_build)
            text = {'dependency_error': '此前检测发现依赖问题，需核对当前运行方式',
                    'load_failed': '此前启动检测失败；不同核心或构建的结果不能直接套用',
                    'stale': '内容审计记录已过期，当前配置与文件会独立核对',
                    'runtime_unavailable': '审计环境当时缺少运行组件，实际可用性以当前服务为准'}[status]
            add('review_' + status, text, block)
        return issues

    def _view(self, game, document, current_identity, current_signature, review=None, requested_mode=None, *, browser_limit=None):
        if document is None:
            return {'status': 'needs_review', 'configured': False, 'stale': False, 'verified_playable': False,
                    'issues': ['尚未生成游戏启动配置'], 'issue_details': [{'code': 'config_missing', 'message': '尚未生成游戏启动配置', 'blocking': False}],
                    'missing_dependencies': [], 'input_profile': 'default'}
        settings = dict(document['generated']['defaults'])
        overrides = self._validate_overrides(game, document['overrides'])
        settings.update({key: value for key, value in overrides.items() if value is not None})
        context = self._context(game, settings, self._mode(game, settings, requested_mode))
        issues = self._issues(game, current_identity, settings, context, review, browser_limit=browser_limit)
        stale = document['generated']['source_signature'] != current_signature
        if stale:
            issues.insert(0, {'code': 'stale_config', 'message': '游戏文件或运行规则已变化，请重新生成启动配置', 'blocking': True})
        return {**context, 'status': 'blocked' if any(issue['blocking'] for issue in issues) else ('needs_review' if issues else 'configured'),
                'configured': True, 'stale': stale, 'verified_playable': False,
                'source_signature': document['generated']['source_signature'],
                'issues': [issue['message'] for issue in issues], 'issue_details': issues,
                'missing_dependencies': [asset.get('relative_path') or 'invalid_path' for asset in current_identity['sources'] if asset.get('error')],
                'overridden_fields': sorted(overrides), 'input_profiles': sorted(INPUT_PROFILES)}

    def generate(self, game, review=None):
        with self.locked():
            old = self._read(game)
            identity, source_signature = self._snapshot(game)
            generated = {'source_signature': source_signature, 'asset_identity': identity,
                         'defaults': self._defaults(game), 'verified_playable': False}
            document = {'schema_version': VERSION, 'game_id': game['id'], 'generated': generated,
                        'overrides': old['overrides'] if old else {}}
            # No timestamp churn or disk writes on a scan with identical inputs.
            if not old or {key: old.get(key) for key in document} != document:
                document['updated_at'] = datetime.now(timezone.utc).isoformat()
                self._write(game, document)
            else: document = old
            return copy.deepcopy(document)

    def get(self, game):
        with self.locked(): return self._read(game)

    def summary(self, game, review=None):
        with self.locked():
            document = self._read(game)
            identity, current_signature = self._snapshot(game)
            return self._view(game, document, identity, current_signature, review)

    def update_overrides(self, game, patch, review=None):
        changes = self._validate_overrides(game, patch)
        with self.locked():
            document = self._read(game)
            if document is None:
                identity, source_signature = self._snapshot(game)
                document = {'schema_version': VERSION, 'game_id': game['id'],
                            'generated': {'source_signature': source_signature, 'asset_identity': identity,
                                          'defaults': self._defaults(game), 'verified_playable': False}, 'overrides': {}}
            overrides = dict(document['overrides'])
            for key, value in changes.items():
                if value is None: overrides.pop(key, None)
                else: overrides[key] = value
            self._validate_overrides(game, overrides)
            document['overrides'] = overrides
            document['updated_at'] = datetime.now(timezone.utc).isoformat()
            self._write(game, document)
            identity, current_signature = self._snapshot(game)
            return self._view(game, document, identity, current_signature, review)

    def require_download(self, game):
        """Validate enabled/source configuration without requiring an emulator.

        The caller separately verifies a single contained regular ROM. Runtime
        failures, absent cores and BIOS do not make the original file unavailable.
        """
        if self.get(game) is None:
            self.generate(game)
        with self.locked():
            document = self._read(game)
            identity, signature = self._snapshot(game)
            overrides = self._validate_overrides(game, document['overrides'])
            if overrides.get('disabled'):
                raise GameConfigError('manually_disabled', '此游戏已在启动配置中停用')
            if game.get('blocked_reason'):
                raise GameConfigError('catalog_blocked', '游戏资源存在待处理问题，请先检查游戏库')
            if any(asset.get('error') for asset in identity['sources']):
                raise GameConfigError('invalid_sources', '游戏资源缺失或路径不安全')
            if document['generated']['source_signature'] != signature:
                raise GameConfigError('stale_config', '游戏文件或运行规则已变化，请重新扫描游戏库')

    def resolve(self, game, requested_mode=None, review=None, *, browser_limit=None):
        # Only an explicit, separately validated browser experiment may relax
        # the auto rule. This is ephemeral: no schema, signature or saved setting
        # changes. BrowserRuntime.require must validate the installed core,
        # actual file format, dependencies and archive size before this call.
        if browser_limit is not None and (requested_mode != 'browser' or type(browser_limit) is not int or
                                          browser_limit < 0):
            raise GameConfigError('invalid_browser_limit', '浏览器实验模式的载入条件无效', 422)
        # Lazily create configurations for libraries imported before this feature.
        if self.get(game) is None: self.generate(game, review)
        with self.locked():
            document = self._read(game)
            identity, current_signature = self._snapshot(game)
            definition = self.definitions.get(game.get('platform'), {})
            if (browser_limit is not None and definition.get('browser_experimental_only') and
                    game.get('browser_core') is None and
                    document['generated']['defaults'].get('browser_core') is None):
                # An explicitly validated newly available WASM core must work
                # with old catalog/config rows without rewriting their identity,
                # changing auto mode or bypassing stale-source/manual checks.
                document = copy.deepcopy(document)
                document['generated']['defaults']['browser_core'] = definition.get('browser_core')
            view = self._view(game, document, identity, current_signature, review, requested_mode,
                              browser_limit=browser_limit)
            failures = [issue for issue in view.get('issue_details', []) if issue['blocking']]
            if failures:
                raise GameConfigError(failures[0]['code'], failures[0]['message'], summary=view)
            settings = dict(document['generated']['defaults'])
            settings.update({key: value for key, value in document['overrides'].items() if value is not None})
            resolved = dict(game, mode=view['mode'])
            for field in ('native_core', 'browser_core', 'engine_hint', 'input_profile'):
                resolved[field] = settings.get(field)
            resolved['_game_config'] = {key: view[key] for key in ('source_signature', 'mode', 'engine', 'core', 'input_profile', 'status')}
            resolved['verified_playable'] = False
            return resolved
