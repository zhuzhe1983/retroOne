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
from .catalog import ARCHIVES, BROWSER_LIMIT, Catalog, contained_path, inspect_archive, platform_definitions, public_game
from .profiles import PROFILE_ID, ProfileError, ProfileRegistry
from .presence import PresenceStore
from .network import INSTRUCTIONS, VERIFIED_GAMES, NetworkError, NetworkStore, WorkerGateway
from .settings import DisplaySettings, DisplaySettingsError, StreamResolution, display_view
from .game_metadata import GameMetadataError

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


class StopBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_session_id: str | None = Field(default=None, min_length=1, max_length=128)


class DisplayBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    stream_resolution: StreamResolution


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


def create_app(rom_root: Path | None = None, config_root: Path | None = None, *, runner=None, browser_data: Path | None = None, auto_scan: bool = True, profile_id: str | None = None, base_path: str | None = None) -> FastAPI:
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
    worker_gateway = WorkerGateway(os.getenv('RETROONE_COORDINATOR_URL', 'http://retroone:3000'))

    def url(path: str) -> str:
        return base_path + path

    catalog = Catalog(rom_root, config_root)
    from .library_management import LibraryManager
    library_manager = LibraryManager(catalog)
    from .game_config import GameConfigError

    def resolved_game(game, requested_mode=None):
        try:
            return catalog.game_configs.resolve(game, requested_mode, game.get('review'))
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

    @asynccontextmanager
    async def lifespan(app):
        if auto_scan:
            catalog.begin_scan()
        yield
        # The service only owns this Runner instance and its child process.
        runner.stop()
        artwork.close()

    app = FastAPI(title='RetroOne', version='0.1.0', lifespan=lifespan)
    app.state.catalog = catalog
    app.state.runner = runner
    app.state.profile_id = profile_id
    app.state.profile_registry = profile_registry
    app.state.presence = presence_store
    app.state.network = network_store
    app.state.worker_gateway = worker_gateway
    app.state.display_settings = display_settings

    @app.exception_handler(ProfileError)
    async def profile_error(request: Request, error: ProfileError):
        return JSONResponse({'detail': str(error)}, status_code=error.status_code)

    @app.exception_handler(NetworkError)
    async def network_error(request: Request, error: NetworkError):
        return JSONResponse({'detail': error.detail}, status_code=error.status_code)

    @app.exception_handler(DisplaySettingsError)
    async def display_settings_error(request: Request, error: DisplaySettingsError):
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
        if request.url.path in {'/', '/index.html', '/app.js', '/styles.css'}:
            response.headers['Cache-Control'] = 'no-cache, max-age=0, must-revalidate'
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

    def current_display_settings():
        provider = getattr(runner, 'display_capabilities', None)
        runtime = provider() if provider else {'available': False, 'reason': '云端分辨率设置尚未就绪'}
        return dict(display_view(display_settings.get(), runner.status(), runtime), profile_id=profile_id)

    @app.get('/api/settings/display')
    def get_display_settings():
        return current_display_settings()

    @app.patch('/api/settings/display')
    def update_display_settings(body: DisplayBody):
        display_settings.update(body.model_dump())
        return current_display_settings()

    @app.get('/api/platforms')
    def platforms():
        refresh_modes()
        return {'platforms': catalog.platforms()}

    @app.get('/api/games')
    def games(q: str = Query('', max_length=200), platform: str = Query('', max_length=60), mode: str = Query('', pattern='^(browser|stream|unavailable)?$'), favorite: bool = False, recent: bool = False, review_filter: str = Query('', pattern='^(all|needs_attention|pending|observed)?$'), offset: int = Query(0, ge=0), limit: int = Query(60, ge=1, le=200)):
        refresh_modes()
        result = catalog.list(q=q.strip(), platform=platform, mode=mode, favorite=favorite, recent=recent, review_filter=review_filter, offset=offset, limit=limit)
        result['games'] = [public_view(game) for game in result['games']]
        return result

    @app.get('/api/games/{game_id}')
    def game_detail(game_id: str):
        return public_view(game_or_404(game_id))

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

    @app.post('/api/games/{game_id}/launch')
    def launch(game_id: str, body: LaunchBody | None = None):
        game = resolved_game(game_or_404(game_id), body.mode if body else None)
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
            browser_rom(game)
            if not (browser_data / 'loader.js').is_file():
                raise HTTPException(503, '浏览器模拟器资源尚未安装')
            if platform_definitions().get(game['platform'], {}).get('requires_bios') and not bios_for(game['platform']):
                raise HTTPException(409, '游戏需要 BIOS；已保留游戏，待配置对应固件')
            catalog.mark_played(game_id)
            return {'mode': 'browser', 'play_url': url(f'/play/{game_id}'), 'profile_id': profile_id}
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
            game = dict(game, _display=display_settings.launch_snapshot())
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
    def bios(game_id: str):
        game = game_or_404(game_id)
        browser_rom(game)
        path = bios_for(game['platform'])
        if not path:
            raise HTTPException(404, '未找到对应 BIOS')
        return FileResponse(path, media_type='application/octet-stream')

    def save_path(game_id: str) -> Path:
        game_or_404(game_id)
        return config_root / 'browser-saves' / f'{game_id}.state'

    @app.get('/api/games/{game_id}/save-state')
    def download_save(game_id: str):
        path = save_path(game_id)
        if not path.is_file():
            raise HTTPException(404, '暂无服务器存档')
        return FileResponse(path, media_type='application/octet-stream', filename=f'{game_id}.state', headers={'Cache-Control': 'no-store'})

    @app.put('/api/games/{game_id}/save-state')
    async def upload_save(game_id: str, request: Request):
        path = save_path(game_id)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_SAVE_SIZE:
                raise HTTPException(413, '存档不能超过 32 MiB')
        if not data:
            raise HTTPException(400, '存档为空')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f'.{time.time_ns()}.tmp')
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return {'saved': True, 'size': len(data)}

    @app.get('/play/{game_id}', response_class=HTMLResponse)
    def play(game_id: str, restore: bool = False):
        game = resolved_game(game_or_404(game_id), 'browser')
        path = browser_rom(game)
        if not (browser_data / 'loader.js').is_file():
            raise HTTPException(503, '浏览器模拟器资源尚未安装')
        path = browser_payload(game, path)
        bios_path = bios_for(game['platform'])
        if platform_definitions().get(game['platform'], {}).get('requires_bios') and not bios_path:
            raise HTTPException(409, '游戏需要 BIOS；请配置对应固件')
        # Player1 retains the historical storage keys, including already-created
        # EmulatorJS battery saves. Other players get separate personal DBs below.
        save_title = game.get('_save_title', game['title'])
        storage_game_name = save_title if profile_id == 'player1' else f"{profile_id}_{game_id}_{save_title}"
        settings = {'EJS_player': '#game', 'EJS_core': game['browser_core'], 'EJS_gameUrl': url(f'/api/games/{game_id}/rom/{quote(path.name)}'),
                    'EJS_pathtodata': url('/emulatorjs/data/'), 'EJS_gameName': storage_game_name, 'EJS_gameID': int(game_id[:12], 16),
                    'EJS_defaultControls': DEFAULT_CONTROLS, 'EJS_language': 'zh-CN', 'EJS_color': '#64d9f3', 'EJS_backgroundColor': '#0b1424',
                    'EJS_startOnLoaded': False, 'EJS_startButtonName': '开始游戏', 'EJS_defaultOptions': {'save-state-location': 'download'}}
        if bios_path:
            settings['EJS_biosUrl'] = url(f'/api/games/{game_id}/bios')
        if restore and save_path(game_id).is_file():
            settings['EJS_loadStateURL'] = url(f'/api/games/{game_id}/save-state')
        settings_json = json.dumps(settings, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        title = html.escape(game['title'])
        home_url, play_url, save_url = url('/'), url(f'/play/{game_id}'), url(f'/api/games/{game_id}/save-state')
        storage_namespace = '' if profile_id == 'player1' else f'RetroOne-{profile_id}-'
        return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · RetroOne</title><link rel="icon" href="{url('/favicon.svg')}" type="image/svg+xml">
<style>html,body{{margin:0;background:#08111e;color:#deedf7;font:14px system-ui;height:100%}}body{{display:flex;flex-direction:column}}header{{display:flex;gap:20px;align-items:center;padding:12px 20px;flex-wrap:wrap}}a{{color:#7de5f8;text-decoration:none}}h1{{font-size:16px;font-weight:500;margin:0}}#game{{flex:1;min-height:400px}}footer{{padding:9px 20px;color:#9dacc2;font-size:12px}}button{{background:#162a42;color:#deedf7;border:1px solid #31506d;border-radius:5px;padding:6px 9px;cursor:pointer}}#status{{color:#c4d3e3}}</style>
<header><a class="back-link" href="{home_url}" onclick="leave();return false">← 返回游戏库</a><h1>{title}</h1><a href="{play_url}?restore=true">载入服务器存档</a><a href="{save_url}">导出服务器存档</a><label><button onclick="document.getElementById('save-upload').click()">上传已有存档</button><input hidden type="file" id="save-upload" accept=".state"></label><span id="status"></span></header>
<div id="game"></div><footer>WASD 移动 · J/K/U/I 动作 · Enter 开始 · Shift 选择 · 键位及 Xbox / PlayStation 手柄可在模拟器菜单调整。游戏内存档保存在当前浏览器；点击模拟器“保存状态”会下载存档并备份到服务器。原作低于 60 帧时保留原速。</footer>
<script>
// IDBFS uses a fixed /data/saves database upstream. Scope only personal data;
// read-only ROM, BIOS and core caches remain shared. This override is iframe-local.
const storageNamespace={json.dumps(storage_namespace)};
if(storageNamespace&&window.indexedDB){{const idb=window.indexedDB;const nativeOpen=idb.open.bind(idb);const nativeDelete=idb.deleteDatabase.bind(idb);const personalName=name=>['/data/saves','EmulatorJS-states'].includes(String(name))?storageNamespace+name:name;idb.open=(name,...args)=>nativeOpen(personalName(name),...args);idb.deleteDatabase=(name,...args)=>nativeDelete(personalName(name),...args);}}
Object.assign(window,{settings_json});window.EJS_threads=window.crossOriginIsolated;function leave(){{if(parent!==window)parent.postMessage({{type:'retroone:exit'}},location.origin);else location.href='{home_url}'}}window.EJS_onExit=leave;if(parent!==window){{document.querySelector('.back-link').hidden=true;document.querySelector('h1').hidden=true;}}
const status=document.getElementById('status');
async function backupState(blob){{const response=await fetch('{save_url}',{{method:'PUT',body:blob}});if(!response.ok)throw Error((await response.json()).detail);status.textContent='存档已备份到服务器'}}
window.EJS_onSaveState=async(event)=>{{if(!event||!event.state){{status.textContent='模拟器未生成有效存档';return}}const blob=new Blob([event.state],{{type:'application/octet-stream'}});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download='{game_id}.state';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);try{{await backupState(blob)}}catch(error){{status.textContent='文件已下载，服务器备份失败：'+error.message}}}};
document.getElementById('save-upload').addEventListener('change',async(e)=>{{const file=e.target.files[0];if(!file)return;if(file.size>{MAX_SAVE_SIZE}){{status.textContent='存档超过 32 MiB';return}}try{{const response=await fetch('{save_url}',{{method:'PUT',body:file}});if(!response.ok)throw Error((await response.json()).detail);status.textContent='存档已备份到服务器'}}catch(error){{status.textContent='备份失败：'+error.message}}}});
</script><script src="{url('/emulatorjs/data/loader.js')}"></script></html>'''

    # Only bundled application/vendor files are static mounts. ROMs are never mounted.
    if browser_data.is_dir():
        app.mount('/emulatorjs/data', StaticFiles(directory=browser_data), name='emulatorjs')
    web_root = ROOT / 'web'
    if web_root.is_dir():
        @app.get('/', response_class=HTMLResponse)
        @app.get('/index.html', response_class=HTMLResponse)
        def versioned_index():
            document = (web_root / 'index.html').read_text('utf-8')
            for filename in ('app.js', 'styles.css'):
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
