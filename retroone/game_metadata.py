"""Shared presentation metadata, independent of ROMs, launch settings and saves.

``root`` is RETROONE_LIBRARY_DATA_ROOT/metadata, outside the read-only ROM root.
SQLite transactions serialize writers across threads/processes. A committed batch
advances one revision; unchanged imports do not write or advance it. Consumers
materialize changes_since() into their own search indexes and retain that returned
revision only after their local transaction succeeds. There are no deletions:
missing scan entries retain their original IDs and manual edits.

Media is imported, never editable through overrides. References are stored relative
to rom_root; internal effective entries expand them for the current ROM mount.
summary() exposes only text and media kinds, never those filesystem references.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import stat


SCHEMA_VERSION = 1
TEXT_LIMITS = {'title': 512, 'description': 65536, 'sort_by': 1024}
MEDIA_KINDS = {'cover', 'logo', 'video'}
GAME_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


class GameMetadataError(RuntimeError):
    def __init__(self, code, message, status_code=409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _identifier(value):
    if not isinstance(value, str) or not GAME_ID.fullmatch(value):
        raise GameMetadataError('invalid_game_id', '无效的游戏 ID', 422)
    return value


def _text(key, value):
    if (type(value) is not str or len(value) > TEXT_LIMITS[key] or '\x00' in value or
            (key == 'title' and not value.strip())):
        raise GameMetadataError('invalid_metadata', '无效或过长的游戏资料字段：' + key, 422)
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise GameMetadataError('invalid_metadata', '游戏资料包含无效字符：' + key, 422) from error
    return value


def _patch(value):
    if not isinstance(value, dict) or set(value) - set(TEXT_LIMITS):
        raise GameMetadataError('invalid_metadata', '只能修改名称、简介和排序文字', 422)
    return {key: None if item is None else _text(key, item) for key, item in value.items()}


class GameMetadataStore:
    def __init__(self, root: Path, rom_root: Path):
        # Check the original spelling before resolve() can hide a symlink.
        self.root = Path(os.path.abspath(root))
        self.rom_root = Path(rom_root).resolve()
        if _inside(self.root.resolve(), self.rom_root):
            raise GameMetadataError('unsafe_metadata_path', '共享游戏资料必须保存在 ROM 目录之外', 503)
        self._check_directory()
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        except OSError as error:
            raise GameMetadataError('metadata_io_error', '无法创建共享游戏资料目录', 503) from error
        self.db_path = self.root / 'games.sqlite3'
        self._initialize()

    def _check_directory(self):
        try:
            for path in (self.root, *self.root.parents):
                if path.is_symlink():
                    raise GameMetadataError('unsafe_metadata_path', '共享游戏资料目录不能是符号链接', 503)
                if path.exists() and not path.is_dir():
                    raise GameMetadataError('unsafe_metadata_path', '共享游戏资料路径不是目录', 503)
        except OSError as error:
            raise GameMetadataError('metadata_io_error', '无法检查共享游戏资料目录', 503) from error

    def _check_database_paths(self):
        self._check_directory()
        try:
            for suffix in ('', '-wal', '-shm', '-journal'):
                path = Path(str(self.db_path) + suffix)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise GameMetadataError('unsafe_metadata_path', '共享游戏资料文件不能是符号链接或特殊文件', 503)
        except OSError as error:
            raise GameMetadataError('metadata_io_error', '无法检查共享游戏资料文件', 503) from error

    @contextmanager
    def _connect(self, *, write=False, initialize=False):
        self._check_database_paths()
        db = None
        try:
            # After initialization, never silently replace a missing database.
            mode = 'rwc' if initialize else ('rw' if write else 'ro')
            db = sqlite3.connect(self.db_path.as_uri() + '?mode=' + mode,
                                 uri=True, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA busy_timeout=30000')
            if initialize:
                db.execute('PRAGMA journal_mode=WAL')
            if write:
                db.execute('PRAGMA synchronous=FULL')
            else:
                db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except sqlite3.Error as error:
            busy = isinstance(error, sqlite3.OperationalError) and any(word in str(error).lower() for word in ('locked', 'busy'))
            raise GameMetadataError('metadata_busy' if busy else 'metadata_database_error',
                                    '共享游戏资料正在更新，请稍后重试' if busy else '共享游戏资料无法读取或保存；请检查原数据库', 503) from error
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()

    def _initialize(self):
        with self._connect(write=True, initialize=True) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                if tables:
                    raise GameMetadataError('metadata_schema_error', '共享游戏资料版本不支持；原数据库已保留', 503)
                db.execute('CREATE TABLE metadata_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL CHECK(revision>=0))')
                db.execute('INSERT INTO metadata_state VALUES (1,0)')
                db.execute('CREATE TABLE game_metadata (game_id TEXT PRIMARY KEY, generated TEXT NOT NULL, overrides TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0))')
                db.execute('CREATE INDEX metadata_revision ON game_metadata(revision)')
                db.execute('PRAGMA user_version=1')
            elif version != SCHEMA_VERSION:
                raise GameMetadataError('metadata_schema_error', '共享游戏资料版本不支持；原数据库已保留', 503)
            self._revision(db)
            db.execute('SELECT game_id,generated,overrides,revision FROM game_metadata LIMIT 0')

    @staticmethod
    def _revision(db):
        row = db.execute('SELECT revision FROM metadata_state WHERE singleton=1').fetchone()
        if row is None or type(row[0]) is not int or row[0] < 0:
            raise GameMetadataError('metadata_corrupt', '共享游戏资料版本记录损坏；原数据库已保留', 503)
        return row[0]

    def _media(self, value):
        if not isinstance(value, dict) or set(value) - MEDIA_KINDS:
            raise GameMetadataError('invalid_metadata_media', '游戏媒体引用类型无效', 422)
        result = {}
        for kind, reference in value.items():
            if type(reference) is not str or not reference or len(reference) > 8192 or '\x00' in reference or '\\' in reference or '://' in reference:
                raise GameMetadataError('invalid_metadata_media', '游戏媒体引用无效', 422)
            path = Path(reference)
            if not path.is_absolute():
                path = self.rom_root / path
            if '..' in path.parts or not _inside(path, self.rom_root) or path == self.rom_root:
                raise GameMetadataError('invalid_metadata_media', '游戏媒体引用必须位于 ROM 目录内', 422)
            try:
                # Missing files remain references, but symlink traversal is never imported.
                if path.resolve() != path:
                    raise GameMetadataError('invalid_metadata_media', '游戏媒体引用不能经过符号链接', 422)
                if path.exists() and not path.is_file():
                    raise GameMetadataError('invalid_metadata_media', '游戏媒体引用必须指向文件', 422)
            except (OSError, RuntimeError) as error:
                raise GameMetadataError('invalid_metadata_media', '无法检查游戏媒体引用', 422) from error
            result[kind] = path.relative_to(self.rom_root).as_posix()
        return result

    def _generated(self, game):
        if not isinstance(game, dict):
            raise GameMetadataError('invalid_metadata', '游戏资料必须是对象', 422)
        return {**{key: _text(key, game.get(key, '' if key != 'title' else None)) for key in TEXT_LIMITS},
                'media': self._media(game.get('media', {}))}

    @staticmethod
    def _decode(row):
        try:
            generated = json.loads(row['generated'])
            overrides = json.loads(row['overrides'])
            if not isinstance(generated, dict) or set(generated) != set(TEXT_LIMITS) | {'media'}:
                raise ValueError('invalid fields')
            for key in TEXT_LIMITS:
                _text(key, generated[key])
            if _patch(overrides) != overrides or any(value is None for value in overrides.values()):
                raise ValueError('invalid overrides')
            media = generated['media']
            if not isinstance(media, dict) or set(media) - MEDIA_KINDS:
                raise ValueError('invalid media kinds')
            for reference in media.values():
                if (type(reference) is not str or not reference or len(reference) > 8192 or
                        '\x00' in reference or '\\' in reference or '://' in reference or Path(reference).is_absolute() or
                        '..' in Path(reference).parts or reference == '.'):
                    raise ValueError('invalid media path')
            _identifier(row['game_id'])
            if type(row['revision']) is not int or row['revision'] < 1:
                raise ValueError('invalid revision')
            return generated, overrides
        except (ValueError, TypeError, KeyError, GameMetadataError) as error:
            raise GameMetadataError('metadata_corrupt', '游戏资料记录损坏；原数据库已保留', 503) from error

    def _effective(self, row):
        generated, overrides = self._decode(row)
        return {'id': row['game_id'], **generated, **overrides,
                'media': {kind: str(self.rom_root / reference) for kind, reference in generated['media'].items()}}

    def _summary(self, row):
        generated, overrides = self._decode(row)
        original = {key: generated[key] for key in TEXT_LIMITS}
        return {'id': row['game_id'], 'revision': row['revision'],
                'effective': {**original, **overrides}, 'generated': original,
                'overrides': overrides, 'overridden_fields': sorted(overrides),
                'media_kinds': sorted(generated['media'])}

    def import_many(self, games, only_missing=False):
        """Import a scan in one transaction; only_missing bootstraps old caches.

        Returns the current global revision. Any invalid entry rolls back the
        entire batch. Existing records are untouched by only_missing imports.
        """
        if type(only_missing) is not bool:
            raise GameMetadataError('invalid_metadata', 'only_missing 必须是布尔值', 422)
        with self._connect(write=True) as db:
            previous = self._revision(db)
            changed = False
            for game in games:
                if not isinstance(game, dict):
                    raise GameMetadataError('invalid_metadata', '游戏资料必须是对象', 422)
                identifier = _identifier(game.get('id'))
                row = db.execute('SELECT * FROM game_metadata WHERE game_id=?', (identifier,)).fetchone()
                if row is not None:
                    self._decode(row)
                    if only_missing:
                        continue
                generated = _encoded(self._generated(game))
                if row is not None and row['generated'] == generated:
                    continue
                if row is None:
                    db.execute('INSERT INTO game_metadata VALUES (?,?,?,?)', (identifier, generated, '{}', previous + 1))
                else:
                    db.execute('UPDATE game_metadata SET generated=?,revision=? WHERE game_id=?', (generated, previous + 1, identifier))
                changed = True
            if changed:
                db.execute('UPDATE metadata_state SET revision=? WHERE singleton=1', (previous + 1,))
            return previous + int(changed)

    def revision(self):
        with self._connect() as db:
            return self._revision(db)

    def changes_since(self, revision):
        """Return one consistent snapshot; -1/0 request all effective entries.

        A cursor newer than this database also requests a full snapshot (e.g.
        after restoring an older backup). This API does not delete consumer rows.
        """
        if type(revision) is not int or revision < -1:
            raise GameMetadataError('invalid_metadata_revision', '无效的资料版本号', 422)
        with self._connect() as db:
            current = self._revision(db)
            since = 0 if revision <= 0 or revision > current else revision
            rows = db.execute('SELECT * FROM game_metadata WHERE revision>? ORDER BY game_id', (since,))
            return current, [self._effective(row) for row in rows]

    def apply(self, game):
        """Return an independent copy; never modify runtime or save-name fields."""
        identifier = _identifier(game.get('id'))
        result = copy.deepcopy(game)
        with self._connect() as db:
            row = db.execute('SELECT * FROM game_metadata WHERE game_id=?', (identifier,)).fetchone()
            if row is not None:
                effective = self._effective(row)
                result.update({key: effective[key] for key in (*TEXT_LIMITS, 'media')})
        return result

    def summary(self, game_id):
        identifier = _identifier(game_id)
        with self._connect() as db:
            row = db.execute('SELECT * FROM game_metadata WHERE game_id=?', (identifier,)).fetchone()
            if row is None:
                raise GameMetadataError('metadata_not_found', '游戏资料尚未导入', 404)
            return self._summary(row)

    def update_overrides(self, game_id, patch):
        identifier = _identifier(game_id)
        patch = _patch(patch)
        with self._connect(write=True) as db:
            row = db.execute('SELECT * FROM game_metadata WHERE game_id=?', (identifier,)).fetchone()
            if row is None:
                raise GameMetadataError('metadata_not_found', '游戏资料尚未导入', 404)
            _, overrides = self._decode(row)
            merged = dict(overrides)
            for key, value in patch.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            if merged != overrides:
                revision = self._revision(db) + 1
                db.execute('UPDATE game_metadata SET overrides=?,revision=? WHERE game_id=?', (_encoded(merged), revision, identifier))
                db.execute('UPDATE metadata_state SET revision=? WHERE singleton=1', (revision,))
                row = db.execute('SELECT * FROM game_metadata WHERE game_id=?', (identifier,)).fetchone()
            return self._summary(row)
