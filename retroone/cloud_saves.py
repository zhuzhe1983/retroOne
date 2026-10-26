"""Versioned PSP in-game saves. Never exports states, firmware or ROM content."""
from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
from uuid import UUID, uuid4

MAX_BYTES = 32 * 1024 * 1024
MAX_FILES = 256
MAX_BODY = 45 * 1024 * 1024
LEASE_SECONDS = 90
GAME_ID = re.compile(r'^[a-f0-9]{24}$')
COMPONENT = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_. -]{0,95}$')


class CloudSaveError(Exception):
    def __init__(self, code, message, status_code=409):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def normalize_files(files):
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise CloudSaveError('invalid_snapshot', 'Too many save files', 413)
    result, total, seen = [], 0, set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'data'}:
            raise CloudSaveError('invalid_snapshot', 'Invalid save file record', 422)
        path, encoded = item['path'], item['data']
        if not isinstance(path, str) or not isinstance(encoded, str):
            raise CloudSaveError('invalid_snapshot', 'Save paths and data must be strings', 422)
        parts = path.split('/')
        if (not 2 <= len(parts) <= 4 or any(not COMPONENT.fullmatch(p) or p in {'.', '..'} for p in parts)
                or path.casefold() in seen or any(p.lower().endswith(('.state', '.keys', '.key')) for p in parts)):
            raise CloudSaveError('invalid_snapshot', 'Invalid or duplicated SAVEDATA-relative path', 422)
        if len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
            raise CloudSaveError('too_large', 'Save snapshot exceeds 32 MiB', 413)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise CloudSaveError('invalid_snapshot', 'Invalid save file encoding', 422) from error
        total += len(data)
        if total > MAX_BYTES:
            raise CloudSaveError('too_large', 'Save snapshot exceeds 32 MiB', 413)
        seen.add(path.casefold())
        result.append({'path': path, 'data': base64.b64encode(data).decode('ascii')})
    result.sort(key=lambda value: value['path'])
    # Files must not also serve as directories.
    paths = {entry['path'].casefold() for entry in result}
    if any('/'.join(entry['path'].casefold().split('/')[:i]) in paths
           for entry in result for i in range(1, len(entry['path'].split('/')))):
        raise CloudSaveError('invalid_snapshot', 'Conflicting save paths', 422)
    return result


def encoded(files):
    return json.dumps(files, ensure_ascii=True, separators=(',', ':'))


def fingerprint(files):
    return hashlib.sha256(encoded(files).encode()).hexdigest()


class CloudSaveStore:
    def __init__(self, config_root: Path, *, clock=time.time):
        self.root = Path(config_root).absolute()
        self.clock = clock
        self.lock = threading.RLock()
        self._directory(('cloud-saves',), create=True).close()
        self.db = self.root / 'cloud-saves' / 'games.sqlite3'
        if self.db.is_symlink():
            raise CloudSaveError('unsafe_path', 'Cloud save database must not be a link', 503)
        with self._transaction() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS snapshots (
                game TEXT NOT NULL, revision INTEGER NOT NULL, files TEXT NOT NULL,
                digest TEXT NOT NULL, source TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(game, revision));
                CREATE TABLE IF NOT EXISTS current (game TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                pending INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS leases (game TEXT PRIMARY KEY, client TEXT NOT NULL,
                token TEXT NOT NULL, expires REAL NOT NULL);''')

    @contextmanager
    def _transaction(self):
        with self.lock:
            if self.db.is_symlink():
                raise CloudSaveError('unsafe_path', 'Cloud save database must not be a link', 503)
            db = sqlite3.connect(self.db, timeout=5)
            db.row_factory = sqlite3.Row
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    class Directory:
        def __init__(self, fd): self.fd = fd
        def close(self): os.close(self.fd)
        def __enter__(self): return self.fd
        def __exit__(self, *_): self.close()

    def _directory(self, parts, *, create=False):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in parts:
                if create:
                    try: os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError: pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return self.Directory(fd)
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _game(game):
        if not GAME_ID.fullmatch(str(game.get('id', ''))):
            raise CloudSaveError('invalid_game', 'Invalid game identity', 422)
        if game.get('platform') != 'psp':
            raise CloudSaveError('unsupported', 'Only PPSSPP in-game saves are supported', 422)
        return game['id']

    @staticmethod
    def _parts(game_id):
        return ('saves', game_id, 'PPSSPP', 'PSP')

    def _native(self, game_id):
        files, total = [], 0
        def walk(fd, prefix=''):
            nonlocal total
            for name in sorted(os.listdir(fd)):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                path = prefix + name
                if not COMPONENT.fullmatch(name) or len(path.split('/')) > 4:
                    raise CloudSaveError('unsafe_path', 'Unsupported native save path')
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try: walk(child, path + '/')
                    finally: os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    if len(files) >= MAX_FILES or total + info.st_size > MAX_BYTES:
                        raise CloudSaveError('too_large', 'Native save snapshot exceeds limits', 413)
                    source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    with os.fdopen(source, 'rb') as handle:
                        before = os.fstat(handle.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise CloudSaveError('unsafe_path', 'Save file changed while reading')
                        data = handle.read(MAX_BYTES - total + 1)
                        after = os.fstat(handle.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise CloudSaveError('native_changed', 'Native save changed while reading')
                    total += len(data)
                    if total > MAX_BYTES: raise CloudSaveError('too_large', 'Native save exceeds limits', 413)
                    files.append({'path': path, 'data': base64.b64encode(data).decode('ascii')})
                else:
                    raise CloudSaveError('unsafe_path', 'Save directory contains a link or non-file')
        try:
            directory = self._directory(self._parts(game_id) + ('SAVEDATA',))
        except FileNotFoundError:
            return []
        except OSError as error:
            raise CloudSaveError('unsafe_path', 'Native save directory is not safely accessible', 503) from error
        try:
            with directory as fd: walk(fd)
        except OSError as error:
            raise CloudSaveError('native_changed', 'Native save files changed while reading', 409) from error
        return normalize_files(files)

    @staticmethod
    def _current(db, game_id):
        return db.execute('SELECT s.*, c.pending FROM current c JOIN snapshots s ON s.game=c.game AND s.revision=c.revision WHERE c.game=?', (game_id,)).fetchone()

    def _record(self, db, game_id, files, source, *, pending=0):
        old = self._current(db, game_id)
        revision = old['revision'] + 1 if old else 0
        db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?)', (game_id, revision, encoded(files), fingerprint(files), source, self.clock()))
        db.execute('INSERT INTO current VALUES (?,?,?) ON CONFLICT(game) DO UPDATE SET revision=excluded.revision,pending=excluded.pending', (game_id, revision, pending))
        # Keep the current and 15 earlier full snapshots as recoverable backups.
        db.execute('DELETE FROM snapshots WHERE game=? AND revision<?', (game_id, revision - 15))
        return self._current(db, game_id)

    def _apply(self, game_id, files):
        # A committed pending snapshot survives interruption. All writes and
        # renames stay relative to opened directory descriptors, never a second
        # pathname lookup through a possibly replaced player directory.
        with self._directory(self._parts(game_id), create=True) as parent:
            stage = 'SAVEDATA.stage-' + uuid4().hex
            backup = 'SAVEDATA.previous-' + uuid4().hex
            os.mkdir(stage, 0o700, dir_fd=parent)
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                for item in files:
                    parts = item['path'].split('/')
                    fd = os.dup(stage_fd)
                    try:
                        for name in parts[:-1]:
                            try: os.mkdir(name, 0o700, dir_fd=fd)
                            except FileExistsError: pass
                            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                            os.close(fd)
                            fd = child
                        output_fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                        with os.fdopen(output_fd, 'wb') as output:
                            output.write(base64.b64decode(item['data']))
                            output.flush()
                            os.fsync(output.fileno())
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                os.fsync(stage_fd)
                try:
                    info = os.stat('SAVEDATA', dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(info.st_mode):
                        raise CloudSaveError('unsafe_path', 'Native SAVEDATA is not a directory')
                    os.rename('SAVEDATA', backup, src_dir_fd=parent, dst_dir_fd=parent)
                except FileNotFoundError: pass
                os.rename(stage, 'SAVEDATA', src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                try: os.stat(backup, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError: pass
                else: shutil.rmtree(backup, dir_fd=parent)
            finally:
                os.close(stage_fd)
                try: os.stat(stage, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError: pass
                else: shutil.rmtree(stage, dir_fd=parent)

    def _recover(self, db, game_id):
        row = self._current(db, game_id)
        if row and row['pending']:
            self._apply(game_id, json.loads(row['files']))
            db.execute('UPDATE current SET pending=0 WHERE game=?', (game_id,))
        return row

    def _refresh(self, db, game_id):
        self._recover(db, game_id)
        files = self._native(game_id)
        row = self._current(db, game_id)
        if not row or row['digest'] != fingerprint(files):
            row = self._record(db, game_id, files, 'native')
        return row

    @staticmethod
    def _view(row, *, include_files=False):
        files = json.loads(row['files'])
        result = {'revision': row['revision'], 'digest': row['digest'], 'source': row['source'],
                  'updated_at': row['created'], 'file_count': len(files),
                  'size_bytes': sum(len(base64.b64decode(item['data'])) for item in files)}
        if include_files: result['files'] = files
        return result

    def status(self, game):
        if game.get('platform') != 'psp': return {'supported': False, 'reason': 'Only PSP in-game saves are supported'}
        game_id = self._game(game)
        with self._transaction() as db:
            row = self._current(db, game_id)
            lease = db.execute('SELECT expires FROM leases WHERE game=?', (game_id,)).fetchone()
            return {'supported': True, 'format': 'ppsspp-savedata-v1', 'max_bytes': MAX_BYTES,
                    'leased': bool(lease and lease['expires'] > self.clock()),
                    'snapshot': self._view(row) if row else None}

    def acquire(self, game, client_id):
        game_id = self._game(game)
        client_id = str(UUID(str(client_id)))
        with self._transaction() as db:
            lease = db.execute('SELECT * FROM leases WHERE game=?', (game_id,)).fetchone()
            if lease and lease['expires'] > self.clock() and lease['client'] != client_id:
                raise CloudSaveError('lease_busy', 'This game is already open in another browser')
            row = self._refresh(db, game_id)
            token = lease['token'] if lease and lease['client'] == client_id and lease['expires'] > self.clock() else str(uuid4())
            expiry = self.clock() + LEASE_SECONDS
            db.execute('INSERT OR REPLACE INTO leases VALUES (?,?,?,?)', (game_id, client_id, token, expiry))
            return {'lease_id': token, 'expires_at': expiry, **self._view(row, include_files=True)}

    def _lease(self, db, game_id, token):
        row = db.execute('SELECT * FROM leases WHERE game=?', (game_id,)).fetchone()
        if not row or row['token'] != str(token) or row['expires'] <= self.clock():
            raise CloudSaveError('lease_expired', 'Browser save lease expired; reconnect before continuing')
        return row

    def heartbeat(self, game, token):
        game_id = self._game(game)
        with self._transaction() as db:
            self._lease(db, game_id, token)
            expiry = self.clock() + LEASE_SECONDS
            db.execute('UPDATE leases SET expires=? WHERE game=?', (expiry, game_id))
            return {'expires_at': expiry}

    def release(self, game, token):
        game_id = self._game(game)
        with self._transaction() as db:
            db.execute('DELETE FROM leases WHERE game=? AND token=?', (game_id, str(token)))
        return {'released': True}

    def upload(self, game, token, base_revision, files):
        game_id = self._game(game)
        if type(base_revision) is not int or base_revision < 0:
            raise CloudSaveError('invalid_revision', 'Invalid base revision', 422)
        files = normalize_files(files)
        with self._transaction() as db:
            self._lease(db, game_id, token)
            row = self._refresh(db, game_id)
            if row['digest'] == fingerprint(files): return self._view(row)
            if row['revision'] != base_revision:
                raise CloudSaveError('revision_conflict', 'Cloud and local saves both changed; neither was overwritten')
            row = self._record(db, game_id, files, 'browser', pending=1)
            result = self._view(row)
        # Persist the recovery record BEFORE materializing native files.
        with self._transaction() as db: self._recover(db, game_id)
        return result

    def assert_native_available(self, game):
        if game.get('platform') != 'psp': return
        game_id = self._game(game)
        with self._transaction() as db:
            lease = db.execute('SELECT expires FROM leases WHERE game=?', (game_id,)).fetchone()
            if lease and lease['expires'] > self.clock():
                raise CloudSaveError('browser_active', 'This game is open in a browser; finish save sync and close it first')
            self._recover(db, game_id)
