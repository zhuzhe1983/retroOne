"""Read-only ROM discovery, safe archive accounting, and a persistent SQLite index."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import shutil
import subprocess
import re
import sqlite3
import stat
import struct
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from .library_review import ReviewIndex
from .game_config import GameConfigStore, GameConfigError
from .game_metadata import GameMetadataStore
from .library_settings import SORT_OPTIONS
from .library_layout import LibraryLayout, LEGACY_SOURCES, canonical_manifest

BROWSER_LIMIT = 20 * 1024 * 1024
MAX_METADATA = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20000
MAX_CENTRAL_DIRECTORY = 8 * 1024 * 1024
SCAN_BATCH_SIZE = 250
DISPLAY_FIELDS = ('title', 'description', 'sort_by', 'media', 'details', 'first_added_at')
ARCHIVES = {'.zip', '.7z', '.rar', '.gz', '.dosz'}
DISC_LINKS = {'.cue', '.m3u', '.m3u8', '.gdi'}
AUXILIARY_DIRS = {'media', 'screenshots', 'saves', 'savestates', 'cheats', 'bios', 'firmware', '@eadir', '#recycle', '.git'}
COMMON_EXTENSIONS = {'.nes', '.fds', '.sfc', '.smc', '.gb', '.gbc', '.gba', '.nds', '.n64', '.z64', '.v64', '.md', '.gen', '.sms', '.gg', '.32x', '.pce', '.ngp', '.ngc', '.ws', '.wsc', '.a26', '.a52', '.a78', '.lnx', '.vb', '.iso', '.cso', '.chd', '.pbp', '.cue', '.gdi', '.m3u', '.m3u8', '.cdi', '.nsp', '.xci', '.3ds', '.cci', '.cia', '.rvz', '.wbfs', '.gcm', '.zip', '.7z', '.dosz', '.bat', '.com', '.exe'}


def platform_definitions() -> dict[str, dict]:
    try:
        from .platforms import PLATFORMS
        return PLATFORMS
    except ImportError:
        return {'nes': {'name': '红白机 FC', 'browser_core': 'nes', 'extensions': ['nes', 'fds', 'zip', '7z']},
                'psx': {'name': 'PlayStation', 'browser_core': 'psx', 'extensions': ['cue', 'chd', 'pbp', 'm3u']},
                'dos': {'name': 'DOS', 'browser_core': None, 'extensions': ['zip', 'dosz', 'exe', 'bat', 'com']}}


def contained_path(root: Path, candidate: Path, *, exists: bool = True) -> Path:
    """Reject symlinks escaping the library as well as lexical path traversal."""
    root = root.resolve()
    candidate = candidate.resolve(strict=exists)
    if not candidate.is_relative_to(root):
        raise ValueError('文件路径超出游戏库')
    return candidate


def relative_reference(root: Path, directory: Path, value: str) -> Path:
    value = value.strip().strip('"').replace('\\', '/')
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or re.match(r'^[A-Za-z]:', value):
        raise ValueError('游戏引用含不安全路径')
    try:
        return contained_path(root, directory.joinpath(*path.parts))
    except FileNotFoundError:
        # Pegasus libraries were often authored on case-insensitive Windows.
        # Match each existing path component only when the casefold result is
        # unique; never guess between variants or leave the read-only ROM root.
        current = contained_path(root, directory)
        for component in path.parts:
            exact = current / component
            if exact.exists():
                current = contained_path(root, exact)
                continue
            matches = [child for child in current.iterdir() if child.name.casefold() == component.casefold()]
            if len(matches) != 1:
                raise FileNotFoundError(value)
            current = contained_path(root, matches[0])
        return current


def safe_archive_name(name: str) -> bool:
    normalized = name.replace('\\', '/')
    path = PurePosixPath(normalized)
    return bool(normalized) and not path.is_absolute() and '..' not in path.parts and not re.match(r'^[A-Za-z]:', normalized) and '\x00' not in normalized


def inspect_archive(path: Path) -> tuple[int | None, str | None]:
    """Inspect headers only, never extract. Unknown formats cannot use browser mode."""
    if path.suffix.lower() == '.7z':
        return inspect_7z(path)
    if path.suffix.lower() not in {'.zip', '.dosz'}:
        return None, None
    try:
        with path.open('rb') as source:
            source.seek(max(0, path.stat().st_size - 65557))
            tail = source.read(65557)
        marker = tail.rfind(b'PK\x05\x06')
        if marker < 0 or len(tail) - marker < 22:
            return None, 'ZIP 目录损坏，需检查原文件'
        record = struct.unpack_from('<4s4H2LH', tail, marker)
        entries, directory_bytes = record[4], record[5]
        if entries == 65535 or directory_bytes == 0xFFFFFFFF:
            return None, None
        if entries > MAX_ARCHIVE_ENTRIES or directory_bytes > MAX_CENTRAL_DIRECTORY:
            return None, None
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                return None, None
            total = 0
            for info in infos:
                if not safe_archive_name(info.filename) or stat.S_ISLNK(info.external_attr >> 16):
                    return None, '压缩包包含不安全路径或符号链接'
                if info.flag_bits & 1:
                    return None, '压缩包已加密，需要先提供可读副本'
                total += info.file_size
            return total, None
    except (OSError, ValueError, zipfile.BadZipFile, NotImplementedError):
        return None, '压缩包无法读取，需检查原文件'


def inspect_7z(path: Path) -> tuple[int | None, str | None]:
    """Bound subprocess output/time; no extraction, shell, or password interaction."""
    binary = shutil.which('7zz') or shutil.which('7z')
    if not binary:
        return None, None
    process = subprocess.Popen([binary, 'l', '-slt', '-ba', '-p', '--', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    output = bytearray()
    deadline = time.monotonic() + 8
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if time.monotonic() > deadline:
                    return None, None
                if not selector.select(timeout=0.1):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_CENTRAL_DIRECTORY:
                    return None, None
        if process.wait(timeout=1) != 0:
            return None, '7z 压缩包无法读取或已加密'
        total, entries = 0, 0
        for line in output.decode('utf-8', errors='replace').splitlines():
            if line.startswith('Path = '):
                entries += 1
                if entries > MAX_ARCHIVE_ENTRIES:
                    return None, None
                if not safe_archive_name(line[7:]):
                    return None, '7z 压缩包含不安全路径'
            elif line.startswith('Size = '):
                total += int(line[7:])
            elif line.startswith(('Symbolic Link = ', 'Hard Link = ')) or line == 'Encrypted = +':
                return None, '7z 压缩包包含链接或加密文件'
        return total if entries else None, None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, '7z 压缩包检查失败'
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


ALLOWED_CORE_HINTS = {'fbneo', 'fbalpha2012', 'mame2003_plus', 'mame2003', 'mame2010', 'fceumm', 'nestopia', 'snes9x', 'snes9x2010', 'genesis_plus_gx', 'picodrive', 'gambatte', 'mgba', 'dosbox_pure'}


def safe_core_hint(launch: str) -> str | None:
    matches = re.findall(r'([a-z0-9_]+)_libretro\.(?:dll|so|dylib)', launch.lower())
    return next((name for name in matches if name in ALLOWED_CORE_HINTS), None)


def parse_pegasus(path: Path) -> tuple[dict, list[dict]]:
    if path.stat().st_size > MAX_METADATA:
        raise ValueError('元数据超过读取上限')
    text = path.read_bytes().decode('utf-8-sig', errors='replace')
    collection: dict[str, Any] = {}
    games: list[dict] = []
    current = collection
    last_key = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line[:1].isspace() and last_key:
            if last_key == 'files':
                current.setdefault('files', []).append(line.strip())
            else:
                current[last_key] = str(current.get(last_key, '')) + '\n' + line.strip()
            continue
        match = re.match(r'^([\w.-]+):\s*(.*)$', line)
        if not match:
            continue
        key, value = match.groups()
        key = key.lower()
        if key == 'game':
            current = {'title': value, 'files': []}
            games.append(current)
            last_key = 'title'
        elif key in {'file', 'files'}:
            current.setdefault('files', []).append(value) if value else current.setdefault('files', [])
            last_key = 'files'
        else:
            current[key] = value.replace('\\n', '\n')
            last_key = key
    return collection, games


def dependency_files(root: Path, path: Path, seen: set[Path] | None = None) -> list[Path]:
    """Follow only bounded local CUE/M3U/GDI references; all tracks count toward size."""
    seen = set() if seen is None else seen
    path = contained_path(root, path)
    if path in seen:
        return []
    if len(seen) >= 512:
        raise ValueError('光盘关联文件过多')
    seen.add(path)
    files = [path]
    suffix = path.suffix.lower()
    if suffix not in DISC_LINKS:
        return files
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('光盘索引文件过大')
    content = path.read_text(encoding='utf-8-sig', errors='replace')
    refs = []
    if suffix == '.cue':
        refs = [m.group(1) or m.group(2) for m in re.finditer(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))\s+', content, re.I | re.M)]
    elif suffix == '.gdi':
        for line in content.splitlines()[1:]:
            m = re.match(r'^\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"([^"]+)"|(\S+))', line)
            if m:
                refs.append(m.group(1) or m.group(2))
    else:
        refs = [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    for ref in refs:
        child = relative_reference(root, path.parent, ref)
        files.extend(dependency_files(root, child, seen))
    return files


class Catalog:
    def __init__(self, rom_root: Path, config_root: Path):
        self.rom_root = Path(rom_root).resolve()
        self.config_root = Path(config_root).resolve()
        self.config_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.config_root / 'catalog.sqlite3'
        self._scan_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._scan_layout = None
        self._runtime_key = None
        self.library_data_root = Path(os.getenv('RETROONE_LIBRARY_DATA_ROOT', str(self.config_root / 'library-data'))).resolve()
        self.game_configs = GameConfigStore(self.library_data_root / 'runtime', self.rom_root)
        self.game_metadata = GameMetadataStore(self.library_data_root / 'metadata', self.rom_root)
        self._metadata_revision = -1
        self._metadata_lock = threading.RLock()
        self._reviews = ReviewIndex(self.rom_root, self.library_data_root / 'metadata' / 'library-review.json')
        self._review_key = object()
        self._review_lock = threading.RLock()
        self._review_generation = 0
        self._media_roots: dict[Path, bool] = {}
        self.scan_status = {'running': False, 'scanned': 0, 'errors': 0, 'started_at': None, 'finished_at': None, 'last_error': None, 'error_types': {}, 'warning_samples': []}
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS games (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, platform TEXT NOT NULL,
                    collection TEXT NOT NULL, mode TEXT NOT NULL, browser_eligible INTEGER NOT NULL,
                    reason TEXT, blocked_reason TEXT, payload TEXT NOT NULL, scan_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS games_platform ON games(platform);
                CREATE INDEX IF NOT EXISTS games_mode ON games(mode);
                CREATE TABLE IF NOT EXISTS favorites (game_id TEXT PRIMARY KEY, favorite INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS history (game_id TEXT PRIMARY KEY, played_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS game_play_counts (game_id TEXT PRIMARY KEY, play_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS game_reviews (game_id TEXT PRIMARY KEY, group_id TEXT, status TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS reviews_group ON game_reviews(group_id);
            ''')
            # Keep the legacy two-column history table compatible with old workers.
            db.execute("INSERT OR IGNORE INTO settings VALUES('play_count_since',?)", (str(time.time()),))
            row = db.execute("SELECT value FROM settings WHERE key='last_scan'").fetchone()
            if row:
                self.scan_status.update(json.loads(row[0]))
                self.scan_status['running'] = False
            # Existing profile catalogs are import caches. Never let an older
            # member cache replace metadata already managed in the shared store.
            self.game_metadata.import_many((json.loads(row[0]) for row in db.execute('SELECT payload FROM games')), only_missing=True)

    def _sync_metadata(self, *, force=False):
        """Materialize shared display fields for profile-local search/sorting.

        Runtime fields, original browser save names and member preferences are
        not owned by the metadata store and must survive every refresh.
        """
        with self._metadata_lock:
            if not force and self.game_metadata.revision() == self._metadata_revision:
                return
            with self.connect() as db:
                # Read the shared snapshot only after serializing local writers.
                # A scan or another Catalog instance must not lose runtime fields
                # to a payload read before its commit.
                db.execute('BEGIN IMMEDIATE')
                revision, changes = self.game_metadata.changes_since(-1 if force else self._metadata_revision)
                for fields in changes:
                    row = db.execute('SELECT payload FROM games WHERE id=?', (fields['id'],)).fetchone()
                    if row is None:
                        continue
                    game = json.loads(row[0])
                    if '_save_title' in game and all(game.get(key) == fields[key] for key in DISPLAY_FIELDS):
                        continue
                    game.setdefault('_save_title', game['title'])
                    self._apply_display_fields(game, fields)
                    db.execute('UPDATE games SET title=?,payload=? WHERE id=?', (game['title'], json.dumps(game, ensure_ascii=False), game['id']))
            self._metadata_revision = revision

    @staticmethod
    def _apply_display_fields(game, fields):
        game.update({key: fields[key] for key in DISPLAY_FIELDS})
        for kind in ('cover', 'video', 'logo'):
            game[kind + '_url'] = f"/api/games/{game['id']}/media/{kind}" if kind in game['media'] else None

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def count(self) -> int:
        self._sync_reviews()
        with self.connect() as db:
            return db.execute('SELECT count(DISTINCT coalesce(r.group_id,g.id)||g.mode) FROM games g LEFT JOIN game_reviews r ON r.game_id=g.id').fetchone()[0]

    def has_games(self) -> bool:
        """Check startup readiness without walking ROMs or revalidating reviews."""
        with self.connect() as db:
            return db.execute('SELECT 1 FROM games LIMIT 1').fetchone() is not None

    def _sync_reviews(self):
        generation = self._review_generation
        with self._review_lock:
            self._reviews.load()
            if self._review_key != self._reviews.key:
                rows = self._reviews.rows()
                with self.connect() as db:
                    db.execute('DELETE FROM game_reviews')
                    db.executemany('INSERT INTO game_reviews VALUES(?,?,?,?)', rows)
                self._review_key = self._reviews.key
                # rows() already validates every source, including hidden aliases.
                self._review_generation += 1
                return
            if generation != self._review_generation:
                # A complete validation finished while this request waited. Share
                # that snapshot with overlapping requests only: the next request
                # still rechecks every alias, without a stale-validity TTL.
                return
            invalid = self._reviews.changed_groups()
            if invalid:
                with self.connect() as db:
                    db.executemany("UPDATE game_reviews SET group_id=NULL,status='stale' WHERE group_id=?", [(g,) for g in invalid])
            self._review_generation += 1

    def begin_scan(self) -> bool:
        if not self._scan_lock.acquire(blocking=False):
            return False
        self.scan_status.update(running=True, scanned=0, errors=0, started_at=time.time(), finished_at=None, last_error=None, error_types={}, warning_samples=[])
        self._scan_thread = threading.Thread(target=self._scan_locked, name='retroone-library-scan', daemon=True)
        self._scan_thread.start()
        return True

    def scan(self) -> dict:
        if not self._scan_lock.acquire(blocking=False):
            return dict(self.scan_status)
        self.scan_status.update(running=True, scanned=0, errors=0, started_at=time.time(), finished_at=None, last_error=None, error_types={}, warning_samples=[])
        self._scan_locked()
        return dict(self.scan_status)

    def _scan_warning(self, kind: str, path: Path, reference: str = '') -> None:
        self.scan_status['errors'] += 1
        kinds = self.scan_status.setdefault('error_types', {})
        kinds[kind] = kinds.get(kind, 0) + 1
        samples = self.scan_status.setdefault('warning_samples', [])
        if len(samples) < 30:
            try:
                location = path.relative_to(self.rom_root).as_posix()
            except ValueError:
                location = path.name
            samples.append({'kind': kind, 'metadata': location, 'reference': reference[:240]})

    def _scan_locked(self) -> None:
        scan_id = str(time.time_ns())
        self._media_roots = {}
        try:
            if not (self.rom_root / 'library').is_dir():
                raise FileNotFoundError('one/library 尚未就绪；保留现有索引')
            self._scan_layout = self._load_layout(remember=True)
            batch = []
            # Discovery may spend minutes on archive headers or network storage.
            # Hold no catalog transaction while waiting for the next game.
            for game in self.discover():
                try:
                    self.game_configs.generate(game)
                except GameConfigError:
                    self._scan_warning('launch_config_needs_review', Path(game['rom_path']))
                batch.append(game)
                self.scan_status['scanned'] += 1
                if len(batch) >= SCAN_BATCH_SIZE:
                    self._write_scan_batch(batch, scan_id)
                    batch = []
            self._write_scan_batch(batch, scan_id)
            with self.connect() as db:
                db.execute('DELETE FROM games WHERE scan_id != ?', (scan_id,))
            self._runtime_key = None
            self._review_key = object()
        except Exception as error:
            self.scan_status['last_error'] = str(error)[:500]
            self.scan_status['errors'] += 1
        finally:
            self._scan_layout = None
            # Each committed batch already has effective shared display fields.
            # Pick up concurrent edits, including on an interrupted scan, without
            # waiting until this point to restore details or manual overrides.
            try:
                self._sync_metadata()
            except Exception as error:
                self.scan_status['last_error'] = str(error)[:500]
                self.scan_status['errors'] += 1
            self.scan_status.update(running=False, finished_at=time.time())
            with self.connect() as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES('last_scan',?)", (json.dumps(self.scan_status),))
            self._scan_lock.release()

    def _write_scan_batch(self, batch: list[dict], scan_id: str) -> None:
        if not batch:
            return
        # Keep source imports separate from effective overrides and enrichment.
        # Import before taking the catalog write lock; it may inspect media paths.
        self.game_metadata.import_many(batch)
        with self._metadata_lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            fields = self.game_metadata.effective_many(game['id'] for game in batch)
            for source in batch:
                game = dict(source)
                row = db.execute('SELECT payload FROM games WHERE id=?', (game['id'],)).fetchone()
                previous = json.loads(row['payload']) if row else {}
                game['_save_title'] = previous.get('_save_title', previous.get('title', game['title']))
                self._apply_display_fields(game, fields[game['id']])
                db.execute('''INSERT INTO games VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,platform=excluded.platform,
                    collection=excluded.collection,mode=excluded.mode,browser_eligible=excluded.browser_eligible,
                    reason=excluded.reason,blocked_reason=excluded.blocked_reason,payload=excluded.payload,scan_id=excluded.scan_id''',
                    (game['id'], game['title'], game['platform'], game['collection'], game['mode'], int(game['browser_eligible']), game['reason'], game.get('blocked_reason'), json.dumps(game, ensure_ascii=False), scan_id))

    def _load_layout(self, *, remember=False):
        layout = LibraryLayout.load(self.rom_root)
        with self.connect() as db:
            required = db.execute("SELECT value FROM settings WHERE key='library_layout_required'").fetchone()
            if required and required['value'] == '1' and not layout.declared:
                raise ValueError('此玩家索引已启用固定身份布局，但布局文件缺失；保留现有索引')
            saved = db.execute("SELECT value FROM settings WHERE key='library_layout_bindings'").fetchone()
            bindings = layout.document['collections']
            if saved:
                try:
                    previous = canonical_manifest({'schema_version': 1, 'collections': json.loads(saved['value'])})['collections']
                except (ValueError, TypeError) as error:
                    raise ValueError('玩家索引中的游戏库身份绑定无效；保留现有索引') from error
                current = {entry['path']: entry['original_path'] for entry in bindings}
                if (not layout.declared or any(current.get(entry['path']) != entry['original_path'] for entry in previous)):
                    raise ValueError('游戏库原有身份绑定已被删除或更改；请通过目录迁移工具更新，保留现有索引')
            if remember and layout.declared:
                db.execute("INSERT OR REPLACE INTO settings VALUES('library_layout_required','1')")
                db.execute("INSERT OR REPLACE INTO settings VALUES('library_layout_bindings',?)",
                           (json.dumps(bindings, ensure_ascii=False, sort_keys=True, separators=(',', ':')),))
        return layout

    def discover(self) -> Iterator[dict]:
        library = self.rom_root / 'library'
        # Validate every declared binding before yielding anything: an invalid
        # manifest must not partially import a collection under new identities.
        layout = self._scan_layout or self._load_layout()
        declared = {entry.path for entry in layout.collections}
        emitted = {}
        handled: set[Path] = set()
        primary_paths: set[Path] = set()
        owned_dirs: set[Path] = set()
        def games(platform, source, collection, *, recursive=True, excluded=(), archives_only=False):
            for game in self._collection(platform, source, collection, layout=layout,
                                         recursive=recursive, excluded=excluded,
                                         handled=handled, primary_paths=primary_paths,
                                         owned_dirs=owned_dirs, archives_only=archives_only):
                previous = emitted.get(game['id'])
                if previous is not None:
                    if previous != game['rom_path']:
                        raise ValueError('不同物理路径产生相同游戏 ID；请检查游戏库布局')
                    continue
                emitted[game['id']] = game['rom_path']
                yield game
        for binding in layout.collections:
            yield from games(binding.platform, '', binding.path)
        for platform_dir in sorted(library.iterdir()):
            if (not platform_dir.is_dir() or platform_dir.is_symlink()
                    or platform_dir.name.startswith('.') or platform_dir.name.lower() in AUXILIARY_DIRS):
                continue
            definition = platform_definitions().get(platform_dir.name, {})
            directory_game = definition.get('kind') in {'windows', 'dos'} or platform_dir.name in {'teknoparrot', 'openbor'}
            # Loose ROMs and platform-level metadata are supported without a
            # source/collection wrapper. Bare PC EXEs need an explicit launcher;
            # scanning each one would turn setup/utilities into separate games.
            yield from games(platform_dir.name, '', platform_dir, recursive=False, excluded=declared,
                             archives_only=directory_game)
            for child in sorted(platform_dir.iterdir()):
                if (not child.is_dir() or child.is_symlink() or child in declared
                        or child.name.startswith('.') or child.name.lower() in AUXILIARY_DIRS):
                    continue
                # An unmarked PC folder remains the legacy arbitrary source
                # layer. Guessing it is a new game would change historical IDs.
                has_metadata = any(path.name.lower() == 'metadata.pegasus.txt' and path.is_file() for path in child.iterdir())
                legacy_source = child.name in LEGACY_SOURCES or (directory_game and not has_metadata)
                if legacy_source:
                    source = '' if layout.declared else child.name
                    # Self-contained archives support a new single collection
                    # layer; ambiguous PC folders keep legacy boundaries and
                    # need metadata to distinguish a game from its helper EXEs.
                    yield from games(platform_dir.name, source, child, recursive=False, excluded=declared,
                                     archives_only=directory_game)
                    for collection in sorted(child.iterdir()):
                        if (not collection.is_dir() or collection.is_symlink() or collection in declared
                                or collection.name.startswith('.') or collection.name.lower() in AUXILIARY_DIRS):
                            continue
                        yield from games(platform_dir.name, source, collection, excluded=declared)
                else:
                    # A new PC metadata folder can be a single game, not a
                    # collection. Read its explicit records without guessing
                    # that data/ or another EXE is an additional title.
                    yield from games(platform_dir.name, '', child, excluded=declared,
                                     recursive=not directory_game, archives_only=directory_game)

    def _collection(self, platform: str, source: str, collection: Path, *, layout=None,
                    recursive=True, excluded=(), handled=None, primary_paths=None,
                    owned_dirs=None, archives_only=False) -> Iterator[dict]:
        definition = platform_definitions().get(platform, {})
        extensions = {'.' + ext.lstrip('.').lower() for ext in definition.get('extensions', [])} or set(COMMON_EXTENSIONS)
        directory_game = definition.get('kind') in {'windows', 'dos'} or platform in {'teknoparrot', 'openbor'}
        handled = set() if handled is None else handled
        primary_paths = set() if primary_paths is None else primary_paths
        owned_dirs = set() if owned_dirs is None else owned_dirs
        candidates: list[Path] = []
        def excluded_path(path):
            return any(path.is_relative_to(root) for root in excluded)
        def owned_path(path):
            return any(path.is_relative_to(root) for root in owned_dirs)
        for directory, subdirs, files in os.walk(collection, followlinks=False):
            directory = Path(directory)
            if owned_path(directory):
                subdirs[:] = []
                continue
            subdirs[:] = sorted(d for d in subdirs if recursive and d.lower() not in AUXILIARY_DIRS
                                and not d.startswith('.') and not (directory / d).is_symlink()
                                and not excluded_path(directory / d) and not owned_path(directory / d))
            metadata_name = next((name for name in files if name.lower() == 'metadata.pegasus.txt'), None)
            if metadata_name:
                try:
                    info, records = parse_pegasus(directory / metadata_name)
                except (OSError, ValueError):
                    self._scan_warning('metadata_unreadable', directory / metadata_name)
                    records, info = [], {}
                for record in records:
                    record['native_core'] = safe_core_hint(record.get('launch', info.get('launch', '')))
                    paths = []
                    new_owned_dirs = set()
                    unsafe_reason = None
                    for ref in record.get('files', []):
                        try:
                            path = relative_reference(self.rom_root, directory, ref)
                            paths.append(path)
                            if path.is_dir():
                                new_owned_dirs.add(path)
                            elif directory_game and path.parent != directory:
                                new_owned_dirs.add(directory / path.relative_to(directory).parts[0])
                        except (OSError, ValueError):
                            unsafe_reason = '元数据引用文件缺失或路径不安全'
                            # Keep a safe missing reference visible as a blocked
                            # game; disappearance must not look like a clean scan.
                            value = ref.strip().strip('"').replace('\\', '/')
                            relative = PurePosixPath(value)
                            if value and not relative.is_absolute() and '..' not in relative.parts and not re.match(r'^[A-Za-z]:', value):
                                try:
                                    missing = contained_path(self.rom_root, directory.joinpath(*relative.parts), exists=False)
                                    paths.append(missing)
                                    self._scan_warning('metadata_reference_missing_or_unsafe', directory / metadata_name, ref)
                                except (ValueError, OSError):
                                    pass
                    if not paths:
                        self._scan_warning('metadata_reference_missing_or_unsafe', directory / metadata_name, ' | '.join(record.get('files', [])))
                        continue
                    # Directory ownership suppresses implicit helper discovery,
                    # never another explicit game in the same metadata file.
                    # A dependency can also be an explicitly listed game: an
                    # arcade clone may reference its playable parent ZIP first.
                    if excluded_path(paths[0]) or paths[0] in primary_paths:
                        continue
                    game = self._build_game(platform, source, collection, paths, record, directory, unsafe_reason, layout=layout)
                    handled.update(Path(p) for p in game['files'])
                    handled.update(paths)
                    primary_paths.add(paths[0])
                    owned_dirs.update(new_owned_dirs)
                    yield game
            # Directory based PC games own their support files. Never turn drivers,
            # redistributables or helper EXEs into hundreds of separate games.
            subdirs[:] = [name for name in subdirs if directory / name not in owned_dirs]
            if directory_game:
                for name in subdirs:
                    path = directory / name
                    game = self._build_game(platform, source, collection, [path], {}, directory, '目录游戏已保留；需要单独配置启动项及运行环境', layout=layout)
                    owned_dirs.add(path)
                    handled.update(Path(p) for p in game['files'])
                    handled.add(path)
                    primary_paths.add(path)
                    yield game
                subdirs[:] = []
            for name in sorted(files):
                path = directory / name
                if path.is_symlink() or name.startswith('.'):
                    continue
                if (path.suffix.lower() in extensions and path not in handled
                        and (not archives_only or path.suffix.lower() in ARCHIVES)):
                    candidates.append(path)
        # Index playlists before CUE sheets and tracks, retaining whole disc sets.
        priority = {'.m3u': 0, '.m3u8': 0, '.cue': 1, '.gdi': 1}
        for path in sorted(candidates, key=lambda p: (priority.get(p.suffix.lower(), 2), str(p))):
            if path in handled:
                continue
            game = self._build_game(platform, source, collection, [path], {}, path.parent, layout=layout)
            handled.update(Path(p) for p in game['files'])
            handled.add(path)
            primary_paths.add(path)
            yield game

    def _build_game(self, platform: str, source: str, collection: Path, paths: list[Path], metadata: dict, media_root: Path,
                    blocked_reason: str | None = None, *, layout=None) -> dict:
        definitions = platform_definitions()
        definition = definitions.get(platform, {})
        if platform == 'switch' and any(path.suffix.lower() in {'.nsz', '.xcz'} for path in paths):
            blocked_reason = blocked_reason or 'Switch 压缩格式 NSZ / XCZ 需先转换为 NSP / XCI，再更新游戏库'
        files: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            try:
                files.extend(dependency_files(self.rom_root, path, seen))
            except (OSError, ValueError):
                blocked_reason = blocked_reason or '光盘关联文件缺失或引用路径不安全'
                if path not in files:
                    files.append(path)
        size = 0
        unpacked = 0
        unknown_unpacked = False
        size_complete = True
        for path in files:
            try:
                if path.is_dir():
                    unknown_unpacked = True
                    size_complete = False
                    continue
                file_size = path.stat().st_size
                size += file_size
                if path.suffix.lower() in ARCHIVES:
                    actual, error = inspect_archive(path)
                    blocked_reason = blocked_reason or error
                    unknown_unpacked |= actual is None
                    unpacked += actual or 0
                else:
                    unpacked += file_size
            except OSError:
                blocked_reason = blocked_reason or '游戏文件暂时不可访问'
        if definition.get('kind') in {'dos', 'windows'} and any(path.suffix.lower() not in ARCHIVES for path in paths):
            unknown_unpacked = True
            size_complete = False
        core = definition.get('browser_core')
        hint = metadata.get('native_core')
        if hint and definition.get('arcade'):
            core = hint if hint in {'fbneo', 'fbalpha2012', 'mame2003_plus', 'mame2003'} else None
        # Loose disc sets require native launch; only self-contained payloads go to WASM.
        eligible = bool(core and len(files) == 1 and size <= BROWSER_LIMIT and not unknown_unpacked and unpacked <= BROWSER_LIMIT and not blocked_reason and paths[0].suffix.lower() not in DISC_LINKS)
        mode = 'browser' if eligible else ('stream' if definition and not blocked_reason else 'unavailable')
        reason = blocked_reason or ('在浏览器本地运行' if eligible else ('解压后超过 20 MiB，使用服务器运行' if unpacked > BROWSER_LIMIT else '使用服务器模拟器运行'))
        engine_hint = None
        if platform == 'windows' and paths[0].is_file():
            siblings = {p.name.lower() for p in paths[0].parent.iterdir() if p.is_file()}
            if {'pal.exe', 'abc.mkf', 'data.mkf', 'm.msg', 'word.dat'} <= siblings:
                engine_hint = 'sdlpal'
            # Only these complete classic PC layouts have been exercised with
            # our Wine image. Preserve the declared BAT as metadata, never run it.
            wine_layouts = {
                'rich4': {'rich4.exe', 'data.mkf', 'effect.mkf', 'panel.mkf'},
                'hsl01': {'hsl01.exe', 'hsl.pak', 'movie.pak'},
            }
            required = wine_layouts.get(paths[0].stem.casefold())
            if required and required <= siblings:
                engine_hint = 'wine'
        relative = layout.original_relative(paths[0]) if layout is not None else paths[0].relative_to(self.rom_root).as_posix()
        game_id = hashlib.sha256(relative.encode()).hexdigest()[:24]
        title = metadata.get('title') or paths[0].stem
        media: dict[str, str] = {}
        explicit = {'cover': ['assets.box_front', 'assets.boxfront', 'assets.poster'], 'video': ['assets.video'], 'logo': ['assets.logo']}
        defaults = {'cover': ['boxFront.jpg', 'boxFront.png', 'boxFront.webp', 'boxfront.jpg', 'boxfront.png', 'boxfront.webp', 'boxFront.jpeg', 'boxfront.jpeg', 'poster.jpg', 'cover.jpg'], 'video': ['video.mp4', 'video.webm'], 'logo': ['logo.png', 'logo.webp']}
        # Nested standalone ROMs can share the collection's original media/
        # directory. Prefer their own metadata/media root before this fallback.
        media_roots = list(dict.fromkeys([media_root, collection]))
        for root in media_roots:
            if root not in self._media_roots:
                self._media_roots[root] = (root / 'media').is_dir()
        for kind, keys in explicit.items():
            options: list[Path] = []
            for key in keys:
                if metadata.get(key):
                    try:
                        options.append(relative_reference(self.rom_root, media_root, metadata[key]))
                    except (ValueError, OSError):
                        pass
            for root in media_roots:
                if self._media_roots[root]:
                    for stem in dict.fromkeys([paths[0].stem, str(title), paths[0].parent.name]):
                        for filename in defaults[kind]:
                            options.append(root / 'media' / stem / filename)
            for option in options:
                try:
                    safe = contained_path(self.rom_root, option)
                    if safe.is_file():
                        media[kind] = str(safe)
                        break
                except (OSError, ValueError):
                    pass
        return {'id': game_id, 'title': title, 'platform': platform, 'platform_name': definition.get('name', platform),
                'collection': collection.name, 'source': source, 'size': size, 'size_complete': size_complete, 'unpacked_size': None if unknown_unpacked else unpacked,
                'mode': mode, 'browser_core': core, 'engine_hint': engine_hint, 'native_core': hint or definition.get('native_core'), 'browser_eligible': eligible, 'verified_playable': False,
                'compatibility_note': '街机 ROM 集需要匹配内核版本及父包/BIOS，启动后仍需实测' if definition.get('arcade') else '运行环境支持，具体游戏尚待实测', 'description': metadata.get('description', ''),
                'reason': reason, 'blocked_reason': blocked_reason, 'rom_path': str(paths[0]), 'files': [str(p) for p in files],
                'launch_files': [str(p) for p in paths], 'media': media, 'sort_by': metadata.get('sort-by', ''),
                'cover_url': f'/api/games/{game_id}/media/cover' if 'cover' in media else None,
                'video_url': f'/api/games/{game_id}/media/video' if 'video' in media else None,
                'logo_url': f'/api/games/{game_id}/media/logo' if 'logo' in media else None}

    def apply_runtime(self, capabilities: dict, browser_ready: bool, browser_bios: dict | None = None, browser_cores: set[str] | None = None) -> None:
        key = json.dumps([capabilities, browser_ready, browser_bios, sorted(browser_cores) if browser_cores is not None else None], sort_keys=True)
        if key == self._runtime_key:
            return
        platforms = capabilities.get('platforms', {})
        with self.connect() as db:
            # Native is the fallback even for small ROMs when this bundled
            # EmulatorJS release has no corresponding WASM core.
            for row in db.execute('SELECT DISTINCT platform FROM games').fetchall():
                platform = row[0]
                value = platforms.get(platform, {})
                available = value.get('available', False) if isinstance(value, dict) else bool(value)
                reason = value.get('reason') or '服务端尚未配置该平台模拟器' if isinstance(value, dict) else '服务端尚未配置该平台模拟器'
                native_reason = reason if isinstance(value, dict) and value.get('experimental') else '在服务器运行并串流画面；具体游戏仍待实测'
                db.execute("UPDATE games SET mode=?, reason=? WHERE platform=? AND blocked_reason IS NULL", ('stream' if available else 'unavailable', native_reason if available else reason, platform))
            sdlpal = capabilities.get('engines', {}).get('sdlpal', {})
            if sdlpal.get('available'):
                db.execute("UPDATE games SET mode='stream',reason='使用 SDLPAL 运行仙剑奇侠传并串流；具体游戏仍待实测' WHERE json_extract(payload,'$.engine_hint')='sdlpal' AND blocked_reason IS NULL")
            if capabilities.get('engines', {}).get('wine', {}).get('available'):
                db.execute("UPDATE games SET mode='stream',reason='使用 Wine 运行经典 Windows 游戏并串流' WHERE json_extract(payload,'$.engine_hint')='wine' AND blocked_reason IS NULL")
            if browser_ready:
                rows = db.execute("SELECT DISTINCT platform,json_extract(payload,'$.browser_core') core FROM games WHERE browser_eligible=1 AND blocked_reason IS NULL").fetchall()
                for row in rows:
                    bios_ok = browser_bios is None or browser_bios.get(row['platform'], True)
                    core_ok = browser_cores is None or row['core'] in browser_cores
                    if bios_ok and core_ok:
                        db.execute("UPDATE games SET mode='browser',reason='由浏览器本地运行；具体游戏仍待实测' WHERE platform=? AND browser_eligible=1 AND json_extract(payload,'$.browser_core')=? AND blocked_reason IS NULL", (row['platform'], row['core']))
                    elif not bios_ok:
                        db.execute("UPDATE games SET reason='游戏需要对应 BIOS；游戏已完整保留' WHERE platform=? AND browser_eligible=1 AND mode='unavailable'", (row['platform'],))
        if not self.scan_status['running']:
            self._runtime_key = key

    def _decode(self, row: sqlite3.Row) -> dict:
        game = json.loads(row['payload'])
        game.update(mode=row['mode'], reason=row['reason'], favorite=bool(row['favorite']), played_at=row['played_at'],
                    play_count=row['play_count'] if 'play_count' in row.keys() else 0,
                    added_at=game.get('first_added_at'))
        entry = json.loads(row['review_payload']) if 'review_payload' in row.keys() and row['review_payload'] else None
        game['review'] = self._reviews.public(game, entry, row['review_status'] if 'review_status' in row.keys() else None)
        try:
            game['launch_config'] = self.game_configs.summary(game, game['review'])
        except GameConfigError as error:
            game['launch_config'] = {'status': 'blocked', 'issues': [str(error)], 'verified_playable': False}
        if 'group_id' in row.keys() and row['group_id']:
            with self.connect() as db:
                members = [dict(r) for r in db.execute('''SELECT g.id,g.title,g.collection,coalesce(f.favorite,0) favorite,h.played_at
                    FROM games g JOIN game_reviews r ON r.game_id=g.id LEFT JOIN favorites f ON f.game_id=g.id
                    LEFT JOIN history h ON h.game_id=g.id WHERE r.group_id=? AND g.mode=?
                    ORDER BY h.played_at DESC,coalesce(f.favorite,0) DESC,g.id''', (row['group_id'], game['mode']))]
            game['review']['duplicates'] = {'group_id': row['group_id'], 'total_count': len(members), 'representative_id': members[0]['id'], 'members': members}
        return game

    def get(self, game_id: str) -> dict | None:
        self._sync_metadata()
        self._sync_reviews()
        with self.connect() as db:
            row = db.execute('''SELECT g.*,coalesce(f.favorite,0) favorite,h.played_at,coalesce(pc.play_count,0) play_count,r.payload review_payload,r.group_id,r.status review_status FROM games g
                LEFT JOIN favorites f ON f.game_id=g.id LEFT JOIN history h ON h.game_id=g.id LEFT JOIN game_play_counts pc ON pc.game_id=g.id LEFT JOIN game_reviews r ON r.game_id=g.id WHERE g.id=?''', (game_id,)).fetchone()
            return self._decode(row) if row else None

    def _query(self, *, q='', platform='', mode='', favorite=False, recent=False,
               review_filter='', sort='default', direction=None, count_only=False):
        if sort not in SORT_OPTIONS or direction not in (None, 'asc', 'desc'):
            raise ValueError('无效的游戏排序方式')
        where, values = ['1=1'], []
        if q:
            where.append("(g.title LIKE ? ESCAPE '\\' OR g.collection LIKE ? ESCAPE '\\')")
            query = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            values.extend([query, query])
        if platform:
            where.append('g.platform=?')
            values.append(platform)
        if mode:
            where.append('g.mode=?')
            values.append(mode)
        if favorite:
            where.append('f.favorite=1')
        if recent:
            where.append('h.played_at IS NOT NULL')
        if review_filter == 'needs_attention':
            where.append("r.status IN ('dependency_error','load_failed')")
        elif review_filter == 'observed':
            where.append("r.status IN ('boot_observed','input_observed','gameplay_observed')")
        elif review_filter == 'pending':
            where.append("coalesce(r.status,'unverified') NOT IN ('dependency_error','load_failed','boot_observed','input_observed','gameplay_observed')")
        clause = ' AND '.join(where)
        joins = 'FROM games g LEFT JOIN favorites f ON f.game_id=g.id LEFT JOIN history h ON h.game_id=g.id LEFT JOIN game_play_counts pc ON pc.game_id=g.id LEFT JOIN game_reviews r ON r.game_id=g.id'
        if count_only:
            # Counting identities does not need to sort each group or choose its
            # representative. Keep precisely the same filters and mode boundary.
            return f'''SELECT count(*) FROM (SELECT coalesce(r.group_id,g.id),g.mode
                {joins} WHERE {clause} GROUP BY coalesce(r.group_id,g.id),g.mode)''', '', values
        selection = f'''WITH ranked AS (SELECT g.id,coalesce(r.group_id,g.id) identity,g.mode,
            coalesce(f.favorite,0) favorite,h.played_at,coalesce(pc.play_count,0) play_count,
            row_number() OVER (PARTITION BY coalesce(r.group_id,g.id),g.mode ORDER BY h.played_at DESC,coalesce(f.favorite,0) DESC,g.id) choice
            {joins} WHERE {clause})'''
        if sort == 'default':
            order = "ranked.played_at DESC,g.id" if recent else "g.platform,g.collection,json_extract(g.payload,'$.sort_by'),g.title,g.id"
        else:
            expressions = {
                'name': 'g.title COLLATE NOCASE',
                'popularity': "json_extract(g.payload,'$.details.popularity.score')",
                'release_date': "json_extract(g.payload,'$.details.release_date')",
                'added_at': "json_extract(g.payload,'$.first_added_at')",
                'play_count': 'ranked.play_count', 'last_played': 'ranked.played_at',
            }
            expression = expressions[sort]
            direction = (direction or SORT_OPTIONS[sort]).upper()
            order = f'({expression} IS NULL) ASC,{expression} {direction},g.title COLLATE NOCASE ASC,g.id'
        return selection, order, values

    def list(self, *, q: str = '', platform: str = '', mode: str = '', favorite: bool = False, recent: bool = False, review_filter: str = '', offset: int = 0, limit: int = 60, sort: str = 'default', direction: str | None = None) -> dict:
        self._sync_metadata()
        self._sync_reviews()
        selection, order, values = self._query(q=q, platform=platform, mode=mode, favorite=favorite,
                                              recent=recent, review_filter=review_filter, sort=sort, direction=direction)
        count_query, _, _ = self._query(q=q, platform=platform, mode=mode, favorite=favorite,
                                       recent=recent, review_filter=review_filter, sort=sort, direction=direction,
                                       count_only=True)
        with self.connect() as db:
            total = db.execute(count_query, values).fetchone()[0]
            rows = db.execute(f'''{selection} SELECT g.*,ranked.favorite,ranked.played_at,ranked.play_count,r.payload review_payload,r.group_id,r.status review_status
                FROM ranked JOIN games g ON g.id=ranked.id LEFT JOIN game_reviews r ON r.game_id=g.id
                WHERE choice=1 ORDER BY {order} LIMIT ? OFFSET ?''', [*values, limit, offset]).fetchall()
        return {'games': [self._decode(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}

    def neighbors(self, game_id: str, **filters) -> dict:
        self._sync_metadata()
        self._sync_reviews()
        selection, order, values = self._query(**filters)
        with self.connect() as db:
            row = db.execute(f'''{selection}, ordered AS (
                SELECT ranked.id,ranked.identity,ranked.mode,row_number() OVER (ORDER BY {order}) - 1 position
                FROM ranked JOIN games g ON g.id=ranked.id WHERE choice=1), neighbors AS (
                SELECT *,lag(id) OVER (ORDER BY position) previous_id,lead(id) OVER (ORDER BY position) next_id,
                count(*) OVER () total FROM ordered)
                SELECT n.previous_id,n.next_id,n.position,n.total FROM neighbors n JOIN ranked target
                ON n.identity=target.identity AND n.mode=target.mode WHERE target.id=?''', [*values, game_id]).fetchone()
            if row:
                return {'previous_id': row['previous_id'], 'next_id': row['next_id'], 'index': row['position'], 'total': row['total']}
            count_query, _, _ = self._query(**filters, count_only=True)
            total = db.execute(count_query, values).fetchone()[0]
            return {'previous_id': None, 'next_id': None, 'index': None, 'total': total}

    def platforms(self) -> list[dict]:
        self._sync_reviews()
        definitions = platform_definitions()
        with self.connect() as db:
            rows = db.execute("""WITH entries AS (SELECT g.platform,g.mode,coalesce(r.group_id,g.id) identity FROM games g LEFT JOIN game_reviews r ON r.game_id=g.id GROUP BY g.platform,g.mode,identity)
                SELECT platform id,count(*) count,sum(mode='browser') browser_count,sum(mode='stream') stream_count FROM entries GROUP BY platform ORDER BY platform""").fetchall()
            statistics = {row['platform']: dict(row) for row in db.execute("SELECT g.platform,sum(coalesce(pc.play_count,0)) play_count,max(h.played_at) last_played_at FROM games g JOIN history h ON h.game_id=g.id LEFT JOIN game_play_counts pc ON pc.game_id=g.id GROUP BY g.platform")}
            return [dict(row, name=definitions.get(row['id'], {}).get('name', row['id']),
                         play_count=statistics.get(row['id'], {}).get('play_count', 0),
                         last_played_at=statistics.get(row['id'], {}).get('last_played_at')) for row in rows]

    def set_favorite(self, game_id: str, favorite: bool) -> None:
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO favorites VALUES(?,?)', (game_id, int(favorite)))

    def mark_played(self, game_id: str) -> None:
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO history VALUES(?,?)', (game_id, time.time()))
            db.execute('INSERT INTO game_play_counts VALUES(?,1) ON CONFLICT(game_id) DO UPDATE SET play_count=game_play_counts.play_count+1', (game_id,))


def public_game(game: dict) -> dict:
    hidden = {'rom_path', 'files', 'launch_files', 'media', 'browser_eligible', 'blocked_reason', '_save_title'}
    return {key: value for key, value in game.items() if key not in hidden}
