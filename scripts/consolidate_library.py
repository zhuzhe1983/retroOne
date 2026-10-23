#!/usr/bin/env python3
"""Offline, reversible removal of library provenance directories.

The default is a read-only plan. Apply/rollback require --workers-stopped;
the caller must stop every catalog, game and provisioning worker first.
Collections are moved intact, never merged, overwritten or deduplicated.
Backups and a crash-recoverable journal live outside the ROM directory.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, contextmanager
import copy
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SOURCES = ('new', 'old', 'pegasus', 'import')
STAT_FIELDS = ('dev', 'ino', 'size', 'mtime_ns', 'ctime_ns')
MANIFEST = '.retroone/library-layout.json'
REQUIRED = '.retroone/library-layout.required'
_last_progress = 0.0


class MigrationError(RuntimeError):
    pass


def progress(message, *, force=False):
    """Keep stdout machine-readable; bound diagnostics during million-file walks."""
    global _last_progress
    now = time.monotonic()
    if force or now - _last_progress >= 5:
        print('[consolidate] ' + message, file=sys.stderr, flush=True)
        _last_progress = now


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise MigrationError('Invalid relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != value:
        raise MigrationError('Unsafe relative path: ' + value)
    return value


def safe_path(path):
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise MigrationError('Symlink is not supported: ' + str(item))
    return path


def file_identity(path):
    path = safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise MigrationError('Evidence source is not a regular file: ' + str(path))
    return {key: getattr(info, 'st_' + key) for key in STAT_FIELDS} | {'executable': info.st_mode & 0o111}


def attributes(path):
    info = path.stat()
    attrs = dict(mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid,
                 atime_ns=info.st_atime_ns, mtime_ns=info.st_mtime_ns)
    if hasattr(os, 'listxattr'):
        try:
            attrs['xattrs'] = {name: base64.b64encode(os.getxattr(path, name)).decode('ascii')
                              for name in os.listxattr(path)}
        except OSError as error:
            if error.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
    return attrs


def set_owner_mode(path, attrs):
    os.chmod(path, attrs['mode'])
    current = path.stat()
    if (current.st_uid, current.st_gid) != (attrs['uid'], attrs['gid']):
        os.chown(path, attrs['uid'], attrs['gid'])
    for name, value in attrs.get('xattrs', {}).items():
        os.setxattr(path, name, base64.b64decode(value, validate=True))


def atomic_json(path, value, attrs=None):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if attrs is None and path.exists():
        attrs = attributes(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.layout-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write((encoded(value) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
            if attrs:
                set_owner_mode(temporary, attrs)
            else:
                temporary.chmod(0o644)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(path):
    return json.loads(safe_path(path).read_text('utf-8'))


def no_replace_rename(source, destination):
    """Use the OS no-replace operation; a check followed by rename can clobber."""
    source, destination = safe_path(source), safe_path(destination)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        result = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        result = libc.renamex_np(os.fsencode(source), os.fsencode(destination), 4)
    else:
        raise MigrationError('Atomic no-replace directory rename is unavailable on this OS')
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def _references(path):
    """Read only path-bearing metadata, never alter collection content."""
    kind = 'pegasus' if path.name.lower().endswith('.pegasus.txt') else path.suffix.lower()
    if kind not in ('pegasus', '.cue', '.m3u', '.m3u8', '.gdi'):
        return []
    if path.stat().st_size > 16 * 1024**2:
        raise MigrationError('Reference document exceeds preflight limit: ' + str(path))
    text = path.read_bytes().decode('utf-8-sig', errors='surrogateescape')
    result, current = [], ''
    for number, line in enumerate(text.splitlines(), 1):
        value = None
        if kind == 'pegasus':
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            if line[:1].isspace():
                if current in ('file', 'files') or current.startswith('assets.'):
                    value = line.strip()
            else:
                match = re.match(r'^([\w.-]+):\s*(.*)$', line)
                current = match[1].lower() if match else ''
                if match and (current in ('file', 'files') or current.startswith('assets.')):
                    value = match[2].strip()
        elif kind in ('.m3u', '.m3u8'):
            value = line.strip() if not line.lstrip().startswith('#') else None
        elif kind == '.cue':
            match = re.match(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))\s+\S+\s*$', line, re.I)
            if match:
                value = match[1] or match[2]
            elif re.match(r'^\s*FILE\b', line, re.I):
                raise MigrationError(f'Cannot parse CUE reference: {path}:{number}')
        elif kind == '.gdi' and line.strip():
            try:
                parts = shlex.split(line, posix=True)
            except ValueError as error:
                raise MigrationError(f'Cannot parse GDI reference: {path}:{number}') from error
            if len(parts) >= 6:
                value = parts[4]
            elif not (len(parts) == 1 and parts[0].isdigit()):
                raise MigrationError(f'Cannot parse GDI reference: {path}:{number}')
        if value and not re.match(r'^https?://', value, re.I):
            result.append((number, value))
    return result


def _check_references(path, collection=None, moving_roots=()):
    for number, value in _references(path):
        value = value.strip().strip('"').replace('\\', '/')
        absolute = value.startswith('/') or bool(re.match(r'^[A-Za-z]:', value))
        target = Path(os.path.abspath(path.parent / value))
        if collection is not None:
            if absolute or not target.is_relative_to(collection):
                raise MigrationError(f'Reference leaves its collection: {path}:{number}: {value}')
        elif any(target == item or item in target.parents for item in moving_roots) or (absolute and '/library/' in value):
            raise MigrationError(f'External metadata references a moving collection: {path}:{number}: {value}')


def _check_tree(directory):
    # scandir's type information avoids a stat for every file in large PC trees.
    pending, inspected = [directory], 0
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                inspected += 1
                if inspected % 256 == 0:
                    progress(f'Checking {directory.name}: {inspected:,} entries')
                if entry.is_symlink():
                    raise MigrationError('Collection contains a symlink; review before moving: ' + entry.path)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif not entry.is_file(follow_symlinks=False):
                    raise MigrationError('Collection contains a special file: ' + entry.path)
                else:
                    _check_references(Path(entry.path), directory)


def plan_layout(rom_root):
    from retroone.library_layout import LibraryLayout
    root = safe_path(Path(rom_root).resolve(strict=True))
    library = safe_path(root / 'library')
    progress('Planning collection moves and checking references', force=True)
    previous = LibraryLayout.load(root).document
    if previous.get('schema_version') != 1 or not isinstance(previous.get('collections'), list):
        raise MigrationError('Unsupported layout manifest')
    existing = set()
    for entry in previous['collections']:
        path, original = relative(entry['path']), relative(entry['original_path'])
        if len(PurePosixPath(path).parts) != 3 or len(PurePosixPath(original).parts) != 4:
            raise MigrationError('Unexpected collection manifest path')
        if path in existing:
            raise MigrationError('Duplicate manifest collection')
        existing.add(path)
    if (root / REQUIRED).exists() and not (root / MANIFEST).is_file():
        raise MigrationError('Required layout manifest is missing; restore it before planning')
    moves, source_dirs, leftover, unhandled = [], [], [], []
    fold = lambda name: unicodedata.normalize('NFC', name).casefold()
    for platform in sorted(library.iterdir()):
        if not platform.is_dir():
            continue
        safe_path(platform)
        occupied = {fold(item.name) for item in platform.iterdir()}
        unhandled.extend(item.relative_to(root).as_posix() for item in platform.iterdir()
                         if item.is_dir() and item.name not in SOURCES
                         and item.relative_to(root).as_posix() not in existing)
        for source_name in SOURCES:
            source = platform / source_name
            if not source.exists() or source.relative_to(root).as_posix() in existing:
                continue
            safe_path(source)
            if not source.is_dir():
                raise MigrationError('Source layer is not a directory: ' + str(source))
            source_dirs.append({'path': source.relative_to(root).as_posix(), 'attributes': attributes(source)})
            for collection in sorted(source.iterdir()):
                safe_path(collection)
                if not collection.is_dir():
                    leftover.append(collection.relative_to(root).as_posix())
                    continue
                name = collection.name
                index = 2
                while fold(name) in occupied:
                    name = collection.name + '（合集 ' + str(index) + '）'
                    index += 1
                occupied.add(fold(name))
                destination = platform / name
                info = collection.stat()
                if info.st_dev != platform.stat().st_dev:
                    raise MigrationError('Collection must remain on the same filesystem')
                progress('Checking ' + collection.relative_to(root).as_posix())
                _check_tree(collection)
                moves.append({'original_path': collection.relative_to(root).as_posix(),
                              'path': destination.relative_to(root).as_posix(),
                              'directory_identity': {'dev': info.st_dev, 'ino': info.st_ino}})
    moving_roots = {root / move['original_path'] for move in moves}
    # Metadata outside collections can name old source-relative paths too. Walk
    # only untouched trees; collections were already checked above.
    for directory, dirs, files in os.walk(library, followlinks=False):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if parent / name not in moving_roots
                   and not (parent / name).is_symlink()]
        for name in files:
            path = parent / name
            if not path.is_symlink():
                _check_references(path, moving_roots=moving_roots)
    progress(f'Plan ready: {len(moves)} collections; {len(unhandled)} other directories retained', force=True)
    return {'schema_version': 1, 'rom_root': str(root), 'moves': moves,
            'previous_manifest': previous, 'source_dirs': source_dirs,
            'unmoved_source_files': leftover, 'unhandled_directories': sorted(unhandled),
            'sources_in_scope': list(SOURCES), 'collection_count': len(moves)}


class PathMap:
    def __init__(self, root, moves, prefixes=()):
        self.root = Path(root)
        self.moves = [(relative(m['original_path']), relative(m['path'])) for m in moves]
        self.prefixes = sorted(set([str(root), '/roms', *map(str, prefixes)]), key=len, reverse=True)

    def split(self, value):
        if not isinstance(value, str):
            return None
        if value.startswith('/'):
            for prefix in self.prefixes:
                if value.startswith(prefix.rstrip('/') + '/'):
                    return prefix.rstrip('/'), value[len(prefix.rstrip('/')) + 1:]
            return None
        if value.startswith('library/'):
            return '', value
        return None

    def rewrite(self, value):
        split = self.split(value)
        if split is None:
            return value
        prefix, rel = split
        relative(rel)
        for old, new in self.moves:
            if rel == old or rel.startswith(old + '/'):
                return (prefix + '/' if prefix else '') + new + rel[len(old):]
        return value

    def actual(self, value):
        split = self.split(value)
        if split is None:
            raise MigrationError('Unrecognized source prefix: ' + str(value))
        return self.root / relative(split[1])

    def nested(self, value):
        if isinstance(value, str):
            return self.rewrite(value)
        if isinstance(value, list):
            return [self.nested(item) for item in value]
        if isinstance(value, dict):
            return {key: self.nested(item) for key, item in value.items()}
        return value


class Evidence:
    def __init__(self, mapping):
        self.mapping, self.before = mapping, {}

    def capture(self, value):
        if value not in self.before:
            try:
                self.before[value] = file_identity(self.mapping.actual(value))
            except (OSError, MigrationError):
                self.before[value] = None
        return self.before[value]

    def matches(self, value, expected):
        actual = self.capture(value)
        return bool(actual and all(actual.get(key) == item for key, item in expected.items()))

    def unchanged(self, value):
        before = self.before.get(value)
        if before is None:
            return False
        try:
            return file_identity(self.mapping.actual(self.mapping.rewrite(value))) == before
        except (OSError, MigrationError):
            return False


@contextmanager
def exclusive_lock(path):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+b') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MigrationError('Worker/cache is busy: ' + str(path)) from error
        yield


def sqlite_connect(path, writable=False):
    path = safe_path(path)
    return sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'), uri=True, timeout=2)


def _validate_profile_bindings(profiles, root, previous):
    from retroone.library_layout import canonical_manifest
    known = {item['path']: item['original_path'] for item in previous['collections']}
    for profile in profiles:
        with sqlite_connect(profile / 'catalog.sqlite3') as db:
            settings = dict(db.execute('SELECT key,value FROM settings'))
        if settings.get('library_layout_required') == '1' and not (root / REQUIRED).is_file():
            raise MigrationError('Profile requires the missing layout seal: ' + str(profile))
        if 'library_layout_bindings' not in settings:
            continue
        saved = canonical_manifest({'schema_version': 1, 'collections': json.loads(settings['library_layout_bindings'])})
        if any(known.get(item['path']) != item['original_path'] for item in saved['collections']):
            raise MigrationError('Existing profile layout bindings changed or disappeared: ' + str(profile))


def _storage_digest(item, state):
    """Hash committed logical content, so SQLite WAL/checkpoints do not matter."""
    path = safe_path(item['path'])
    if not path.exists():
        return None
    result = hashlib.sha256()
    if item['kind'] != 'sqlite':
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                result.update(block)
    else:
        profile = path in {Path(p) / 'catalog.sqlite3' for p in state['profiles']}
        with sqlite_connect(path) as db:
            db.execute('BEGIN')
            tables = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            for table, schema in tables:
                if profile and table in ('games', 'game_reviews'):
                    continue  # Rebuildable scan caches, not family state.
                query = 'SELECT * FROM "' + table.replace('"', '""') + '"'
                rows = db.execute(query).fetchall()
                if profile and table == 'settings':
                    rows = [row for row in rows if row[0] != 'last_scan']
                def normalize(row):
                    return encoded([{'blob_hex': v.hex()} if isinstance(v, bytes) else v for v in row])
                result.update(encoded([table, schema]).encode())
                for row in sorted(normalize(row) for row in rows):
                    result.update(row.encode()); result.update(b'\n')
    return result.hexdigest()


def _record_committed_storage(state):
    cache_paths = set(state.get('cache_paths', []))
    state['committed_storage'] = [dict(path=item['path'], kind=item['kind'],
        sha256=_storage_digest(item, state)) for item in state['backups'] if item['path'] not in cache_paths]


def _check_committed_storage(state):
    if state['status'] == 'complete' and not state.get('committed_storage'):
        raise MigrationError('Completed migration has no rollback content baseline')
    for item in state.get('committed_storage', []):
        if _storage_digest(item, state) != item['sha256']:
            raise MigrationError('Storage changed since migration; refusing rollback: ' + item['path'])


def discover_caches(data_root):
    result = []
    for folder in (data_root / 'metadata/audit', data_root / 'private-audits'):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob('*')):
            if path.suffix not in ('.sqlite', '.sqlite3', '.db') or not path.is_file():
                continue
            safe_path(path)
            try:
                with sqlite_connect(path) as db:
                    found = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_hashes_v1'").fetchone()
                    if found:
                        columns = {row[1] for row in db.execute('PRAGMA table_info(audit_hashes_v1)')}
                        if columns != {'path', 'identity', 'kind', 'payload'}:
                            raise MigrationError('Unsupported audit cache schema')
                        result.append(path)
            except sqlite3.DatabaseError:
                continue
    return result


def _review_prepare(document, evidence):
    result = copy.deepcopy(document)
    valid = {}
    for identifier, entry in result.get('games', {}).items():
        sources = entry.get('sources', [])
        status = entry.get('verification', {}).get('status')
        valid[identifier] = bool(sources) and status != 'stale'
        for source in sources:
            expected = {key: source[key] for key in ('size', 'mtime_ns', 'ctime_ns', 'ino') if key in source}
            good = len(expected) == 4 and evidence.matches(source.get('relative_path'), expected)
            valid[identifier] = valid[identifier] and good
    return result, valid


def _review_finish(prepared, evidence, report):
    document, valid = prepared
    mapping = evidence.mapping
    for identifier, entry in document.get('games', {}).items():
        for source in entry.get('sources', []):
            value = source.get('relative_path')
            if not evidence.unchanged(value):
                valid[identifier] = False
            source['relative_path'] = mapping.rewrite(value)
        for key in ('primary', 'resource_root'):
            if key in entry:
                entry[key] = mapping.rewrite(entry[key])
        if not valid[identifier] and entry.get('fingerprint'):
            entry.pop('fingerprint')
            entry.setdefault('verification', {})['status'] = 'stale'
            report['stale_evidence_preserved'] += 1
        elif entry.get('fingerprint'):
            report['fingerprints_preserved'] += 1
    document['known_sources'] = mapping.nested(document.get('known_sources', []))
    groups = document.get('groups', [])
    document['groups'] = [g for g in groups if g.get('game_ids') and all(valid.get(i, False) for i in g['game_ids'])]
    report['evidence_groups_preserved'] += len(document['groups'])
    report['stale_groups_disabled'] += len(groups) - len(document['groups'])
    return document


class Transaction:
    def __init__(self, folder, state):
        self.folder, self.state = folder, state
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        (folder / 'backups').mkdir()
        self.save()

    def save(self):
        atomic_json(self.folder / 'transaction.json', self.state)

    def log(self, event, **fields):
        with (self.folder / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(encoded({'at': timestamp(), 'event': event, **fields}) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def backup(self, path, database=False):
        path = safe_path(path)
        if any(item['path'] == str(path) for item in self.state['backups']):
            return
        item = {'path': str(path), 'existed': path.exists(), 'kind': 'sqlite' if database else 'file'}
        if path.exists():
            if not path.is_file():
                raise MigrationError('Expected a regular storage file: ' + str(path))
            item['attributes'] = attributes(path)
            item['backup'] = 'backups/' + str(len(self.state['backups']))
            target = self.folder / item['backup']
            if database:
                with sqlite_connect(path) as source, sqlite3.connect(target) as destination:
                    source.backup(destination)
                    if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise MigrationError('Database backup failed integrity check')
            else:
                shutil.copy2(path, target)
        self.state['backups'].append(item)
        progress(f'Backing up state: {len(self.state["backups"]):,} files')


def _inputs(root, data, profiles, artwork, mapping, evidence):
    inputs = {'catalogs': [], 'metadata': None, 'configs': [], 'markers': [], 'artwork': None, 'review': None, 'caches': []}
    for profile in profiles:
        progress('Reading player state: ' + profile.name)
        path = safe_path(profile / 'catalog.sqlite3')
        with sqlite_connect(path) as db:
            rows = []
            for identifier, payload in db.execute('SELECT id,payload FROM games'):
                game = json.loads(payload)
                if game.get('id') != identifier:
                    raise MigrationError('Catalog ID and payload disagree')
                changed = copy.deepcopy(game)
                for key in ('rom_path', 'files', 'launch_files', 'media', 'resource_root'):
                    if key in changed:
                        changed[key] = mapping.nested(changed[key])
                if changed != game:
                    original = mapping.split(game.get('rom_path'))
                    for old, new in mapping.moves:
                        if original and (original[1] == old or original[1].startswith(old + '/')):
                            changed['collection'] = PurePosixPath(new).name
                            changed['source'] = ''
                            break
                    rows.append((identifier, changed))
            reviews = []
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='game_reviews'").fetchone():
                for identifier, payload in db.execute('SELECT game_id,payload FROM game_reviews'):
                    entry = json.loads(payload)
                    reviews.append((identifier, _review_prepare({'games': {identifier: entry}}, evidence)))
            # Require the real schema rather than inventing a partial catalog.
            db.execute('SELECT key,value FROM settings LIMIT 0')
        inputs['catalogs'].append((path, rows, reviews))
        work = profile / 'work'
        if work.exists():
            markers = set()
            for pattern in ('*/.retroone-extracted.json', '*/content/.retroone-extracted.json',
                            '*/disc-*/content/.retroone-extracted.json'):
                markers.update(work.glob(pattern))
            for marker in sorted(markers):
                safe_path(marker)
                try:
                    document = read_json(marker)
                except (ValueError, UnicodeError):
                    continue  # Never touch malformed or incomplete working copies.
                value = document.get('path')
                if (mapping.rewrite(value) != value and evidence.matches(value, {
                        'size': document.get('size'), 'mtime_ns': document.get('mtime_ns')})):
                    inputs['markers'].append((marker, document))
    metadata_path = data / 'metadata/games.sqlite3'
    if metadata_path.exists():
        with sqlite_connect(metadata_path) as db:
            revision = db.execute('SELECT revision FROM metadata_state WHERE singleton=1').fetchone()[0]
            changes = []
            for identifier, text in db.execute('SELECT game_id,generated FROM game_metadata'):
                value = json.loads(text)
                updated = mapping.nested(value.get('media', {}))
                if updated != value.get('media', {}):
                    value['media'] = updated
                    changes.append((identifier, value))
        inputs['metadata'] = (metadata_path, revision, changes)
    for path in sorted((data / 'runtime/game-configs').glob('*.json')):
        document = read_json(path)
        generated = document.get('generated', {})
        identity = generated.get('asset_identity')
        if not isinstance(identity, dict) or generated.get('source_signature') != digest(identity):
            raise MigrationError('Invalid generated configuration; refusing to repair implicitly: ' + str(path))
        updated = copy.deepcopy(identity)
        def assets(value):
            if isinstance(value, dict):
                return {k: mapping.rewrite(v) if k in ('relative_path', 'resolved_relative_path') else assets(v) for k, v in value.items()}
            if isinstance(value, list):
                return [assets(v) for v in value]
            return value
        updated = assets(updated)
        if 'sources' in updated:
            updated['sources'].sort(key=encoded)
        if updated != identity:
            document['generated']['asset_identity'] = updated
            document['generated']['source_signature'] = digest(updated)
            inputs['configs'].append((path, document))
    review_path = data / 'metadata/library-review.json'
    if review_path.exists():
        inputs['review'] = (review_path, _review_prepare(read_json(review_path), evidence))
    if artwork and (artwork / 'local-overrides.json').exists():
        path = artwork / 'local-overrides.json'
        document = read_json(path)
        for entry in document.get('games', {}).values():
            if 'source_path' in entry:
                entry['source_path'] = mapping.rewrite(entry['source_path'])
        inputs['artwork'] = (path, document)
    return inputs


def _rewrite_stores(inputs, mapping, evidence, report, manifest):
    for path, changes, reviews in inputs['catalogs']:
        with sqlite_connect(path, True) as db:
            for identifier, game in changes:
                db.execute('UPDATE games SET collection=?,payload=? WHERE id=?', (game['collection'], encoded(game), identifier))
            for identifier, prepared in reviews:
                result = _review_finish(prepared, evidence, {key: 0 for key in report})
                entry = result['games'][identifier]
                status = entry.get('verification', {}).get('status', 'unverified')
                db.execute('UPDATE game_reviews SET payload=?,status=?,group_id=CASE WHEN ? THEN NULL ELSE group_id END WHERE game_id=?',
                           (encoded(entry), status, status == 'stale', identifier))
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('library_layout_required','1')")
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('library_layout_bindings',?)",
                       (encoded(manifest['collections']),))
        report['catalog_records_relocated'] += len(changes)
        report['catalogs_updated'] += 1
    if inputs['metadata']:
        path, revision, changes = inputs['metadata']
        with sqlite_connect(path, True) as db:
            for identifier, value in changes:
                db.execute('UPDATE game_metadata SET generated=?,revision=? WHERE game_id=?', (encoded(value), revision + 1, identifier))
            if changes:
                db.execute('UPDATE metadata_state SET revision=? WHERE singleton=1', (revision + 1,))
        report['metadata_media_relocated'] += len(changes)
    for path, document in inputs['configs']:
        atomic_json(path, document)
        report['runtime_configs_relocated'] += 1
    for path, document in inputs['markers']:
        if evidence.unchanged(document['path']):
            document['path'] = mapping.rewrite(document['path'])
            atomic_json(path, document)
            report['extraction_markers_relocated'] += 1
    if inputs['artwork']:
        atomic_json(*inputs['artwork'])
    if inputs['review']:
        path, prepared = inputs['review']
        atomic_json(path, _review_finish(prepared, evidence, report))
    for path, rows in inputs['caches']:
        with sqlite_connect(path, True) as db:
            for old, identity, kind, payload in rows:
                if evidence.unchanged(old):
                    key = mapping.rewrite(old)
                    existing = db.execute('SELECT payload FROM audit_hashes_v1 WHERE path=? AND identity=? AND kind=?', (key, identity, kind)).fetchone()
                    if existing and existing[0] != payload:
                        raise MigrationError('Conflicting full-identity hash-cache row')
                    db.execute('INSERT OR IGNORE INTO audit_hashes_v1 VALUES(?,?,?,?)', (key, identity, kind, payload))
                    report['cache_rows_relocated'] += 1


def _restore(folder, state):
    if state.get('status') == 'rolled_back':
        return {'status': 'rolled_back', 'backup_path': str(folder), 'already_rolled_back': True,
                'collections_restored': 0}
    progress('Restoring collection directories and backed-up state', force=True)
    root = Path(state['rom_root'])
    # Directory identity is checked before anything is restored or overwritten.
    for move in state['moves']:
        old, new = root / relative(move['original_path']), root / relative(move['path'])
        expected = move['directory_identity']
        found = old if old.exists() else new
        if not found.is_dir() or found.is_symlink() or any(getattr(found.stat(), 'st_' + k) != v for k, v in expected.items()):
            raise MigrationError('Collection changed; refusing rollback: ' + str(found))
    for source in state['source_dirs']:
        path = safe_path(root / relative(source['path']))
        if not path.exists():
            path.mkdir()
            set_owner_mode(path, source['attributes'])
    for move in reversed(state['moves']):
        old, new = root / move['original_path'], root / move['path']
        if not old.exists():
            no_replace_rename(new, old)
    for item in reversed(state['backups']):
        path = safe_path(item['path'])
        if not item['existed']:
            if path.exists():
                if not path.is_file():
                    raise MigrationError('Unexpected object at new metadata path')
                path.unlink()
            continue
        backup = safe_path(folder / relative(item['backup']))
        if item['kind'] == 'sqlite':
            with sqlite_connect(backup) as source, sqlite3.connect(path) as target:
                source.backup(target)
            set_owner_mode(path, item['attributes'])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copy2(backup, temporary)
                set_owner_mode(temporary, item['attributes'])
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    for source in state['source_dirs']:
        attrs = source['attributes']
        os.utime(root / source['path'], ns=(attrs['atime_ns'], attrs['mtime_ns']))
    state['status'] = 'rolled_back'
    state['rolled_back_at'] = timestamp()
    atomic_json(folder / 'transaction.json', state)
    return {'status': 'rolled_back', 'backup_path': str(folder), 'collections_restored': len(state['moves'])}


def rollback(backup_directory, *, workers_stopped=False):
    if not workers_stopped:
        raise MigrationError('Rollback requires --workers-stopped')
    folder = safe_path(Path(backup_directory).resolve(strict=True))
    state = read_json(folder / 'transaction.json')
    if state.get('schema_version') != 1 or state.get('operation') != 'consolidate-library':
        raise MigrationError('Not a library-consolidation journal')
    expected_parent = Path(state['library_data_root']) / 'private-migrations'
    if folder.parent != expected_parent:
        raise MigrationError('Journal is outside its recorded migration directory')
    with ExitStack() as locks:
        for lock in state['lock_paths']:
            locks.enter_context(exclusive_lock(Path(lock)))
        if state['status'] != 'rolled_back':
            progress('Checking for user changes before rollback', force=True)
            _check_committed_storage(state)
        return _restore(folder, state)


def consolidate(rom_root, library_data_root, profile_config_roots=(), artwork_root=None, *,
                apply=False, workers_stopped=False, path_prefixes=()):
    plan = plan_layout(rom_root)
    root = Path(plan['rom_root'])
    data = safe_path(Path(library_data_root).resolve(strict=True))
    profiles = [safe_path(Path(p).resolve(strict=True)) for p in profile_config_roots]
    if len(set(profiles)) != len(profiles):
        raise MigrationError('Duplicate profile-config roots')
    artwork = safe_path(Path(artwork_root).resolve(strict=True)) if artwork_root else None
    state_roots = [data, *profiles, *([artwork] if artwork else [])]
    if any(root == path or root in path.parents for path in state_roots):
        raise MigrationError('Shared/player/artwork state must remain outside the ROM root')
    _validate_profile_bindings(profiles, root, plan['previous_manifest'])
    if not apply:
        return dict(plan, dry_run=True, profiles=[str(p) for p in profiles],
                    requires_workers_stopped=True, physical_deduplication=False)
    if not workers_stopped:
        raise MigrationError('Apply requires --workers-stopped')
    if not profiles:
        raise MigrationError('Supply every --profile-config-root before applying')
    if not plan['moves']:
        return {'status': 'unchanged', 'collections_moved': 0, 'dry_run': False}
    mapping = PathMap(root, plan['moves'], path_prefixes)
    evidence = Evidence(mapping)
    from retroone.library_layout import canonical_manifest, manifest_digest
    manifest = copy.deepcopy(plan['previous_manifest'])
    manifest['collections'].extend({k: move[k] for k in ('path', 'original_path')} for move in plan['moves'])
    manifest = canonical_manifest(manifest)
    with ExitStack() as locks:
        progress('Acquiring audit and cache locks', force=True)
        lock_paths = [data / 'metadata/audit/worker.lock', data / 'private-migrations/worker.lock']
        for path in lock_paths:
            locks.enter_context(exclusive_lock(path))
        caches = discover_caches(data)
        for cache in caches:
            path = cache.with_suffix(cache.suffix + '.lock')
            locks.enter_context(exclusive_lock(path))
            lock_paths.append(path)
        # A path plan is rechecked while all cooperative workers are excluded.
        if plan_layout(root)['moves'] != plan['moves']:
            raise MigrationError('Layout changed while acquiring locks')
        inputs = _inputs(root, data, profiles, artwork, mapping, evidence)
        progress('Validating cached file identities', force=True)
        for cache in caches:
            rows = []
            with sqlite_connect(cache) as db:
                for path, identity, kind, payload in db.execute('SELECT path,identity,kind,payload FROM audit_hashes_v1'):
                    expected = json.loads(identity)
                    if (isinstance(expected, dict) and set(expected) == {*STAT_FIELDS, 'executable'}
                            and mapping.rewrite(path) != path and evidence.matches(path, expected)):
                        rows.append((path, identity, kind, payload))
            inputs['caches'].append((cache, rows))
        folder = data / 'private-migrations' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:10])
        state = dict(schema_version=1, operation='consolidate-library', status='preparing', started_at=timestamp(),
                     rom_root=str(root), library_data_root=str(data), profiles=[str(p) for p in profiles],
                     artwork_root=str(artwork) if artwork else None, moves=plan['moves'], source_dirs=plan['source_dirs'],
                     lock_paths=[str(p) for p in lock_paths], cache_paths=[str(p) for p in caches], backups=[])
        tx = Transaction(folder, state)
        report = {key: 0 for key in ('collections_moved', 'source_directories_removed', 'catalogs_updated',
                  'catalog_records_relocated', 'metadata_media_relocated', 'runtime_configs_relocated',
                  'extraction_markers_relocated', 'fingerprints_preserved', 'stale_evidence_preserved',
                  'evidence_groups_preserved', 'stale_groups_disabled', 'cache_rows_relocated')}
        try:
            for path, _, _ in inputs['catalogs']:
                tx.backup(path, True)
            if inputs['metadata']:
                tx.backup(inputs['metadata'][0], True)
            for key in ('configs', 'markers'):
                for path, _ in inputs[key]:
                    tx.backup(path)
            for key in ('review', 'artwork'):
                if inputs[key]:
                    tx.backup(inputs[key][0])
            for path, _ in inputs['caches']:
                tx.backup(path, True)
            tx.backup(root / MANIFEST)
            tx.backup(root / REQUIRED)
            state['status'] = 'prepared'
            tx.save()
            tx.log('backups_complete', files=len(state['backups']))
            progress(f'Backups complete; moving {len(plan["moves"])} collections', force=True)
            for move in plan['moves']:
                tx.log('rename_intent', **move)
                no_replace_rename(root / move['original_path'], root / move['path'])
                report['collections_moved'] += 1
                progress(f'Moved {report["collections_moved"]}/{len(plan["moves"])}: {move["path"]}')
            state['status'] = 'directories_moved'
            tx.save()
            progress('Relocating catalog, metadata, configs and verified evidence', force=True)
            _rewrite_stores(inputs, mapping, evidence, report, manifest)
            atomic_json(root / MANIFEST, manifest)
            atomic_json(root / REQUIRED, {'schema_version': 1, 'manifest_sha256': manifest_digest(manifest)})
            for source in plan['source_dirs']:
                path = root / source['path']
                try:
                    path.rmdir()  # Only succeeds when truly empty; no recursive removal.
                    report['source_directories_removed'] += 1
                except OSError as error:
                    if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
            report.update(status='complete', backup_path=str(folder), ids_preserved=True,
                          favorites_history_playcounts_unchanged=True, saves_untouched=True,
                          physical_deduplication=False, source_files_retained=plan['unmoved_source_files'],
                          unhandled_directories=plan['unhandled_directories'], sources_in_scope=list(SOURCES))
            progress('Recording rollback content baselines', force=True)
            _record_committed_storage(state)
            state['status'] = 'complete'
            state['report'] = report
            tx.save()
            tx.log('complete', **report)
            atomic_json(folder / 'report.json', report)
            progress('Migration complete: ' + str(folder), force=True)
            return report
        except BaseException as error:
            tx.log('failed', error=str(error))
            try:
                _restore(folder, state)
                tx.log('automatic_rollback_complete')
            except BaseException as rollback_error:
                state['status'] = 'rollback_failed'
                state['rollback_error'] = str(rollback_error)
                tx.save()
                raise MigrationError('Migration and rollback failed; keep workers stopped. Journal: ' + str(folder)) from error
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rom-root', type=Path)
    parser.add_argument('--library-data-root', type=Path)
    parser.add_argument('--profile-config-root', type=Path, action='append', default=[])
    parser.add_argument('--artwork-root', type=Path)
    parser.add_argument('--path-prefix', action='append', default=[], help='Additional historic absolute ROM prefixes')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--workers-stopped', action='store_true')
    parser.add_argument('--rollback', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.apply and args.dry_run or args.rollback and (args.apply or args.dry_run):
        parser.error('Choose plan, apply or rollback')
    try:
        if args.output:
            destination = safe_path(args.output)
            if destination.exists():
                raise MigrationError('--output must be a new file; refusing to overwrite')
            output_root = args.rom_root
            if args.rollback:
                output_root = Path(read_json(args.rollback / 'transaction.json')['rom_root'])
            if output_root and destination.is_relative_to(Path(output_root).resolve()):
                raise MigrationError('--output must remain outside the ROM root')
        if args.rollback:
            result = rollback(args.rollback, workers_stopped=args.workers_stopped)
        else:
            if not args.rom_root or not args.library_data_root:
                parser.error('--rom-root and --library-data-root are required')
            result = consolidate(args.rom_root, args.library_data_root, args.profile_config_root, args.artwork_root,
                                 apply=args.apply, workers_stopped=args.workers_stopped, path_prefixes=args.path_prefix)
        if args.output:
            atomic_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MigrationError, OSError, ValueError, sqlite3.Error) as error:
        print('Consolidation failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
