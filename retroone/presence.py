"""Browser heartbeats, deliberately separate from persistent emulator sessions."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time

from .profiles import ProfileError


class PresenceStore:
    TTL = 75

    def __init__(self, config_root: Path):
        self.path = Path(config_root) / 'presence.sqlite'
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, activity TEXT NOT NULL, game_id TEXT, seen REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS clients_profile ON clients(profile_id, seen)')

    @contextmanager
    def connection(self):
        try:
            with sqlite3.connect(self.path, timeout=2) as db:
                db.row_factory = sqlite3.Row
                yield db
        except sqlite3.Error as error:
            raise ProfileError('在线状态暂时不可用，请稍后重试') from error
        finally:
            if 'db' in locals():
                db.close()

    def heartbeat(self, profile_id: str, client_id: str, activity: str, game_id: str | None):
        now = time.time()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM clients WHERE seen < ?', (now - self.TTL,))
            count = db.execute('SELECT count(*) FROM clients WHERE profile_id=? AND client_id!=?', (profile_id, client_id)).fetchone()[0]
            if count >= 64:
                raise ProfileError('此成员打开的浏览器页面过多，请关闭部分页面', 429)
            db.execute('INSERT INTO clients VALUES (?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET profile_id=excluded.profile_id,activity=excluded.activity,game_id=excluded.game_id,seen=excluded.seen', (client_id, profile_id, activity, game_id, now))
        return {'ok': True, 'ttl_seconds': self.TTL}

    def leave(self, profile_id: str, client_id: str):
        with self.connection() as db:
            # A late pagehide from the old profile cannot remove the same tab's
            # new profile after a fast switch.
            db.execute('DELETE FROM clients WHERE profile_id=? AND client_id=?', (profile_id, client_id))
        return {'ok': True}

    def snapshot(self) -> dict:
        now = time.time()
        with self.connection() as db:
            rows = db.execute('SELECT * FROM clients WHERE seen >= ? ORDER BY seen DESC', (now - self.TTL,)).fetchall()
        result = {}
        for row in rows:
            current = result.get(row['profile_id'])
            if not current:
                current = result[row['profile_id']] = {'online': True, 'last_seen': row['seen'], 'activity': row['activity'], 'game_id': row['game_id'], 'clients': 0}
            current['clients'] += 1
            if current['activity'] == 'library' and row['activity'] != 'library':
                current.update(activity=row['activity'], game_id=row['game_id'])
        return result

    @staticmethod
    def offline() -> dict:
        return {'online': False, 'last_seen': None, 'activity': None, 'game_id': None, 'game_title': None, 'clients': 0}
