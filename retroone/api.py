"""RetroOne's small LAN API; emulation is delegated to upstream projects."""
from __future__ import annotations

from contextlib import asynccontextmanager
import html
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Literal
from uuid import UUID
import zipfile
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .artwork import ArtworkCache
from .screenshot_cache import ScreenshotCache
from .catalog import ARCHIVES, BROWSER_LIMIT, Catalog, contained_path, inspect_archive, platform_definitions, public_game
from .profiles import PROFILE_ID, ProfileError, ProfileRegistry
from .presence import PresenceStore
from .network import INSTRUCTIONS, VERIFIED_GAMES, NetworkError, NetworkStore, WorkerGateway
from .settings import DisplaySettings, DisplaySettingsError, StreamResolution, display_view
from .game_metadata import GameMetadataError
from .interface_settings import InterfaceLocale, InterfaceSettings, InterfaceSettingsError
from .library_settings import LibrarySettings, LibrarySettingsError, LibrarySort, SortDirection
from .i18n import emulator_locale, player_messages, resolve_locale, text as translated_text
from .pwa import register_pwa
from .browser_runtime import BrowserRuntime, BrowserRuntimeError
from .torrent_distribution import IceConfigurationError, TorrentDistribution, revision as rom_revision

ROOT = Path(__file__).resolve().parent.parent
MAX_SAVE_SIZE = 32 * 1024 * 1024
DEFAULT_CONTROLS = {0: {index: {'value': key, 'value2': pad} for index, key, pad in [
    (0, 'k', 'BUTTON_2'), (1, 'u', 'BUTTON_4'), (2, 'shift', 'SELECT'), (3, 'enter', 'START'),
    (4, 'w', 'DPAD_UP'), (5, 's', 'DPAD_DOWN'), (6, 'a', 'DPAD_LEFT'), (7, 'd', 'DPAD_RIGHT'),
    (8, 'j', 'BUTTON_1'), (9, 'i', 'BUTTON_3'), (10, 'q', 'LEFT_TOP_SHOULDER'), (11, 'e', 'RIGHT_TOP_SHOULDER'),
    (12, '1', 'LEFT_BOTTOM_SHOULDER'), (13, '3', 'RIGHT_BOTTOM_SHOULDER'), (14, '', 'LEFT_STICK'), (15, '', 'RIGHT_STICK'),
    (16, '', 'LEFT_STICK_X:+1'), (17, '', 'LEFT_STICK_X:-1'), (18, '', 'LEFT_STICK_Y:+1'), (19, '', 'LEFT_STICK_Y:-1'),
    (20, '', 'RIGHT_STICK_X:+1'), (21, '', 'RIGHT_STICK_X:-1'), (22, '', 'RIGHT_STICK_Y:+1'), (23, '', 'RIGHT_STICK_Y:-1'),
]}, 1: {}, 2: {}, 3: {}}
BIOS_FILES = {
    'fds': ('disksys.rom',), 'psx': ('scph5501.bin', 'scph5500.bin', 'scph5502.bin', 'scph1001.bin'),
    'segacd': ('bios_CD_U.bin', 'bios_CD_J.bin', 'bios_CD_E.bin'), 'saturn': ('sega_101.bin', 'mpr-17933.bin', 'saturn_bios.bin'),
    '3do': ('panafz1.bin', 'panafz10.bin'), 'atari5200': ('5200.rom',),
    'pcecd': ('syscard3.pce',), 'pcfx': ('pcfx.rom',), 'colecovision': ('colecovision.rom',),
}


class FavoriteBody(BaseModel):
    favorite: bool


class LaunchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: str | None = None
    replace_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    network_room_id: UUID | None = None
    device_id: UUID | None = None


class StopBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_session_id: str | None = Field(default=None, min_length=1, max_length=128)


class DisplayBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    stream_resolution: StreamResolution


class InterfaceBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    locale: InterfaceLocale


class LibrarySettingsBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    pinned_platforms: list[str] = Field(max_length=128)
    sort: LibrarySort
    direction: SortDirection


class InviteBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    from_profile_id: str
    to_profile_id: str
    game_id: str
    request_id: UUID


class NetworkActorBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: str


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    avatar: Literal['pixel', 'leaf', 'star', 'rocket', 'cat']


class CreateProfileBody(ProfileBody):
    request_id: UUID


class PresenceBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: str
    client_id: UUID
    activity: Literal['library', 'browser', 'stream']
    game_id: str | None = None


class LibraryJobBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['audit']


class GameConfigBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['auto', 'browser', 'stream'] | None = None
    native_core: str | None = None
    browser_core: str | None = None
    engine_hint: str | None = None
    input_profile: Literal['default'] | None = None
    disabled: bool | None = None
    notes: str | None = None


class GameMetadataBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str | None = None
    description: str | None = None
    sort_by: str | None = None


class LeavePresenceBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: str
    client_id: UUID


class MissingRunner:
    def capabilities(self):
        return {'available': False, 'platforms': {}, 'reason': '服务端模拟器组件尚未安装', 'max_sessions': 1, 'target_fps': 60}

    def status(self):
        return None

    def stop(self):
        return None


def create_app(rom_root: Path | None = None, config_root: Path | None = None, *, runner=None, browser_data: Path | None = None, auto_scan: bool | None = None, profile_id: str | None = None, base_path: str | None = None) -> FastAPI:
    rom_root = Path(rom_root or os.getenv('ROM_ROOT', '/roms')).resolve()
    config_root = Path(config_root or os.getenv('CONFIG_ROOT', '/config/retroone')).resolve()
    browser_data = Path(browser_data or os.getenv('EMULATORJS_DATA', str(ROOT / 'vendor/emulatorjs/data'))).resolve()
    profile_id = profile_id or os.getenv('RETROONE_PROFILE_ID', 'player1')
    base_path = (base_path if base_path is not None else os.getenv('RETROONE_BASE_PATH', '')).rstrip('/')
    if not PROFILE_ID.fullmatch(profile_id) or base_path not in {'', f'/p/{profile_id}'}:
        raise ValueError('Invalid configured RetroOne player route')
    profile_registry = ProfileRegistry(config_root,
        control_root=Path(os.environ['RETROONE_PROFILE_CONTROL_ROOT']) if os.getenv('RETROONE_PROFILE_CONTROL_ROOT') else None,
        limit=int(os.getenv('RETROONE_MAX_PROFILES', '16')))
    presence_store = PresenceStore(config_root)
    network_store = NetworkStore(config_root)
    display_settings = DisplaySettings(config_root)
    interface_settings = InterfaceSettings(config_root)
    library_settings = LibrarySettings(config_root)
    worker_gateway = WorkerGateway(os.getenv('RETROONE_COORDINATOR_URL', 'http://retroone:3000'))

    def url(path: str) -> str:
        return base_path + path

    def request_locale(request: Request, *, tolerate_settings_error=False):
        try:
            preference = interface_settings.get()['locale']
        except InterfaceSettingsError:
            if not tolerate_settings_error:
                raise
            # A broken UI preference must not prevent saving a running game.
            preference = 'auto'
        return resolve_locale(preference, request.headers.get('accept-language', ''))

    def request_text(request: Request, key: str):
        return translated_text(request_locale(request, tolerate_settings_error=True), key)

    catalog = Catalog(rom_root, config_root)
    screenshot_cache = ScreenshotCache(catalog.library_data_root / 'metadata/screenshots')
    from .library_management import LibraryManager
    library_manager = LibraryManager(catalog)
    from .game_config import GameConfigError

    def resolved_game(game, requested_mode=None, *, browser_limit=None):
        try:
            return catalog.game_configs.resolve(game, requested_mode, game.get('review'), browser_limit=browser_limit)
        except GameConfigError as error:
            raise HTTPException(error.status_code, str(error)) from error
    artwork = ArtworkCache(config_root, enabled=os.getenv('RETROONE_FETCH_ARTWORK', '1') != '0', rom_root=rom_root)
    if runner is None:
        try:
            from .runner import Runner
            runner = Runner(config_root, rom_root)
        except ImportError:
            runner = MissingRunner()
    runtime_lock = threading.Lock()
    mode_refresh_lock = threading.Lock()
    capability_cache = {'time': 0.0, 'value': {}}
    bios_cache: dict[str, Path] = {}
    missing_bios_until: dict[str, float] = {}
    browser_runtime = BrowserRuntime(rom_root, browser_data)
    torrent_distribution = None
    torrent_init_lock = threading.Lock()
    torrent_bundle = ROOT / 'web/vendor/webtorrent.min.js'
    if not torrent_bundle.is_file():
        torrent_bundle = ROOT / 'container/p2p/dist/webtorrent.min.js'
    mode_refresh = {'time': 0.0, 'scan_finished': None}

    def bios_for(platform: str) -> Path | None:
        cached = bios_cache.get(platform)
        if cached and cached.is_file():
            return cached
        if time.monotonic() < missing_bios_until.get(platform, 0):
            return None
        root = rom_root / 'firmware'
        if not root.is_dir():
            missing_bios_until[platform] = time.monotonic() + 30
            return None
        names = {name.casefold() for name in BIOS_FILES.get(platform, ())}
        if not names:
            return None
        # Root's migration preserves known system firmware. Never inspect archive/source trees.
        for directory, subdirs, files in os.walk(root, followlinks=False):
            subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
            for filename in files:
                if filename.casefold() not in names:
                    continue
                try:
                    path = contained_path(root, Path(directory) / filename)
                    if path.is_file() and path.stat().st_size <= BROWSER_LIMIT:
                        bios_cache[platform] = path
                        missing_bios_until.pop(platform, None)
                        return path
                except (OSError, ValueError):
                    pass
        missing_bios_until[platform] = time.monotonic() + 30
        return None

    def capabilities(force: bool = False) -> dict:
        now = time.monotonic()
        if force or now - capability_cache['time'] > 5:
            try:
                value = runner.capabilities()
            except Exception:
                value = MissingRunner().capabilities()
            capability_cache.update(time=now, value=value)
        return capability_cache['value']

    def refresh_modes():
        now = time.monotonic()
        finished = catalog.scan_status.get('finished_at')
        if now - mode_refresh['time'] < 5 and finished == mode_refresh['scan_finished']:
            return
        # Scan progress can invalidate many rows. Only one request refreshes them;
        # concurrent library/session requests keep reading the previous snapshot.
        if not mode_refresh_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            finished = catalog.scan_status.get('finished_at')
            if now - mode_refresh['time'] < 5 and finished == mode_refresh['scan_finished']:
                return
            bios_ready = {key: bool(bios_for(key)) for key, definition in platform_definitions().items() if definition.get('requires_bios')}
            installed_cores = {p.name.removesuffix('-wasm.data') for p in (browser_data / 'cores').glob('*-wasm.data') if p.is_file()}
            catalog.apply_runtime(capabilities(), (browser_data / 'loader.js').is_file(), bios_ready, installed_cores)
            mode_refresh.update(time=time.monotonic(), scan_finished=finished)
        finally:
            mode_refresh_lock.release()

    def public_view(game: dict) -> dict:
        view = public_game(game)
        if game.get('details', {}).get('screenshots'):
            view['details'] = dict(game['details'], screenshots=[
                url(f"/api/games/{game['id']}/screenshots/{index}") + '?v=' + hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]
                for index, source in enumerate(game['details']['screenshots'])])
        if not view.get('cover_url') and artwork.supports(game):
            view['cover_url'] = f"/api/games/{game['id']}/media/cover"
            view['cover_source'] = 'local-exact-name-reuse' if artwork.local(game) else 'libretro-exact-name-fallback'
        for key in ('cover_url', 'video_url', 'logo_url'):
            if view.get(key):
                view[key] = url(view[key])
        return view

    def public_runtime(value: dict) -> dict:
        return dict(value, stream_url=url('/stream/'), profile_id=profile_id)

    def public_session(value: dict | None) -> dict | None:
        return dict(value, id=value.get('id') or value.get('session_id'), play_url=url('/stream/'), profile_id=profile_id) if value else None

    def session_conflict(active: dict):
        current_game = catalog.get(active.get('game_id', ''))
        raise HTTPException(409, {'code': 'session_conflict', 'message': '已有云游戏正在运行，请选择继续或确认切换',
                                  'session': public_session(active), 'game': public_view(current_game) if current_game else None})

    def game_or_404(game_id: str) -> dict:
        refresh_modes()
        game = catalog.get(game_id)
        if not game:
            raise HTTPException(404, '游戏不存在，请刷新游戏库')
        return game

    def browser_rom(game: dict) -> Path:
        if not game.get('browser_eligible') or not game.get('browser_core'):
            raise HTTPException(403, '该游戏不允许下载到浏览器，请使用云游戏模式')
        if not (browser_data / 'cores' / f"{game['browser_core']}-wasm.data").is_file():
            raise HTTPException(409, '当前浏览器模拟器没有该平台内核，请使用云游戏模式')
        try:
            path = contained_path(rom_root / 'library', Path(game['rom_path']))
            if not path.is_file() or path.stat().st_size > BROWSER_LIMIT:
                raise ValueError('游戏大小已改变')
            if path.suffix.lower() in ARCHIVES:
                actual, error = inspect_archive(path)
                if error or actual is None or actual > BROWSER_LIMIT:
                    raise ValueError('压缩包解压大小超限或无法安全验证')
            elif path.stat().st_size > BROWSER_LIMIT:
                raise ValueError('游戏大小超限')
            return path
        except (ValueError, OSError) as error:
            raise HTTPException(403, str(error)) from error

    def browser_payload(game: dict, path: Path) -> Path:
        """Normalize a single cartridge member to ASCII for legacy GBK ZIP names.

        Arcade packages keep their original archive/shortname; their ROM-set
        structure and dependency names are meaningful to the upstream core.
        """
        definition = platform_definitions().get(game['platform'], {})
        if path.suffix.lower() != '.zip' or definition.get('arcade'):
            return path
        extensions = set(definition.get('extensions', [])) - ARCHIVES - {'.exe', '.com', '.bat'}
        try:
            with zipfile.ZipFile(path) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir() and Path(entry.filename).suffix.lower() in extensions]
                if len(entries) != 1:
                    return path
                entry = entries[0]
                identity = f"{path.stat().st_size}-{path.stat().st_mtime_ns}"
                destination = config_root / 'browser-roms' / identity / f"{game['id']}{Path(entry.filename).suffix.lower()}"
                if destination.is_file() and destination.stat().st_size == entry.file_size:
                    return destination
                with archive.open(entry) as source:
                    data = source.read(BROWSER_LIMIT + 1)
                if len(data) > BROWSER_LIMIT or len(data) != entry.file_size:
                    raise ValueError('ROM 解压大小不符合限制')
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(f'.{time.time_ns()}.tmp')
                try:
                    temporary.write_bytes(data)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
                return destination
        except (ValueError, OSError, zipfile.BadZipFile) as error:
            raise HTTPException(409, 'ROM 无法安全解压：' + str(error)) from error

    def experiment_bios(game):
        if game['platform'] != 'nds':
            return bios_for(game['platform'])
        cached = bios_cache.get('nds-package')
        if cached and cached.is_file():
            return cached
        if time.monotonic() < missing_bios_until.get('nds-package', 0):
            return None
        names = {'bios7.bin', 'bios9.bin', 'firmware.bin'}
        files = {}
        root = rom_root / 'firmware'
        for directory, subdirs, filenames in os.walk(root, followlinks=False):
            subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
            for filename in filenames:
                if filename.lower() in names and filename.lower() not in files:
                    try:
                        path = contained_path(root, Path(directory) / filename)
                        if path.is_file() and 0 < path.stat().st_size <= BROWSER_LIMIT:
                            files[filename.lower()] = path
                    except (OSError, ValueError):
                        pass
            if len(files) == len(names):
                break
        if len(files) != len(names):
            missing_bios_until['nds-package'] = time.monotonic() + 30
            return None
        identity = hashlib.sha256(''.join(name + rom_revision(files[name]) for name in sorted(files)).encode()).hexdigest()
        destination = config_root / 'browser-firmware' / (identity + '.zip')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f'.{time.time_ns()}.tmp')
        try:
            with zipfile.ZipFile(temporary, 'w') as archive:
                for name in sorted(files):
                    archive.write(files[name], name)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        bios_cache['nds-package'] = destination
        return destination

    def experiment_game(game_id):
        game = game_or_404(game_id)
        browser_runtime.require(game)
        game = resolved_game(game, 'browser', browser_limit=browser_runtime.max_bytes)
        path = browser_runtime.require(game)
        if (platform_definitions().get(game['platform'], {}).get('requires_bios') or game['platform'] == 'nds') and not experiment_bios(game):
            raise HTTPException(409, '浏览器模拟器所需 BIOS 尚未齐全；DS 需要 bios7.bin、bios9.bin 和 firmware.bin')
        return game, path

    def torrents():
        nonlocal torrent_distribution
        if not torrent_bundle.is_file() or os.getenv('RETROONE_P2P_ENABLED', '1') == '0':
            raise HTTPException(503, 'WebTorrent 组件尚未启用，请选择 HTTP 下载或云游戏')
        with torrent_init_lock:
            if torrent_distribution is None:
                try:
                    torrent_distribution = TorrentDistribution(catalog.library_data_root)
                except IceConfigurationError as error:
                    raise HTTPException(503, str(error)) from error
        return torrent_distribution

    @asynccontextmanager
    async def lifespan(app):
        startup_scan = auto_scan
        if startup_scan is None:
            policy = os.getenv('RETROONE_SCAN_ON_STARTUP', 'auto').strip().lower()
            if policy not in {'auto', '1', 'true', 'always', '0', 'false', 'never'}:
                raise ValueError('RETROONE_SCAN_ON_STARTUP must be auto, 1 or 0')
            startup_scan = policy in {'1', 'true', 'always'} or (policy == 'auto' and not catalog.has_games())
        if startup_scan:
            catalog.begin_scan()
        yield
        # The service only owns this Runner instance and its child process.
        runner.stop()
        artwork.close()

    app = FastAPI(title='RetroOne', version='0.1.0', lifespan=lifespan)
    app.state.catalog = catalog
    app.state.screenshot_cache = screenshot_cache
    app.state.runner = runner
    app.state.profile_id = profile_id
    app.state.profile_registry = profile_registry
    app.state.presence = presence_store
    app.state.network = network_store
    app.state.worker_gateway = worker_gateway
    app.state.display_settings = display_settings
    app.state.interface_settings = interface_settings
    app.state.library_settings = library_settings
    app.state.browser_runtime = browser_runtime

    @app.exception_handler(BrowserRuntimeError)
    async def browser_runtime_error(request: Request, error: BrowserRuntimeError):
        return JSONResponse({'detail': str(error)}, status_code=error.status_code)

    @app.exception_handler(ProfileError)
    async def profile_error(request: Request, error: ProfileError):
        return JSONResponse({'detail': str(error)}, status_code=error.status_code)

    @app.exception_handler(NetworkError)
    async def network_error(request: Request, error: NetworkError):
        return JSONResponse({'detail': error.detail}, status_code=error.status_code)

    @app.exception_handler(DisplaySettingsError)
    async def display_settings_error(request: Request, error: DisplaySettingsError):
        return JSONResponse({'detail': str(error)}, status_code=error.status_code)

    @app.exception_handler(InterfaceSettingsError)
    async def interface_settings_error(request: Request, error: InterfaceSettingsError):
        # The store may be locked or corrupt. Never retry its synchronous read
        # from this async handler, which would delay the response/event loop.
        locale = resolve_locale('auto', request.headers.get('accept-language', ''))
        return JSONResponse({'detail': translated_text(locale, error.code)}, status_code=error.status_code)

    @app.exception_handler(LibrarySettingsError)
    async def library_settings_error(request: Request, error: LibrarySettingsError):
        return JSONResponse({'detail': str(error)}, status_code=error.status_code)

    @app.middleware('http')
    async def origin_and_headers(request: Request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if (origin and urlsplit(origin).netloc != request.headers.get('host')) or request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '请从 RetroOne 本站操作'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path in {'/', '/index.html', '/app.js', '/styles.css', '/i18n.js', '/i18n-en.js', '/library-ui.js'}:
            response.headers['Cache-Control'] = 'no-cache, max-age=0, must-revalidate'
        if request.url.path in {'/api/settings/interface', '/api/settings/display', '/api/settings/library'}:
            response.headers['Cache-Control'] = 'no-store'
        if not request.url.path.startswith('/stream/'):
            response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
            response.headers['Cross-Origin-Embedder-Policy'] = 'require-corp'
            response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
        return response

    @app.get('/api/profiles')
    def profiles():
        snapshot = profile_registry.snapshot()
        active = presence_store.snapshot()
        for record in snapshot['profiles']:
            presence = active.get(record['id'], PresenceStore.offline())
            game = catalog.get(presence['game_id']) if presence.get('game_id') else None
            record['presence'] = dict(presence, game_title=game['title'] if game else None)
        return dict(snapshot, current_profile_id=profile_id)

    @app.post('/api/presence')
    def heartbeat(body: PresenceBody):
        records = profile_registry.snapshot()['profiles']
        record = next((item for item in records if item['id'] == body.profile_id), None)
        if not record:
            raise HTTPException(404, '玩家档案不存在')
        if record['status'] != 'ready':
            raise HTTPException(409, '玩家环境尚未准备好')
        if body.activity == 'library':
            game_id = None
        elif not body.game_id or not catalog.get(body.game_id):
            raise HTTPException(422, '正在游玩的游戏不存在')
        else:
            game_id = body.game_id
        return presence_store.heartbeat(body.profile_id, str(body.client_id), body.activity, game_id)

    @app.post('/api/presence/leave')
    def leave_presence(body: LeavePresenceBody):
        return presence_store.leave(body.profile_id, str(body.client_id))

    @app.post('/api/profiles', status_code=202)
    def add_profile(body: CreateProfileBody):
        return profile_registry.create(body.name, body.avatar, str(body.request_id))

    @app.post('/api/profiles/{identifier}/retry', status_code=202)
    def retry_profile(identifier: str):
        return profile_registry.retry(identifier)

    @app.patch('/api/profiles/{identifier}')
    def update_profile(identifier: str, body: ProfileBody):
        return profile_registry.update(identifier, body.name, body.avatar)

    def network_profiles():
        return {item['id']: item for item in profile_registry.snapshot()['profiles']}

    def require_network_profile(identifier):
        item = network_profiles().get(identifier)
        if not item or item['status'] != 'ready':
            raise NetworkError('玩家不存在或运行环境尚未准备好', 409, 'profile_not_ready', profile_id=identifier)
        return item

    def require_network_idle(identifier):
        active = runner.status() if identifier == profile_id else worker_gateway.session(identifier)
        if active:
            raise NetworkError('该成员已有云游戏运行，请先由本人继续游戏或确认结束', 409, 'profile_busy', profile_id=identifier,
                               session=public_session(active) if identifier == profile_id else active)

    def verified_network_game(game_id):
        game = catalog.get(game_id) if game_id in VERIFIED_GAMES else None
        if not game:
            raise NetworkError('这款游戏尚未通过双实例原生联机验收', 409, 'network_game_unverified')
        if game.get('blocked_reason') or not capabilities().get('platforms', {}).get('psp', {}).get('available'):
            raise NetworkError('联机游戏的运行环境暂时不可用', 409, 'network_runtime_unavailable')
        return game

    def public_invite(value):
        people = network_profiles()
        result = {key: item for key, item in value.items() if key != 'request_id'}
        result['game_title'] = VERIFIED_GAMES.get(value['game_id'], '')
        result['room_status'] = network_store.room(value['room_id'])['status'] if value.get('room_id') else None
        for key, field in [('from', 'from_profile_id'), ('to', 'to_profile_id')]:
            person = people.get(value[field], {})
            result[key] = {name: person.get(name) for name in ('id', 'name', 'avatar')}
        return result

    def public_room(value):
        people = network_profiles()
        members = []
        for field, role in [('host_profile_id', 'host'), ('guest_profile_id', 'guest')]:
            identifier = value[field]
            person = people.get(identifier, {})
            members.append({'profile_id': identifier, 'name': person.get('name', identifier), 'avatar': person.get('avatar', 'pixel'),
                            'role': role, 'play_url': f'/p/{identifier}/stream/'})
        return dict(value, game_title=VERIFIED_GAMES.get(value['game_id'], ''), members=members, instructions=INSTRUCTIONS)

    @app.get('/api/network/capabilities')
    def network_capabilities():
        available = capabilities().get('platforms', {}).get('psp', {}).get('available', False)
        games = [{'game_id': identifier, 'title': name, 'max_players': 2, 'instructions': INSTRUCTIONS}
                 for identifier, name in VERIFIED_GAMES.items()
                 if available and (game := catalog.get(identifier)) and not game.get('blocked_reason')]
        return {'available': bool(games), 'games': games, 'invite_ttl_seconds': NetworkStore.INVITE_TTL,
                'reason': None if games else '已验证的联机游戏或运行环境暂时不可用'}

    @app.get('/api/network/invites')
    def invitations(profile_id: str = Query(..., max_length=30)):
        require_network_profile(profile_id)
        return {'invites': [public_invite(item) for item in network_store.list(profile_id)]}

    @app.post('/api/network/invites', status_code=201)
    def invite(body: InviteBody):
        verified_network_game(body.game_id)
        for identifier in (body.from_profile_id, body.to_profile_id):
            require_network_profile(identifier)
        if body.from_profile_id == body.to_profile_id:
            raise NetworkError('请选择另一位家人', 422, 'same_profile')
        existing = network_store.existing_request(str(body.request_id), body.from_profile_id, body.to_profile_id, body.game_id)
        if existing:
            return public_invite(network_store.invite(existing['id']))
        if not presence_store.snapshot().get(body.to_profile_id, {}).get('online'):
            raise NetworkError('这位家人目前不在线，请等对方打开游戏库', 409, 'profile_offline', profile_id=body.to_profile_id)
        for identifier in (body.from_profile_id, body.to_profile_id):
            require_network_idle(identifier)
        return public_invite(network_store.create(body.from_profile_id, body.to_profile_id, body.game_id, str(body.request_id)))

    @app.post('/api/network/invites/{identifier}/accept')
    def accept_invite(identifier: str, body: NetworkActorBody):
        current = network_store.invite(identifier)
        if body.profile_id != current['to_profile_id']:
            raise NetworkError('只有受邀成员可以接受', 403, 'wrong_invite_member')
        if current['status'] == 'pending':
            for member in (current['from_profile_id'], current['to_profile_id']):
                require_network_profile(member)
                require_network_idle(member)
        accepted = network_store.respond(identifier, body.profile_id, 'accept')
        return {'invite': public_invite(accepted), 'room': public_room(network_store.room(accepted['room_id']))}

    @app.post('/api/network/invites/{identifier}/decline')
    def decline_invite(identifier: str, body: NetworkActorBody):
        return public_invite(network_store.respond(identifier, body.profile_id, 'decline'))

    @app.post('/api/network/invites/{identifier}/cancel')
    def cancel_invite(identifier: str, body: NetworkActorBody):
        return public_invite(network_store.respond(identifier, body.profile_id, 'cancel'))

    @app.get('/api/network/rooms/{identifier}')
    def get_network_room(identifier: str):
        return public_room(network_store.room(identifier))

    @app.post('/api/network/rooms/{identifier}/close')
    def close_network_room(identifier: str, body: NetworkActorBody):
        return public_room(network_store.close(identifier, body.profile_id))

    def network_launch(game_id, room_id):
        room = public_room(network_store.room(room_id)) if profile_id == 'player1' else worker_gateway.room(room_id)
        if room.get('id') != room_id or room.get('game_id') != game_id or game_id not in VERIFIED_GAMES:
            raise NetworkError('联机房间与游戏不匹配', 409, 'room_game_mismatch')
        if room.get('status') != 'ready' or not isinstance(room.get('expires_at'), (float, int)) or room['expires_at'] <= time.time():
            raise NetworkError('联机房间已关闭或过期，请重新邀请', 409, 'room_not_ready')
        role = 'host' if profile_id == room.get('host_profile_id') else 'guest' if profile_id == room.get('guest_profile_id') else None
        member = next((item for item in room.get('members', []) if item.get('profile_id') == profile_id), None)
        if not role or not member:
            raise NetworkError('当前玩家不属于此联机房间', 403, 'wrong_room_member')
        return {'room_id': room_id, 'host_profile_id': room['host_profile_id'], 'role': role, 'nickname': member['name']}

    @app.get('/api/health')
    def health():
        refresh_modes()
        return {'status': 'ok', 'library': {'count': catalog.count(), 'scan': dict(catalog.scan_status)},
                'scan': dict(catalog.scan_status), 'runtime': public_runtime(capabilities()), 'profile_id': profile_id, 'base_path': base_path,
                'browser': {'available': (browser_data / 'loader.js').is_file(), 'limit_bytes': BROWSER_LIMIT, 'engine': 'EmulatorJS', 'cores': sorted(p.name.removesuffix('-wasm.data') for p in (browser_data / 'cores').glob('*-wasm.data') if '-thread-' not in p.name and '-legacy-' not in p.name)},
                'target_fps': 60}

    def current_display_settings(device_id: UUID | None = None):
        provider = getattr(runner, 'display_capabilities', None)
        runtime = provider() if provider else {'available': False, 'reason': '云端分辨率设置尚未就绪'}
        selected = display_settings.selection(device_id)
        return dict(display_view(selected['settings'], runner.status(), runtime),
                    **{key: value for key, value in selected.items() if key != 'settings'}, profile_id=profile_id)

    @app.get('/api/settings/display')
    def get_display_settings(device_id: UUID | None = Query(None)):
        return current_display_settings(device_id)

    @app.patch('/api/settings/display')
    def update_display_settings(body: DisplayBody, device_id: UUID | None = Query(None)):
        display_settings.update(body.model_dump(), device_id)
        return current_display_settings(device_id)

    @app.delete('/api/settings/display')
    def reset_device_display_settings(device_id: UUID = Query(...)):
        display_settings.reset_device(device_id)
        return current_display_settings(device_id)

    @app.get('/api/settings/interface')
    def get_interface_settings():
        return interface_settings.view()

    @app.put('/api/settings/interface')
    def update_interface_settings(body: InterfaceBody):
        settings = interface_settings.update(body.model_dump())
        return {'settings': settings, 'supported_locales': ['zh-CN', 'en']}

    @app.get('/api/settings/library')
    def get_library_settings():
        return library_settings.view()

    @app.put('/api/settings/library')
    def update_library_settings(body: LibrarySettingsBody):
        library_settings.update(body.model_dump())
        return library_settings.view()

    @app.get('/api/platforms')
    def platforms():
        refresh_modes()
        return {'platforms': catalog.platforms()}

    @app.get('/api/games')
    def games(q: str = Query('', max_length=200), platform: str = Query('', max_length=60), mode: str = Query('', pattern='^(browser|stream|unavailable)?$'), favorite: bool = False, recent: bool = False, review_filter: str = Query('', pattern='^(all|needs_attention|pending|observed)?$'), offset: int = Query(0, ge=0), limit: int = Query(60, ge=1, le=200), sort: LibrarySort = 'default', direction: SortDirection | None = None):
        refresh_modes()
        result = catalog.list(q=q.strip(), platform=platform, mode=mode, favorite=favorite, recent=recent, review_filter=review_filter, offset=offset, limit=limit, sort=sort, direction=direction)
        result['games'] = [public_view(game) for game in result['games']]
        return result

    @app.get('/api/games/{game_id}')
    def game_detail(game_id: str):
        return public_view(game_or_404(game_id))

    @app.get('/api/games/{game_id}/neighbors')
    def game_neighbors(game_id: str, q: str = Query('', max_length=200), platform: str = Query('', max_length=60),
                       mode: str = Query('', pattern='^(browser|stream|unavailable)?$'), favorite: bool = False,
                       recent: bool = False, review_filter: str = Query('', pattern='^(all|needs_attention|pending|observed)?$'),
                       sort: LibrarySort = 'default', direction: SortDirection | None = None):
        game_or_404(game_id)
        return catalog.neighbors(game_id, q=q.strip(), platform=platform, mode=mode, favorite=favorite,
                                 recent=recent, review_filter=review_filter, sort=sort, direction=direction)

    @app.post('/api/games/{game_id}/favorite')
    def favorite(game_id: str, body: FavoriteBody):
        game_or_404(game_id)
        catalog.set_favorite(game_id, body.favorite)
        return {'id': game_id, 'favorite': body.favorite}

    @app.get('/api/games/{game_id}/configuration')
    def game_configuration(game_id: str):
        return game_or_404(game_id).get('launch_config')

    @app.get('/api/games/{game_id}/metadata')
    def game_metadata(game_id: str):
        game_or_404(game_id)
        return catalog.game_metadata.summary(game_id)

    @app.patch('/api/games/{game_id}/metadata')
    def update_game_metadata(game_id: str, body: GameMetadataBody):
        game_or_404(game_id)
        try:
            return catalog.game_metadata.update_overrides(game_id, body.model_dump(exclude_unset=True))
        except GameMetadataError as error:
            raise HTTPException(error.status_code, str(error)) from error

    @app.patch('/api/games/{game_id}/configuration')
    def update_game_configuration(game_id: str, body: GameConfigBody):
        game = game_or_404(game_id)
        try:
            return catalog.game_configs.update_overrides(game, body.model_dump(exclude_unset=True), game.get('review'))
        except GameConfigError as error:
            raise HTTPException(error.status_code, str(error)) from error

    @app.get('/api/games/{game_id}/launch-options')
    def launch_options(game_id: str):
        game = game_or_404(game_id)
        options = []
        for mode in ('auto', 'stream'):
            reason = None
            try:
                resolved = resolved_game(game, mode)
                if resolved['mode'] == 'browser':
                    browser_rom(resolved)
                    if platform_definitions().get(game['platform'], {}).get('requires_bios') and not bios_for(game['platform']):
                        raise HTTPException(409, '浏览器模拟器所需 BIOS 尚未齐全')
                else:
                    runtime = capabilities()
                    engine = runtime.get('engines', {}).get(resolved.get('engine_hint'), {}) if resolved.get('engine_hint') else runtime.get('platforms', {}).get(game['platform'], {})
                    if not engine.get('available'):
                        raise HTTPException(409, engine.get('reason') or '服务端尚未配置该平台模拟器')
            except HTTPException as error:
                reason = str(error.detail)
            options.append({'id': mode, 'available': reason is None, 'reason': reason, 'requires_threads': False, 'size_bytes': game.get('size')})
        experimental = browser_runtime.inspect(game)
        if experimental['available']:
            try:
                experiment_game(game_id)
            except (HTTPException, BrowserRuntimeError) as error:
                experimental.update(available=False, reason=str(error.detail) if isinstance(error, HTTPException) else str(error))
        options.append(dict(experimental, id='browser-http'))
        p2p = dict(experimental, id='browser-p2p')
        if p2p['available'] and (not torrent_bundle.is_file() or os.getenv('RETROONE_P2P_ENABLED', '1') == '0'):
            p2p.update(available=False, reason='WebTorrent 组件尚未启用，请选择 HTTP 下载')
        options.append(p2p)
        return JSONResponse({'options': options, 'max_bytes': browser_runtime.max_bytes}, headers={'Cache-Control': 'no-store'})

    @app.get('/vendor/webtorrent.min.js')
    def webtorrent_client():
        if not torrent_bundle.is_file():
            raise HTTPException(404, 'WebTorrent 组件尚未安装')
        return FileResponse(torrent_bundle, media_type='text/javascript', headers={'Cache-Control': 'public, max-age=3600'})

    @app.get('/vendor/rom-chunk-store.js')
    def torrent_chunk_store():
        path = torrent_bundle.parent / 'rom-chunk-store.js'
        if not path.is_file():
            raise HTTPException(404, 'WebTorrent 存储组件尚未安装')
        return FileResponse(path, media_type='text/javascript', headers={'Cache-Control': 'public, max-age=3600'})

    @app.get('/api/games/{game_id}/browser-manifest')
    def browser_manifest(game_id: str, transport: Literal['http', 'p2p'] = 'http'):
        game, path = experiment_game(game_id)
        state = browser_runtime.inspect(game)
        current = rom_revision(path)
        name = game_id + path.suffix.lower()
        download_url = url(f'/api/games/{game_id}/browser-rom/{name}') + '?revision=' + current
        result = {'name': name, 'size': state['size_bytes'], 'revision': current, 'download_url': download_url}
        if transport == 'p2p':
            try:
                result.update(torrents().manifest(path, name, download_url=download_url, expected_revision=current))
            except (OSError, ValueError) as error:
                raise HTTPException(409, '无法准备游戏分片，请刷新后重试') from error
        result.update(requires_threads=state['requires_threads'], requires_webgl2=state['requires_webgl2'], unpacked_bytes=state['unpacked_bytes'])
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.api_route('/api/games/{game_id}/browser-rom/{filename}', methods=['GET', 'HEAD'])
    def experimental_rom(game_id: str, filename: str, revision: str = Query(..., pattern='^[a-f0-9]{64}$')):
        _, path = experiment_game(game_id)
        if filename != game_id + path.suffix.lower():
            raise HTTPException(404, '文件不存在')
        if rom_revision(path) != revision:
            raise HTTPException(409, '游戏文件已变化，请刷新后重新下载')
        return FileResponse(path, media_type='application/octet-stream', headers={'Cache-Control': 'private, no-store'})

    @app.post('/api/games/{game_id}/launch')
    def launch(game_id: str, body: LaunchBody | None = None):
        requested_mode = body.mode if body else None
        experimental = requested_mode in {'browser-http', 'browser-p2p'}
        if experimental:
            game, _ = experiment_game(game_id)
            if requested_mode == 'browser-p2p':
                torrents()
        else:
            game = resolved_game(game_or_404(game_id), requested_mode)
        room_id = str(body.network_room_id) if body and body.network_room_id else None
        if room_id and body.replace_session_id:
            raise HTTPException(422, '联机启动不会自动替换游戏，请先明确结束当前会话')
        requested = game['mode']
        if room_id and requested != 'stream':
            raise HTTPException(422, '此联机游戏需要独立云端实例')
        if room_id:
            game = dict(game, network=network_launch(game_id, room_id))
        if requested not in {'browser', 'stream'}:
            raise HTTPException(409, game.get('reason') or '该平台尚未配置运行环境')
        if requested == 'browser':
            if not experimental:
                browser_rom(game)
            if not (browser_data / 'loader.js').is_file():
                raise HTTPException(503, '浏览器模拟器资源尚未安装')
            if platform_definitions().get(game['platform'], {}).get('requires_bios') and not bios_for(game['platform']):
                raise HTTPException(409, '游戏需要 BIOS；已保留游戏，待配置对应固件')
            catalog.mark_played(game_id)
            query = '?experimental=true&transport=' + ('p2p' if requested_mode == 'browser-p2p' else 'http') if experimental else ''
            return {'mode': 'browser', 'play_url': url(f'/play/{game_id}') + query, 'profile_id': profile_id}
        if game.get('blocked_reason'):
            raise HTTPException(409, game['blocked_reason'])
        runtime = capabilities(force=True)
        engine = runtime.get('engines', {}).get(game.get('engine_hint'), {}) if game.get('engine_hint') else runtime.get('platforms', {}).get(game['platform'], {})
        if not engine.get('available'):
            raise HTTPException(409, engine.get('reason') or '服务端尚未配置该平台模拟器')
        try:
            if hasattr(runner, 'preflight'):
                runner.preflight(game)
            for filename in game.get('launch_files') or [game['rom_path']]:
                if not contained_path(rom_root / 'library', Path(filename)).is_file():
                    raise ValueError('游戏文件已不存在，请重新扫描游戏库')
        except (OSError, ValueError) as error:
            raise HTTPException(409, str(error)) from error
        with runtime_lock:
            active = runner.status()
            if active:
                replacement = body.replace_session_id if body else None
                identifier = active.get('id') or active.get('session_id')
                if active.get('game_id') == game_id and (not room_id or (active.get('network') or {}).get('room_id') == room_id):
                    return {'mode': 'stream', 'play_url': url('/stream/'), 'session_id': identifier, 'session': public_session(active), 'profile_id': profile_id}
                if room_id or not replacement or replacement != identifier:
                    session_conflict(active)
            # Preferences are a server-owned snapshot for a new session only.
            # Read before ending a confirmed old session so corrupt preferences
            # cannot turn an otherwise valid switch into an avoidable stop.
            game = dict(game, _display=display_settings.launch_snapshot(body.device_id if body else None))
            if active:
                runner.stop()
            try:
                session = runner.start(game)
            except (RuntimeError, ValueError, FileNotFoundError) as error:
                raise HTTPException(409, str(error)) from error
            catalog.mark_played(game_id)
            return {'mode': 'stream', 'play_url': url('/stream/'), 'session_id': session.get('id') or session.get('session_id'), 'session': public_session(session), 'profile_id': profile_id}

    @app.get('/api/session')
    def session():
        active = runner.status()
        game = catalog.get(active.get('game_id', '')) if active else None
        return {'session': public_session(active), 'game': public_view(game) if game else None, 'runtime': public_runtime(capabilities()), 'profile_id': profile_id}

    @app.post('/api/session/stop')
    def stop_session(body: StopBody | None = None):
        with runtime_lock:
            active = runner.status()
            if active and body and body.expected_session_id and body.expected_session_id != (active.get('id') or active.get('session_id')):
                session_conflict(active)
            result = runner.stop() if active else False
        return {'session': None, 'stopped': result}

    @app.post('/api/scan', status_code=202)
    def scan():
        started = catalog.begin_scan()
        return {'started': started, 'scan': dict(catalog.scan_status)}

    @app.get('/api/library-management')
    def library_management():
        return library_manager.view()

    @app.post('/api/library-management/jobs', status_code=202)
    def library_job(body: LibraryJobBody):
        try:
            return library_manager.start()
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get('/api/migration')
    def migration():
        for path in [rom_root / '.retroone/migration-status.json', rom_root / '.retroone/progress.json', rom_root / 'migration-progress.json', config_root / 'migration-progress.json', rom_root / '.migration/progress.json']:
            if path.is_file():
                try:
                    if path.stat().st_size < 1024 * 1024:
                        return json.loads(path.read_text())
                except (OSError, ValueError):
                    pass
        return {'available': False, 'message': '迁移进度文件尚未生成'}

    @app.api_route('/api/games/{game_id}/rom/{filename}', methods=['GET', 'HEAD'])
    def rom(game_id: str, filename: str):
        game = game_or_404(game_id)
        path = browser_rom(game)
        prepared = browser_payload(game, path)
        if filename not in {path.name, prepared.name}:
            raise HTTPException(404, '文件不存在')
        return FileResponse(prepared, media_type='application/octet-stream', headers={'Cache-Control': 'private, max-age=3600'})

    @app.get('/api/games/{game_id}/screenshots/{index}')
    def screenshot(game_id: str, index: int, v: str | None = Query(None, pattern='^[a-f0-9]{16}$')):
        game = game_or_404(game_id)
        screenshots = game.get('details', {}).get('screenshots', [])
        if not 0 <= index < len(screenshots):
            raise HTTPException(404, '游戏截图不存在')
        if v and v != hashlib.sha256(screenshots[index].encode('utf-8')).hexdigest()[:16]:
            raise HTTPException(404, '游戏截图资料已更新，请刷新页面')
        cached = screenshot_cache.get(screenshots[index])
        if not cached:
            raise HTTPException(404, '游戏截图暂时无法读取')
        path, media_type = cached
        return FileResponse(path, media_type=media_type, headers={'Cache-Control': 'private, max-age=86400'})

    @app.get('/api/games/{game_id}/media/{kind}')
    def media(game_id: str, kind: str):
        if kind not in {'cover', 'logo', 'video'}:
            raise HTTPException(404)
        game = game_or_404(game_id)
        value = game.get('media', {}).get(kind)
        if not value:
            cached = artwork.get(game) if kind == 'cover' else None
            if cached:
                return FileResponse(cached, headers={'Cache-Control': 'public, max-age=604800'})
            raise HTTPException(404, '未找到精确匹配的封面')
        try:
            path = contained_path(rom_root / 'library', Path(value))
            if not path.is_file() or path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.mp4', '.webm'}:
                raise ValueError()
        except (ValueError, OSError):
            raise HTTPException(404)
        return FileResponse(path, headers={'Cache-Control': 'public, max-age=86400'})

    @app.get('/api/games/{game_id}/bios')
    def bios(game_id: str, experimental: bool = False):
        if experimental:
            game, _ = experiment_game(game_id)
        else:
            game = game_or_404(game_id)
            browser_rom(game)
        path = experiment_bios(game) if experimental else bios_for(game['platform'])
        if not path:
            raise HTTPException(404, '未找到对应 BIOS')
        return FileResponse(path, media_type='application/octet-stream')

    def save_path(game_id: str) -> Path:
        game_or_404(game_id)
        return config_root / 'browser-saves' / f'{game_id}.state'

    @app.get('/api/games/{game_id}/save-state')
    def download_save(game_id: str, request: Request):
        path = save_path(game_id)
        if not path.is_file():
            raise HTTPException(404, request_text(request, 'save_not_found'))
        return FileResponse(path, media_type='application/octet-stream', filename=f'{game_id}.state', headers={'Cache-Control': 'no-store'})

    @app.put('/api/games/{game_id}/save-state')
    async def upload_save(game_id: str, request: Request):
        path = save_path(game_id)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_SAVE_SIZE:
                raise HTTPException(413, request_text(request, 'save_too_large'))
        if not data:
            raise HTTPException(400, request_text(request, 'save_empty'))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f'.{time.time_ns()}.tmp')
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return {'saved': True, 'size': len(data)}

    @app.get('/play/{game_id}', response_class=HTMLResponse)
    def play(game_id: str, request: Request, restore: bool = False, experimental: bool = False, transport: Literal['http', 'p2p'] = 'http'):
        locale = request_locale(request)
        messages = player_messages(locale)
        if experimental:
            game, path = experiment_game(game_id)
            if transport == 'p2p':
                torrents()
        else:
            game = resolved_game(game_or_404(game_id), 'browser')
            path = browser_rom(game)
        if not (browser_data / 'loader.js').is_file():
            raise HTTPException(503, messages['browser_resources_missing'])
        if not experimental:
            path = browser_payload(game, path)
        bios_path = experiment_bios(game) if experimental else bios_for(game['platform'])
        if platform_definitions().get(game['platform'], {}).get('requires_bios') and not bios_path:
            raise HTTPException(409, messages['browser_bios_missing'])
        # Player1 retains the historical storage keys, including already-created
        # EmulatorJS battery saves. Other players get separate personal DBs below.
        save_title = game.get('_save_title', game['title'])
        storage_game_name = save_title if profile_id == 'player1' else f"{profile_id}_{game_id}_{save_title}"
        settings = {'EJS_player': '#game', 'EJS_core': game['browser_core'], 'EJS_gameUrl': url(f'/api/games/{game_id}/rom/{quote(path.name)}'),
                    'EJS_pathtodata': url('/emulatorjs/data/'), 'EJS_gameName': storage_game_name, 'EJS_gameID': int(game_id[:12], 16),
                    'EJS_defaultControls': DEFAULT_CONTROLS, 'EJS_language': emulator_locale(locale), 'EJS_color': '#64d9f3', 'EJS_backgroundColor': '#0b1424',
                    'EJS_startOnLoaded': False, 'EJS_startButtonName': messages['start_game'], 'EJS_defaultOptions': {'save-state-location': 'download'}}
        if bios_path:
            settings['EJS_biosUrl'] = url(f'/api/games/{game_id}/bios') + ('?experimental=true' if experimental else '')
        if experimental:
            settings['EJS_CacheLimit'] = 0
        if restore and save_path(game_id).is_file():
            settings['EJS_loadStateURL'] = url(f'/api/games/{game_id}/save-state')
        settings_json = json.dumps(settings, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        messages_json = json.dumps(messages, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        title = html.escape(game['title'])
        home_url, play_url, save_url = url('/'), url(f'/play/{game_id}'), url(f'/api/games/{game_id}/save-state')
        restore_url = play_url + ('?experimental=true&transport=' + transport + '&restore=true' if experimental else '?restore=true')
        storage_namespace = '' if profile_id == 'player1' else f'RetroOne-{profile_id}-'
        bridge = ROOT / 'web' / 'emulator-input.js'
        bridge_version = hashlib.sha256(bridge.read_bytes()).hexdigest()[:16] if bridge.is_file() else 'pending'
        runtime_script = f'<script src="{url("/emulatorjs/data/loader.js")}"></script>'
        if experimental:
            config = {'transport': transport, 'manifestUrl': url(f'/api/games/{game_id}/browser-manifest') + '?transport=' + transport,
                      'loaderUrl': url('/emulatorjs/data/loader.js'), 'webtorrentUrl': url('/vendor/webtorrent.min.js'),
                      'chunkStoreUrl': url('/vendor/rom-chunk-store.js'), 'locale': locale}
            transfer_script = ROOT / 'web/rom-transfer.js'
            version = hashlib.sha256(transfer_script.read_bytes()).hexdigest()[:16] if transfer_script.is_file() else 'pending'
            runtime_script = '<script>window.RetroRomTransferConfig=' + json.dumps(config).replace('<', '\\u003c') + ';</script>' + f'<script src="{url("/rom-transfer.js")}?v={version}"></script>'
        document = f'''<!doctype html><html lang="{locale}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{title} · RetroOne</title><link rel="icon" href="{url('/favicon.svg')}" type="image/svg+xml">
<style>html,body{{margin:0;background:#08111e;color:#deedf7;font:14px system-ui;height:100%;width:100%;min-height:0}}body{{display:flex;flex-direction:column}}header{{display:flex;gap:8px 12px;align-items:center;padding:8px 12px;flex-wrap:wrap;flex-shrink:0}}a{{color:#7de5f8;text-decoration:none}}h1{{font-size:16px;font-weight:500;margin:0}}#game{{flex:1;min-height:0;position:relative;overflow:hidden}}footer{{padding:6px 12px;color:#9dacc2;font-size:12px;flex-shrink:0}}button{{background:#162a42;color:#deedf7;border:1px solid #31506d;border-radius:5px;padding:6px 9px;cursor:pointer}}#status{{color:#c4d3e3}}@media(max-height:480px){{footer{{display:none}}header{{font-size:12px;padding:4px 8px}}}}</style>
<header><a class="back-link" href="{home_url}" onclick="leave();return false">← {html.escape(messages['back_library'])}</a><h1>{title}</h1><a href="{html.escape(restore_url)}">{html.escape(messages['load_server_save'])}</a><a href="{save_url}">{html.escape(messages['export_server_save'])}</a><label><button onclick="document.getElementById('save-upload').click()">{html.escape(messages['upload_existing_save'])}</button><input hidden type="file" id="save-upload" accept=".state"></label><span id="status"></span></header>
<div id="game"></div><footer>{html.escape(messages['controls_help'])}</footer>
<script>
// IDBFS uses a fixed /data/saves database upstream. Scope only personal data;
// read-only ROM, BIOS and core caches remain shared. This override is iframe-local.
const storageNamespace={json.dumps(storage_namespace)};
if(storageNamespace&&window.indexedDB){{const idb=window.indexedDB;const nativeOpen=idb.open.bind(idb);const nativeDelete=idb.deleteDatabase.bind(idb);const personalName=name=>['/data/saves','EmulatorJS-states'].includes(String(name))?storageNamespace+name:name;idb.open=(name,...args)=>nativeOpen(personalName(name),...args);idb.deleteDatabase=(name,...args)=>nativeDelete(personalName(name),...args);}}
Object.assign(window,{settings_json});window.EJS_threads=window.crossOriginIsolated;function leave(){{if(parent!==window)parent.postMessage({{type:'retroone:exit'}},location.origin);else location.href='{home_url}'}}window.EJS_onExit=leave;if(parent!==window){{document.querySelector('.back-link').hidden=true;document.querySelector('h1').hidden=true;}}
const status=document.getElementById('status');const messages={messages_json};
async function backupState(blob){{const response=await fetch('{save_url}',{{method:'PUT',body:blob}});if(!response.ok)throw Error((await response.json()).detail);status.textContent=messages.save_backed_up}}
window.EJS_onSaveState=async(event)=>{{if(!event||!event.state){{status.textContent=messages.save_invalid;return}}const blob=new Blob([event.state],{{type:'application/octet-stream'}});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='{game_id}.state';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);try{{await backupState(blob)}}catch(error){{status.textContent=messages.save_downloaded_backup_failed+error.message}}}};
document.getElementById('save-upload').addEventListener('change',async(e)=>{{const file=e.target.files[0];if(!file)return;if(file.size>{MAX_SAVE_SIZE}){{status.textContent=messages.save_too_large;return}}try{{await backupState(file)}}catch(error){{status.textContent=messages.save_backup_failed+error.message}}}});
</script><script src="{url('/emulator-input.js')}?v={bridge_version}"></script>{runtime_script}</html>'''
        return HTMLResponse(document, headers={'Content-Language': locale, 'Vary': 'Accept-Language', 'Cache-Control': 'no-store'})

    # Only bundled application/vendor files are static mounts. ROMs are never mounted.
    if browser_data.is_dir():
        app.mount('/emulatorjs/data', StaticFiles(directory=browser_data), name='emulatorjs')
    web_root = ROOT / 'web'
    if web_root.is_dir():
        register_pwa(app, web_root)
        @app.get('/', response_class=HTMLResponse)
        @app.get('/index.html', response_class=HTMLResponse)
        def versioned_index():
            document = (web_root / 'index.html').read_text('utf-8')
            for filename in ('app.js', 'styles.css', 'i18n.js', 'i18n-en.js', 'library-ui.js', 'mobile-player.js', 'mobile-player.css', 'pwa-install.js'):
                path = web_root / filename
                if path.is_file():
                    version = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
                    resource = '/' + filename
                    pattern = r'([\"\'])' + re.escape(resource) + r'(?:\?[^\"\']*)?\1'
                    document = re.sub(pattern, lambda match: match.group(1) + resource + '?v=' + version + match.group(1), document)
            return HTMLResponse(document, headers={'Cache-Control': 'no-cache, max-age=0, must-revalidate'})
        app.mount('/', StaticFiles(directory=web_root, html=True), name='web')
    else:
        @app.get('/', response_class=HTMLResponse)
        def index():
            return '<!doctype html><meta charset="utf-8"><title>RetroOne</title><h1>RetroOne</h1><p>游戏库已启动，前端资源尚未安装。</p>'
    return app


app = create_app()
