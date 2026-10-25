"""Explicit, bounded browser experiments; the automatic 20 MiB rule is unchanged.

This inspects file metadata and bounded ZIP directories, never extracts or reads
an entire ROM. Availability means a suitable installed WASM runtime and a
self-contained payload, not a claim that a particular game has been playtested.
The API must still enforce per-game configuration and firmware requirements.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile

from .platforms import PLATFORMS

AUTO_LIMIT = 20 * 1024**2
DEFAULT_MAX_BYTES = 512 * 1024**2
HARD_MAX_BYTES = 1024**3
LARGE_PLATFORMS = frozenset({'gba', 'n64', 'nds', 'psx', 'psp'})
# Match the pinned EmulatorJS 4.2.3 cores, not the native emulator's formats.
# In particular, its PCSX-ReARMed and PPSSPP cores do not advertise CHD.
CARTRIDGES = {
    'nes': {'.nes', '.unf', '.unif'}, 'fds': {'.fds'},
    'snes': {'.sfc', '.smc', '.bs'}, 'gb': {'.gb'}, 'gbc': {'.gbc', '.gb'},
    'gba': {'.gba'}, 'nds': {'.nds'}, 'n64': {'.z64', '.n64', '.v64'},
    'virtualboy': {'.vb', '.vboy'}, 'megadrive': {'.md', '.gen', '.smd', '.bin'},
    'sega32x': {'.32x', '.bin'}, 'mastersystem': {'.sms'}, 'gamegear': {'.gg'},
    'atari2600': {'.a26', '.bin'}, 'atari5200': {'.a52', '.bin'},
    'atari7800': {'.a78', '.bin'}, 'jaguar': {'.j64', '.jag', '.rom'},
    'lynx': {'.lnx'}, 'wonderswan': {'.ws'}, 'wonderswancolor': {'.wsc', '.ws'},
    'ngp': {'.ngp'}, 'ngpc': {'.ngc', '.ngp'}, 'pce': {'.pce', '.sgx'},
    'colecovision': {'.col', '.rom'},
}
DISC_FILES = {'psx': {'.pbp'}, 'psp': {'.iso', '.cso', '.pbp'}}
MAX_ZIP_ENTRIES = 128
MAX_ZIP_DIRECTORY = 128 * 1024


class BrowserRuntimeError(RuntimeError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class BrowserRuntime:
    def __init__(self, rom_root: Path, browser_data: Path, max_bytes: int | None = None):
        self.rom_root = Path(rom_root).resolve()
        self.library = self.rom_root / 'library'
        self.browser_data = Path(browser_data).resolve()
        value = os.environ.get('RETROONE_WASM_MAX_BYTES', str(DEFAULT_MAX_BYTES)) if max_bytes is None else max_bytes
        if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)) or int(value) < 1:
            raise ValueError('RETROONE_WASM_MAX_BYTES must be a positive integer')
        self.max_bytes = min(int(value), HARD_MAX_BYTES)

    def _path(self, value) -> Path:
        if not isinstance(value, str) or not value or '\x00' in value or '\\' in value:
            raise BrowserRuntimeError('游戏资源路径无效', 403)
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.rom_root / candidate
        if '..' in candidate.parts or not candidate.is_relative_to(self.library):
            raise BrowserRuntimeError('游戏文件不在游戏库内', 403)
        try:
            result = candidate.resolve(strict=True)
            if not result.is_relative_to(self.library) or not result.is_file():
                raise BrowserRuntimeError('游戏文件不在游戏库内或不是独立文件', 403)
            return result
        except (OSError, RuntimeError) as error:
            if isinstance(error, BrowserRuntimeError):
                raise
            raise BrowserRuntimeError('游戏文件暂时无法读取', 409) from error

    @staticmethod
    def _zip_size(path: Path, extensions: set[str], max_bytes: int) -> int:
        """Bound central-directory allocations before zipfile parses the archive."""
        try:
            with path.open('rb') as source:
                size = path.stat().st_size
                source.seek(max(0, size - 65557))
                tail = source.read(65557)
            offset = tail.rfind(b'PK\x05\x06')
            if offset < 0 or len(tail) - offset < 22:
                raise ValueError('directory')
            _, disk, directory_disk, local_count, count, directory_bytes, directory_offset, comment = struct.unpack_from('<4s4H2LH', tail, offset)
            if (disk or directory_disk or local_count != count or not 1 <= count <= MAX_ZIP_ENTRIES or
                    directory_bytes > MAX_ZIP_DIRECTORY or directory_offset + directory_bytes > size or
                    offset + 22 + comment != len(tail)):
                raise ValueError('bounded directory')
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) != count:
                    raise ValueError('entry count')
                members = []
                for info in entries:
                    name = info.filename
                    relative = PurePosixPath(name)
                    file_mode = info.external_attr >> 16
                    if (not name or '\\' in name or '\x00' in name or ':' in name or
                            relative.is_absolute() or '..' in relative.parts or
                            any(ord(character) < 32 for character in name) or
                            (stat.S_IFMT(file_mode) not in {0, stat.S_IFREG, stat.S_IFDIR}) or
                            info.flag_bits & 1 or
                            info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                        raise ValueError('unsafe member')
                    if not info.is_dir():
                        members.append(info)
                if len(members) != 1 or Path(members[0].filename).suffix.lower() not in extensions:
                    raise ValueError('not a single cartridge')
                unpacked = members[0].file_size
                if not 0 < unpacked <= max_bytes:
                    raise ValueError('unpacked limit')
                return unpacked
        except (OSError, ValueError, zipfile.BadZipFile, NotImplementedError, struct.error) as error:
            raise BrowserRuntimeError('浏览器实验版仅支持大小可核验的单卡带 ZIP；请改用云游戏', 403) from error

    def _inspect(self, game: dict) -> tuple[dict, Path]:
        platform = game.get('platform')
        definition = PLATFORMS.get(platform, {})
        core = game.get('browser_core')
        if platform not in CARTRIDGES and platform not in DISC_FILES:
            raise BrowserRuntimeError('该平台尚未开放浏览器实验模式，请使用云游戏')
        if not core or core != definition.get('browser_core'):
            raise BrowserRuntimeError('该游戏没有匹配的浏览器模拟器核心')
        if game.get('blocked_reason'):
            raise BrowserRuntimeError('游戏资源存在待处理问题，请先检查游戏库')
        if game.get('resource_root'):
            raise BrowserRuntimeError('包含外部资源的游戏尚不支持浏览器实验模式')
        path = self._path(game.get('rom_path'))
        for field in ('files', 'launch_files'):
            values = game.get(field, [game.get('rom_path')])
            if not isinstance(values, list) or len(values) != 1 or self._path(values[0]) != path:
                raise BrowserRuntimeError('多文件、分盘或依赖其他 ROM 的游戏请使用云游戏')
        try:
            size = path.stat().st_size
        except OSError as error:
            raise BrowserRuntimeError('游戏文件暂时无法读取') from error
        if not 0 < size <= self.max_bytes:
            raise BrowserRuntimeError('游戏大小超过浏览器实验模式上限，请使用云游戏', 403)
        extensions = CARTRIDGES.get(platform, DISC_FILES.get(platform, set()))
        if path.suffix.lower() == '.zip' and platform in CARTRIDGES:
            unpacked = self._zip_size(path, extensions, self.max_bytes)
        elif path.suffix.lower() in extensions:
            unpacked = size
        else:
            raise BrowserRuntimeError('当前浏览器核心未支持此文件格式；光盘游戏请使用支持的独立镜像', 403)
        if max(size, unpacked) > AUTO_LIMIT and platform not in LARGE_PLATFORMS:
            raise BrowserRuntimeError('该平台的大文件尚未开放浏览器实验模式，请使用云游戏')
        requires_threads = core == 'ppsspp'
        core_file = self.browser_data / 'cores' / f'{core}{"-thread" if requires_threads else ""}-wasm.data'
        if not (self.browser_data / 'loader.js').is_file() or not core_file.is_file():
            raise BrowserRuntimeError('当前服务尚未安装该浏览器模拟器核心')
        if core == 'ppsspp' and not (self.browser_data / 'cores' / 'ppsspp-assets.zip').is_file():
            raise BrowserRuntimeError('PSP 浏览器模拟器所需资源尚未安装')
        return {'available': True, 'reason': '实验模式：完整下载后由此设备运行，兼容性和速度因游戏与设备而异',
                'size_bytes': size, 'unpacked_bytes': unpacked, 'core': core,
                'requires_threads': requires_threads, 'requires_webgl2': core == 'ppsspp',
                'max_bytes': self.max_bytes}, path

    def inspect(self, game: dict) -> dict:
        try:
            return self._inspect(game)[0]
        except BrowserRuntimeError as error:
            core = PLATFORMS.get(game.get('platform'), {}).get('browser_core')
            return {'available': False, 'reason': str(error), 'size_bytes': None,
                    'unpacked_bytes': None, 'core': core, 'requires_threads': core == 'ppsspp',
                    'requires_webgl2': core == 'ppsspp', 'max_bytes': self.max_bytes}

    def require(self, game: dict) -> Path:
        return self._inspect(game)[1]
