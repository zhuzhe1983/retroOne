"""Explicit family invitations for game-native network play, never session takeover."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

from .profiles import PROFILE_ID

RIDGE_RACER_2 = 'f9eb2a58978f3b367e18a092'
VERIFIED_GAMES = {RIDGE_RACER_2: '山脊赛车2'}
INSTRUCTIONS = {
    'host': ['双方分别点击“启动我的游戏”，等待各自游戏标题画面。', '进入 WIRELESS BATTLE，选择创建比赛／建立房间。', '等待对方加入，确认双方都出现后开始比赛。'],
    'guest': ['点击“启动我的游戏”，等待自己的游戏标题画面。', '进入 WIRELESS BATTLE，选择查找比赛／加入房间。', '找到邀请人的比赛并加入，等待邀请人开始比赛。'],
}


class NetworkError(Exception):
    def __init__(self, message, status_code=409, code='network_conflict', **details):
        super().__init__(message)
        self.status_code = status_code
        self.detail = {'code': code, 'message': message, **details}


def valid_uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise NetworkError('联机编号无效', 422, 'invalid_network_id') from error


class NetworkStore:
    INVITE_TTL = 300
    ROOM_TTL = 2 * 60 * 60

    def __init__(self, config_root: Path):
        self.path = Path(config_root) / 'network.sqlite'
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS invites (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, from_profile_id TEXT NOT NULL, to_profile_id TEXT NOT NULL, game_id TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, room_id TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, host_profile_id TEXT NOT NULL, guest_profile_id TEXT NOT NULL, game_id TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS invites_members ON invites(from_profile_id,to_profile_id,status)')
            db.execute('CREATE INDEX IF NOT EXISTS rooms_members ON rooms(host_profile_id,guest_profile_id,status)')

    @contextmanager
    def connection(self, *, write=False):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=3)
            db.row_factory = sqlite3.Row
            with db:
                if write:
                    db.execute('BEGIN IMMEDIATE')
                yield db
        except sqlite3.Error as error:
            raise NetworkError('联机邀请暂时不可用，请稍后重试', 503, 'network_store_unavailable') from error
        finally:
            if db:
                db.close()

    @staticmethod
    def expire(db):
        now = time.time()
        db.execute("UPDATE invites SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
        db.execute("UPDATE rooms SET status='expired' WHERE status='ready' AND expires_at<=?", (now,))
        db.execute("UPDATE invites SET status='expired' WHERE status='accepted' AND room_id IN (SELECT id FROM rooms WHERE status='expired')")

    def existing_request(self, request_id, host, guest, game):
        request_id = valid_uuid(request_id)
        with self.connection() as db:
            row = db.execute('SELECT * FROM invites WHERE request_id=?', (request_id,)).fetchone()
        if row and (row['from_profile_id'], row['to_profile_id'], row['game_id']) != (host, guest, game):
            raise NetworkError('该提交编号已用于另一份邀请，请重新发起', 409, 'request_reused')
        return dict(row) if row else None

    def create(self, host, guest, game, request_id):
        request_id = valid_uuid(request_id)
        with self.connection(write=True) as db:
            self.expire(db)
            row = db.execute('SELECT * FROM invites WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if (row['from_profile_id'], row['to_profile_id'], row['game_id']) != (host, guest, game):
                    raise NetworkError('该提交编号已用于另一份邀请', 409, 'request_reused')
                return dict(row)
            for profile in (host, guest):
                pending = db.execute("SELECT id FROM invites WHERE status='pending' AND (from_profile_id=? OR to_profile_id=?)", (profile, profile)).fetchone()
                room = db.execute("SELECT id FROM rooms WHERE status='ready' AND (host_profile_id=? OR guest_profile_id=?)", (profile, profile)).fetchone()
                if pending or room:
                    raise NetworkError('该成员已有待处理邀请或联机房间，请先处理', 409, 'network_member_busy', profile_id=profile)
            now = time.time()
            row = {'id': str(uuid4()), 'request_id': request_id, 'from_profile_id': host, 'to_profile_id': guest, 'game_id': game, 'status': 'pending', 'created_at': now, 'expires_at': now + self.INVITE_TTL, 'room_id': None}
            db.execute('INSERT INTO invites VALUES (:id,:request_id,:from_profile_id,:to_profile_id,:game_id,:status,:created_at,:expires_at,:room_id)', row)
            return row

    def invite(self, identifier):
        identifier = valid_uuid(identifier)
        with self.connection(write=True) as db:
            self.expire(db)
            row = db.execute('SELECT * FROM invites WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise NetworkError('联机邀请不存在', 404, 'invite_not_found')
            return dict(row)

    def list(self, profile):
        with self.connection(write=True) as db:
            self.expire(db)
            return [dict(row) for row in db.execute('SELECT * FROM invites WHERE from_profile_id=? OR to_profile_id=? ORDER BY created_at DESC LIMIT 50', (profile, profile))]

    def respond(self, identifier, actor, action):
        identifier = valid_uuid(identifier)
        with self.connection(write=True) as db:
            self.expire(db)
            current = db.execute('SELECT * FROM invites WHERE id=?', (identifier,)).fetchone()
            if not current:
                raise NetworkError('联机邀请不存在', 404, 'invite_not_found')
            row = dict(current)
            expected_actor = row['from_profile_id'] if action == 'cancel' else row['to_profile_id']
            if actor != expected_actor:
                raise NetworkError('此操作不属于当前玩家', 403, 'wrong_invite_member')
            target = {'accept': 'accepted', 'decline': 'declined', 'cancel': 'cancelled'}[action]
            if row['status'] == target:
                return row
            if row['status'] != 'pending':
                raise NetworkError('邀请已处理或过期，请重新发起', 409, 'invite_not_pending')
            if action == 'accept':
                now = time.time()
                row['room_id'] = str(uuid4())
                db.execute('INSERT INTO rooms VALUES (?,?,?,?,?,?,?)', (row['room_id'], row['from_profile_id'], row['to_profile_id'], row['game_id'], 'ready', now, now + self.ROOM_TTL))
            row['status'] = target
            db.execute('UPDATE invites SET status=?,room_id=? WHERE id=?', (row['status'], row['room_id'], row['id']))
            return row

    def room(self, identifier):
        identifier = valid_uuid(identifier)
        with self.connection(write=True) as db:
            self.expire(db)
            row = db.execute('SELECT * FROM rooms WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise NetworkError('联机房间不存在', 404, 'room_not_found')
            return dict(row)

    def close(self, identifier, actor):
        identifier = valid_uuid(identifier)
        with self.connection(write=True) as db:
            row = db.execute('SELECT * FROM rooms WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise NetworkError('联机房间不存在', 404, 'room_not_found')
            if actor not in (row['host_profile_id'], row['guest_profile_id']):
                raise NetworkError('当前玩家不属于此房间', 403, 'wrong_room_member')
            db.execute("UPDATE rooms SET status='closed' WHERE id=?", (identifier,))
            db.execute("UPDATE invites SET status='cancelled' WHERE room_id=? AND status='accepted'", (identifier,))
            return dict(row, status='closed')


class WorkerGateway:
    """Only operator-configured, internal routes; no client-controlled URL."""
    def __init__(self, coordinator='http://retroone:3000'):
        self.coordinator = coordinator.rstrip('/')
        self.opener = build_opener(ProxyHandler({}))

    def get(self, url):
        try:
            with self.opener.open(Request(url, headers={'Accept': 'application/json'}), timeout=4) as response:
                data = response.read(256 * 1024 + 1)
            if len(data) > 256 * 1024:
                raise ValueError('oversized worker response')
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError('invalid worker response')
            return result
        except (OSError, ValueError) as error:
            raise NetworkError('玩家服务暂时无法连接，请稍后重试', 503, 'worker_unavailable') from error

    def session(self, profile):
        if not PROFILE_ID.fullmatch(profile):
            raise NetworkError('玩家编号无效', 422, 'invalid_profile_id')
        return self.get(self.coordinator + '/p/' + profile + '/api/session').get('session')

    def room(self, identifier):
        return self.get(self.coordinator + '/api/network/rooms/' + valid_uuid(identifier))
