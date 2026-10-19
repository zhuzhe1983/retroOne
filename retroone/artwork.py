"""Exact-name Libretro box art fallback; user's existing artwork always wins.

Sources: https://github.com/libretro-thumbnails/libretro-thumbnails
https://docs.libretro.com/guides/roms-playlists-thumbnails/
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .profiles import atomic_json

# Repository names are fixed, never taken from a request URL or untrusted metadata.
REPOS = {
    'nes': 'Nintendo_-_Nintendo_Entertainment_System',
    'fds': 'Nintendo_-_Family_Computer_Disk_System',
    'snes': 'Nintendo_-_Super_Nintendo_Entertainment_System',
    'gb': 'Nintendo_-_Game_Boy', 'gbc': 'Nintendo_-_Game_Boy_Color',
    'gba': 'Nintendo_-_Game_Boy_Advance', 'nds': 'Nintendo_-_Nintendo_DS',
    '3ds': 'Nintendo_-_Nintendo_3DS', 'n64': 'Nintendo_-_Nintendo_64',
    'gamecube': 'Nintendo_-_GameCube', 'wii': 'Nintendo_-_Wii',
    'virtualboy': 'Nintendo_-_Virtual_Boy', 'pokemini': 'Nintendo_-_Pokemon_Mini',
    'psx': 'Sony_-_PlayStation', 'ps2': 'Sony_-_PlayStation_2', 'psp': 'Sony_-_PlayStation_Portable',
    'megadrive': 'Sega_-_Mega_Drive_-_Genesis', 'sega32x': 'Sega_-_32X',
    'segacd': 'Sega_-_Mega-CD_-_Sega_CD', 'mastersystem': 'Sega_-_Master_System_-_Mark_III',
    'gamegear': 'Sega_-_Game_Gear', 'sg1000': 'Sega_-_SG-1000', 'saturn': 'Sega_-_Saturn',
    'dreamcast': 'Sega_-_Dreamcast', '3do': 'The_3DO_Company_-_3DO',
    'atari2600': 'Atari_-_2600', 'atari5200': 'Atari_-_5200', 'atari7800': 'Atari_-_7800',
    'jaguar': 'Atari_-_Jaguar', 'lynx': 'Atari_-_Lynx',
    'wonderswan': 'Bandai_-_WonderSwan', 'wonderswancolor': 'Bandai_-_WonderSwan_Color',
    'ngp': 'SNK_-_Neo_Geo_Pocket', 'ngpc': 'SNK_-_Neo_Geo_Pocket_Color',
    'pce': 'NEC_-_PC_Engine_-_TurboGrafx_16', 'pcecd': 'NEC_-_PC_Engine_CD_-_TurboGrafx-CD',
    'pcfx': 'NEC_-_PC-FX', 'colecovision': 'Coleco_-_ColecoVision',
    'msx': 'Microsoft_-_MSX', 'msx2': 'Microsoft_-_MSX2', 'dos': 'DOS',
}
MAX_IMAGE_BYTES = 4 * 1024 * 1024
NEGATIVE_TTL = 7 * 86400


def thumbnail_names(game: dict) -> list[str]:
    names = [Path(game['rom_path']).stem, str(game.get('title', ''))]
    result = []
    for name in names:
        name = re.sub(r'[&*/:`<>?\\|]', '_', name).strip()
        if name and name not in result:
            result.append(name)
    return result


class ArtworkCache:
    def __init__(self, config_root: Path, enabled: bool = True, rom_root: Path | None = None):
        self.root = Path(os.getenv('RETROONE_ARTWORK_ROOT', str(config_root / 'artwork')))
        self.root.mkdir(parents=True, exist_ok=True)
        self.rom_root = Path(rom_root).resolve() if rom_root else None
        self.local_stamp = None
        self.local_overrides = {}
        self.enabled = enabled
        system_ca = os.getenv('SSL_CERT_FILE') or ('/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None)
        self.ssl_context = ssl.create_default_context(cafile=system_ca)
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='retroone-artwork')
        self.lock = threading.Lock()
        self.jobs: dict[str, Future] = {}

    def supports(self, game: dict) -> bool:
        return bool(self.local(game) or self.cached(game) or self.enabled and game.get('platform') in REPOS)

    def local(self, game: dict) -> Path | None:
        if self.rom_root is None:
            return None
        manifest = self.root / 'local-overrides.json'
        try:
            stat = manifest.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if manifest.is_symlink() or stat.st_size > 8 * 1024 * 1024:
                return None
            with self.lock:
                if self.local_stamp != stamp:
                    data = json.loads(manifest.read_text('utf-8'))
                    if data.get('version') != 1 or not isinstance(data.get('games'), dict):
                        return None
                    self.local_overrides = data['games']
                    self.local_stamp = stamp
                entry = self.local_overrides.get(game.get('id'), {})
            if entry.get('platform') != game.get('platform') or not isinstance(entry.get('source_path'), str):
                return None
            relative = Path(entry['source_path']).relative_to('/roms')
            path = (self.rom_root / relative).resolve()
            path.relative_to(self.rom_root / 'library' / game['platform'])
            return path if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'} and path.is_file() else None
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def _key(self, game: dict) -> str:
        # Identical named variants across collections reuse one exact cover.
        return hashlib.sha256(json.dumps([game['platform'], thumbnail_names(game)], ensure_ascii=False).encode()).hexdigest()

    def cached(self, game: dict) -> Path | None:
        path = self.root / f'{self._key(game)}.png'
        return path if not path.is_symlink() and path.is_file() else None

    def get(self, game: dict, wait_seconds: float = 5) -> Path | None:
        local = self.local(game)
        if local:
            return local
        cached = self.cached(game)
        if cached or not self.supports(game):
            return cached
        key = self._key(game)
        miss = self.root / f'{key}.miss'
        try:
            if miss.is_file() and time.time() - miss.stat().st_mtime < NEGATIVE_TTL:
                return None
        except OSError:
            pass
        with self.lock:
            job = self.jobs.get(key)
            if job is not None and job.done():
                # Once the miss TTL expires, retry even if no other game was
                # requested in the meantime. A completed failed Future is not
                # a permanent negative cache.
                self.jobs.pop(key, None)
                job = None
            if job is None:
                # Bound queued network jobs. Missing art can retry on the next visit.
                self.jobs = {key: future for key, future in self.jobs.items() if not future.done()}
                if len(self.jobs) >= 64:
                    return None
                job = self.pool.submit(self._download, game, key)
                self.jobs[key] = job
        try:
            return job.result(timeout=wait_seconds)
        except (TimeoutError, OSError, ValueError, KeyError):
            return None

    def _download(self, game: dict, key: str) -> Path | None:
        # Every profile shares this directory. A lock covers the whole fetch so
        # separate API processes do not download or publish the same file at once.
        descriptor = os.open(self.root / f'{key}.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return self.cached(game)
            cached = self.cached(game)
            if cached:
                return cached
            return self._download_locked(game, key)

    def _download_locked(self, game: dict, key: str) -> Path | None:
        repo = REPOS[game['platform']]
        permanent_miss = True
        for name in thumbnail_names(game):
            url = f'https://raw.githubusercontent.com/libretro-thumbnails/{repo}/master/Named_Boxarts/{quote(name, safe="")}.png'
            try:
                request = Request(url, headers={'User-Agent': 'RetroOne/0.1 (LAN personal library)', 'Accept': 'image/png'})
                with urlopen(request, timeout=5, context=self.ssl_context) as response:
                    if response.geturl().split('/')[2] != 'raw.githubusercontent.com':
                        continue
                    data = response.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES or not data.startswith(b'\x89PNG\r\n\x1a\n'):
                    continue
                path = self.root / f'{key}.png'
                temporary = self.root / f'{key}.{time.time_ns()}.tmp'
                temporary.write_bytes(data)
                temporary.replace(path)
                atomic_json(self.root / f'{key}.json', {'source': url, 'matched_name': name, 'fetched_at': time.time()})
                (self.root / f'{key}.miss').unlink(missing_ok=True)
                return path
            except HTTPError as error:
                if error.code != 404:
                    permanent_miss = False
            except (OSError, URLError, ValueError):
                permanent_miss = False
        marker = self.root / f'{key}.miss'
        marker.write_text('exact name not found' if permanent_miss else 'network retry later')
        if not permanent_miss:
            # Retry transient failures after 10 minutes rather than hammering offline DNS.
            stamp = time.time() - NEGATIVE_TTL + 600
            os.utime(marker, (stamp, stamp))
        return None

    def close(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
