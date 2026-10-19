"""Durable member registry and a small, path-free provisioning outbox.

Only the API writes the registry. The host provisioner claims requests and
writes matching result files; it never receives commands, paths, or image names.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import threading
import time
from uuid import UUID, uuid4

PROFILE_ID = re.compile(r'player[1-9][0-9]{0,5}\Z')
AVATARS = {'pixel', 'leaf', 'star', 'rocket', 'cat'}
MAX_DOCUMENT = 1024 * 1024


class ProfileError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def atomic_json(path: Path, value: dict):
    """Replace a complete document, including fsync before publishing it."""
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(',', ':'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_document(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > MAX_DOCUMENT:
        raise ValueError('unsafe or oversized profile document')
    value = json.loads(path.read_text('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('expected object')
    return value


def identity(name: str, avatar: str) -> tuple[str, str]:
    if not isinstance(name, str):
        raise ProfileError('昵称需为文字', 422)
    name = name.strip()
    if not 1 <= len(name) <= 20 or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name):
        raise ProfileError('昵称需为 1 至 20 个字符，不能包含控制字符', 422)
    if avatar not in AVATARS:
        raise ProfileError('请选择预设头像', 422)
    return name, avatar


class ProfileRegistry:
    def __init__(self, config_root: Path, *, control_root: Path | None = None, limit: int = 16):
        if not 2 <= limit <= 32:
            raise ValueError('RETROONE_MAX_PROFILES must be between 2 and 32')
        self.path = Path(config_root) / 'profiles.json'
        self.control = Path(control_root or self.path.parent / 'profile-control')
        self.limit = limit
        self.thread_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for name in ('requests', 'running', 'results'):
            (self.control / name).mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        with self.thread_lock, (self.control / 'registry.lock').open('a+b') as handle:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise ProfileError('成员档案正在更新，请稍后重试')
                    time.sleep(.01)
            try:
                yield
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ProfileError('成员档案或准备记录无法读取，请检查服务日志；现有数据未重置') from error
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def default(identifier: str, name: str, avatar: str) -> dict:
        return {'id': identifier, 'name': name, 'avatar': avatar, 'configured': False,
                'status': 'ready', 'error': None, 'job_id': None, 'attempt': 0}

    def load(self) -> dict:
        if not self.path.exists():
            if (self.control / 'sequence.json').exists():
                raise ProfileError('成员档案文件缺失，已停止新增以保护既有成员编号')
            # A queue without a registry must not be interpreted as a fresh install.
            if any(any((self.control / name).iterdir()) for name in ('requests', 'running', 'results')):
                raise ProfileError('成员档案文件缺失，已停止新增以保护既有成员编号')
            source = {}
        else:
            source = read_document(self.path)
        if source.get('version') == 2:
            state = source
            if not isinstance(state.get('profiles'), dict) or not isinstance(state.get('requests'), dict):
                raise ValueError('invalid profile registry')
        elif 'version' in source:
            raise ValueError('unknown registry version')
        else:
            # Preserve the original two-profile metadata format in place.
            records = {'player1': self.default('player1', '玩家1', 'pixel'),
                       'player2': self.default('player2', '玩家2', 'leaf')}
            for key, value in source.items():
                if key not in records or not isinstance(value, dict):
                    raise ValueError('invalid legacy profile registry')
                try:
                    name, avatar = identity(value.get('name', ''), value.get('avatar', ''))
                except ProfileError as error:
                    raise ValueError('invalid legacy identity') from error
                records[key].update(name=name, avatar=avatar, configured=value.get('configured') is True)
            state = {'version': 2, 'profiles': records, 'requests': {}}
        for key, record in state['profiles'].items():
            if not PROFILE_ID.fullmatch(key) or not isinstance(record, dict) or record.get('id') != key:
                raise ValueError('invalid member identifier')
            try:
                identity(record.get('name', ''), record.get('avatar', ''))
            except ProfileError as error:
                raise ValueError('invalid member identity') from error
            if record.get('status') not in {'ready', 'provisioning', 'error'}:
                raise ValueError('invalid member status')
            if record.get('job_id') is not None and str(UUID(record['job_id'])) != record['job_id']:
                raise ValueError('invalid job identifier')
            if record['status'] == 'provisioning' and not record.get('job_id'):
                raise ValueError('missing preparation job')
        if not {'player1', 'player2'} <= state['profiles'].keys():
            raise ValueError('initial profiles missing')
        return state

    def reconcile(self, state: dict):
        changed = False
        for record in state['profiles'].values():
            if record['status'] != 'provisioning':
                continue
            job = record['job_id']
            result_path = self.control / 'results' / (job + '.json')
            if result_path.exists():
                result = read_document(result_path)
                if result.get('version') != 1 or result.get('profile_id') != record['id'] or result.get('job_id') != job or result.get('status') not in {'ready', 'error'}:
                    raise ValueError('mismatching preparation result')
                error = result.get('error') or '运行环境准备失败，请重试'
                if not isinstance(error, str):
                    raise ValueError('invalid preparation error')
                record.update(status=result['status'], error=error[:500] if result['status'] == 'error' else None)
                changed = True
            else:
                self.ensure_request(record)
        if changed:
            atomic_json(self.path, state)

    def ensure_request(self, record: dict):
        job = record['job_id']
        if any((self.control / folder / (job + '.json')).exists() for folder in ('requests', 'running', 'results')):
            return
        atomic_json(self.control / 'requests' / (job + '.json'), {
            'version': 1, 'operation': 'provision', 'profile_id': record['id'],
            'job_id': job, 'attempt': record['attempt'], 'created_at': record['created_at'],
        })

    @staticmethod
    def public(record: dict) -> dict:
        return {key: record[key] for key in ('id', 'name', 'avatar', 'configured', 'status', 'error')} | {'base_path': '/p/' + record['id']}

    def snapshot(self) -> dict:
        with self.locked():
            state = self.load()
            self.reconcile(state)
            records = sorted(state['profiles'].values(), key=lambda item: int(item['id'][6:]))
            return {'profiles': [self.public(record) for record in records],
                    'capacity': {'limit': self.limit, 'total': len(records), 'remaining': max(0, self.limit - len(records))}}

    def update(self, identifier: str, name: str, avatar: str) -> dict:
        name, avatar = identity(name, avatar)
        with self.locked():
            state = self.load()
            if identifier not in state['profiles']:
                raise ProfileError('玩家档案不存在', 404)
            record = state['profiles'][identifier]
            record.update(name=name, avatar=avatar, configured=True)
            atomic_json(self.path, state)
            return self.public(record)

    def create(self, name: str, avatar: str, request_id: str) -> dict:
        name, avatar = identity(name, avatar)
        try:
            request_id = str(UUID(request_id))
        except (ValueError, AttributeError, TypeError) as error:
            raise ProfileError('缺少有效的提交编号，请重新打开新增成员表单', 422) from error
        with self.locked():
            state = self.load()
            self.reconcile(state)
            previous = state['requests'].get(request_id)
            if previous:
                if previous['name'] != name or previous['avatar'] != avatar:
                    raise ProfileError('此次提交编号已用于其他成员信息，请重新提交', 409)
                return self.public(state['profiles'][previous['profile_id']])
            if len(state['profiles']) >= self.limit:
                raise ProfileError('成员数量已达当前上限，请联系管理员调整容量', 409)
            if not self.path.exists():
                atomic_json(self.path, state)
            highest = max(int(key[6:]) for key in state['profiles'])
            sequence = self.control / 'sequence.json'
            if sequence.exists():
                saved = read_document(sequence).get('last_allocated')
                if not isinstance(saved, int) or isinstance(saved, bool) or saved < highest:
                    raise ValueError('invalid member sequence')
                highest = saved
            if highest >= 999999:
                raise ProfileError('成员编号已用尽，请联系管理员', 409)
            number = highest + 1
            # Persist the high-water mark first; an interrupted write may skip an
            # ID but can never allocate it to another member on a later request.
            atomic_json(sequence, {'version': 1, 'last_allocated': number})
            identifier = 'player' + str(number)
            record = self.default(identifier, name, avatar)
            record.update(configured=True, status='provisioning', job_id=str(uuid4()), attempt=1, created_at=time.time())
            state['profiles'][identifier] = record
            state['requests'][request_id] = {'profile_id': identifier, 'name': name, 'avatar': avatar}
            atomic_json(self.path, state)
            self.ensure_request(record)
            return self.public(record)

    def retry(self, identifier: str) -> dict:
        with self.locked():
            state = self.load()
            self.reconcile(state)
            if identifier not in state['profiles']:
                raise ProfileError('玩家档案不存在', 404)
            record = state['profiles'][identifier]
            if record['status'] in {'ready', 'provisioning'}:
                return self.public(record)
            if record['attempt'] >= 1000:
                raise ProfileError('准备重试次数已达上限，请联系管理员检查运行环境', 409)
            record.update(status='provisioning', error=None, job_id=str(uuid4()),
                          attempt=record['attempt'] + 1, created_at=time.time())
            atomic_json(self.path, state)
            self.ensure_request(record)
            return self.public(record)
