"""Private, single-file WebTorrent manifests backed by the existing HTTP server.

No ROM is copied or published. Only piece hashes are cached; every download is
resolved again through the catalog's independent file-download checks in the API.
"""
from __future__ import annotations

import base64
import copy
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from urllib.parse import urlsplit

PIECE_LENGTH = 1024 * 1024
MAX_PIECES = 4096
READ_BLOCK_BYTES = 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024
MAX_JOB_RECORDS = 64
_hash_slots = threading.BoundedSemaphore(2)
ICE_ENV = 'RETROONE_WEBRTC_ICE_SERVERS'


class IceConfigurationError(ValueError):
    """Operator configuration failure; messages never echo URLs or credentials."""


def ice_servers_from_env() -> list[dict]:
    """Only explicitly configured ICE services; no public STUN defaults."""
    def invalid(reason):
        raise IceConfigurationError(f'{ICE_ENV} 配置无效：{reason}')

    raw = os.environ.get(ICE_ENV, '[]')
    try:
        if len(raw.encode('utf-8')) > 32768:
            invalid('JSON 超过 32 KiB')
    except UnicodeError:
        invalid('JSON 必须使用 UTF-8 编码')
    try:
        servers = json.loads(raw)
    except (ValueError, TypeError):
        invalid('必须是 JSON 数组')
    if not isinstance(servers, list) or len(servers) > 8:
        invalid('必须是最多包含 8 项的数组')
    for server in servers:
        if not isinstance(server, dict) or set(server) - {'urls', 'username', 'credential'}:
            invalid('每项仅支持 urls、username 和 credential')
        urls = server.get('urls')
        urls = [urls] if isinstance(urls, str) else urls
        if not isinstance(urls, list) or not 1 <= len(urls) <= 8:
            invalid('urls 必须是地址字符串或包含 1 至 8 个地址的数组')
        has_turn = False
        for value in urls:
            if (not isinstance(value, str) or not 1 <= len(value) <= 512 or
                    not value.isascii() or any(character.isspace() or ord(character) < 32 for character in value)):
                invalid('ICE 地址必须是长度不超过 512 的 ASCII 字符串，且不含空白')
            match = re.fullmatch(r'(stun|stuns|turn|turns):([^/?#]+)(?:\?transport=(udp|tcp))?', value)
            if not match:
                invalid('仅支持 stun:、stuns:、turn:、turns: 地址及 TURN 的 transport 参数')
            scheme, authority, transport = match.groups()
            has_turn |= scheme in {'turn', 'turns'}
            if transport and scheme in {'stun', 'stuns'}:
                invalid('STUN 地址不支持 transport 参数')
            try:
                parsed = urlsplit('//' + authority)
                host = parsed.hostname
                port = parsed.port
                if (parsed.username is not None or parsed.password is not None or not host or
                        parsed.path or parsed.query or parsed.fragment or authority.endswith(':') or
                        (port is not None and not 1 <= port <= 65535)):
                    raise ValueError('authority')
                if ':' in host:
                    ipaddress.IPv6Address(host)
                    if '%' in host:
                        raise ValueError('scoped address')
                else:
                    if len(host) > 253 or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                                              for label in host.rstrip('.').split('.')):
                        raise ValueError('hostname')
            except ValueError:
                invalid('ICE 地址必须包含有效主机名或 IP，以及有效端口')
        for field in ('username', 'credential'):
            if field in server and (not isinstance(server[field], str) or
                                    not 1 <= len(server[field]) <= 512 or
                                    any(ord(character) < 32 or ord(character) == 127 for character in server[field])):
                invalid(f'{field} 必须是长度为 1 至 512 的字符串，且不含控制字符')
        if has_turn and (not server.get('username') or not server.get('credential')):
            invalid('TURN 服务需要 username 和 credential')
    return servers


def bencode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode('ascii') + b'e'
    if isinstance(value, str):
        value = value.encode('utf-8')
    if isinstance(value, bytes):
        return str(len(value)).encode('ascii') + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(bencode(item) for item in value) + b'e'
    if isinstance(value, dict):
        return b'd' + b''.join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b'e'
    raise TypeError('Unsupported torrent metadata value')


def _revision(st) -> str:
    return hashlib.sha256(f'{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}'.encode()).hexdigest()


def _regular_stat(path: Path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise ValueError('Only regular files can be distributed')
    return st


def revision(path: Path) -> str:
    return _revision(_regular_stat(path))


def installation_secret(path: Path) -> bytes:
    """Publish a complete secret atomically, including concurrent first workers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_name('.webtorrent-secret-' + secrets.token_hex(12))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_hex(32))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r', encoding='ascii') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != 64:
            raise ValueError('Invalid private tracker configuration')
        value = bytes.fromhex(stream.read(65))
    if len(value) != 32:
        raise ValueError('Invalid private tracker configuration')
    return value


def piece_length(size: int) -> int:
    """Keep historical <=1 GiB swarms stable and metadata bounded at any size."""
    length = PIECE_LENGTH
    while (size + length - 1) // length > MAX_PIECES:
        length *= 2
    return length


class TorrentDistribution:
    def __init__(self, data_root: Path, *, secret_file: Path | None = None, peer_transport: bool = True):
        self.ice_servers = []
        self.cache_root = data_root / 'metadata' / 'webtorrent'
        self.secret_file = secret_file or Path(os.getenv('RETROONE_TORRENT_SECRET_FILE', str(data_root / 'metadata' / '.webtorrent-secret')))
        self.secret = None
        self.lock = threading.Lock()
        self.jobs = {}
        self.closed = threading.Event()
        if peer_transport:
            self.enable_peers()

    def enable_peers(self):
        """HTTP piece verification does not require ICE or a tracker secret."""
        with self.lock:
            if self.secret is None:
                servers = ice_servers_from_env()
                secret = installation_secret(self.secret_file)
                self.ice_servers, self.secret = servers, secret

    def close(self):
        # Workers check between bounded reads; no long wait during API shutdown.
        self.closed.set()

    def _source(self, path, name, expected_revision=None):
        source_stat = _regular_stat(path)
        current = _revision(source_stat)
        if expected_revision and current != expected_revision:
            raise ValueError('游戏文件已变化，请刷新后重新下载')
        size = source_stat.st_size
        if size <= 0:
            raise ValueError('Empty files cannot be distributed')
        if not isinstance(name, str) or not 0 < len(name) <= 255 or name in {'.', '..'} or any(
                character in '/\\:' or ord(character) < 32 for character in name):
            raise ValueError('Invalid distribution filename')
        key = hashlib.sha256((str(path) + ':' + current).encode()).hexdigest()
        return current, size, piece_length(size), self.cache_root / (key + '.json')

    def _cached(self, cache, current, size, length):
        try:
            fd = os.open(cache, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'r', encoding='ascii') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
                    return None
                record = json.loads(stream.read(MAX_CACHE_BYTES + 1))
            candidate = base64.b64decode(record['pieces'], validate=True)
            if (record['revision'] == current and record.get('piece_length', PIECE_LENGTH) == length and
                    len(candidate) == ((size + length - 1) // length) * 20):
                return candidate
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    @staticmethod
    def _info_result(path, name, current, size, length, pieces):
        if revision(path) != current:
            raise ValueError('游戏文件已变化，请刷新后重新下载')
        info = {'length': size, 'name': name, 'piece length': length, 'pieces': pieces, 'private': 1}
        return info, hashlib.sha1(bencode(info)).hexdigest(), current

    def _compute(self, path, name, expected_revision=None, progress=None):
        current, size, length, cache = self._source(path, name, expected_revision)
        pieces = self._cached(cache, current, size, length)
        if pieces is not None:
            return self._info_result(path, name, current, size, length, pieces)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        # The cache is shared by profiles. Only one worker hashes this revision;
        # later workers wait in the background and reuse its atomic result.
        lockfd = os.open(cache.with_suffix('.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lockfd, 'a+b') as lock:
            while True:
                if self.closed.is_set():
                    raise ValueError('分片准备已停止，请重试')
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.closed.wait(.1)
            pieces = self._cached(cache, current, size, length)
            if pieces is None:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or _revision(opened) != current:
                        raise ValueError('游戏文件已变化，请刷新后重新下载')
                    hashes = []
                    remaining = size
                    while remaining:
                        hasher = hashlib.sha1()
                        piece_remaining = min(length, remaining)
                        while piece_remaining:
                            if self.closed.is_set():
                                raise ValueError('分片准备已停止，请重试')
                            chunk = stream.read(min(READ_BLOCK_BYTES, piece_remaining))
                            if not chunk:
                                raise ValueError('游戏文件读取不完整，请刷新后重新下载')
                            hasher.update(chunk)
                            remaining -= len(chunk)
                            piece_remaining -= len(chunk)
                            if progress:
                                progress(size - remaining)
                        hashes.append(hasher.digest())
                    if _revision(os.fstat(stream.fileno())) != current:
                        raise ValueError('游戏文件已变化，请刷新后重新下载')
                    pieces = b''.join(hashes)
                if revision(path) != current:
                    raise ValueError('游戏文件已变化，请刷新后重新下载')
                temporary = cache.with_suffix('.' + secrets.token_hex(8) + '.tmp')
                try:
                    temporary.write_text(json.dumps({'revision': current, 'piece_length': length,
                                                     'pieces': base64.b64encode(pieces).decode('ascii')}), 'ascii')
                    temporary.replace(cache)
                finally:
                    temporary.unlink(missing_ok=True)
        return self._info_result(path, name, current, size, length, pieces)

    def info(self, path: Path, name: str, expected_revision: str | None = None):
        """Synchronous helper for offline callers; HTTP uses poll_manifest."""
        with _hash_slots:
            return self._compute(path, name, expected_revision)

    def _manifest(self, result, download_url, *, peers=True):
        info, info_hash, current = result
        tracker = None
        if peers:
            self.enable_peers()
            expires = str(int(time.time()) + 12 * 60 * 60)
            signature = hmac.new(self.secret, (info_hash + ':' + expires).encode('ascii'), hashlib.sha256).hexdigest()
            tracker = '/rom-tracker?token=' + expires + '.' + signature
        torrent = bencode({'created by': 'RetroOne', 'info': info})
        return {'name': info['name'], 'size': info['length'], 'revision': current,
                'info_hash': info_hash, 'torrent_base64': base64.b64encode(torrent).decode('ascii'),
                'download_url': download_url, 'tracker_path': tracker, 'p2p_available': bool(peers),
                'piece_length': info['piece length'], 'ice_servers': copy.deepcopy(self.ice_servers) if peers else [], 'private': True}

    def manifest(self, path: Path, name: str, *, download_url: str, expected_revision: str | None = None, peers: bool = True):
        return self._manifest(self.info(path, name, expected_revision), download_url, peers=peers)

    def poll_manifest(self, path: Path, name: str, *, download_url: str, expected_revision: str | None = None, peers: bool = True):
        current, size, length, cache = self._source(path, name, expected_revision)
        pieces = self._cached(cache, current, size, length)
        if pieces is not None:
            return self._manifest(self._info_result(path, name, current, size, length, pieces), download_url, peers=peers)
        if self.closed.is_set():
            raise ValueError('分片准备已停止，请重试')
        key = cache.stem
        with self.lock:
            now = time.monotonic()
            for previous in list(self.jobs):
                job = self.jobs[previous]
                if job['done'] and (now - job['updated'] > 60 or len(self.jobs) >= MAX_JOB_RECORDS or
                                    (previous == key and not job.get('error'))):
                    del self.jobs[previous]
            job = self.jobs.get(key)
            if job and job.get('error'):
                raise ValueError('无法准备游戏分片，请检查文件后稍后重试')
            if job is None and len(self.jobs) < MAX_JOB_RECORDS and _hash_slots.acquire(blocking=False):
                job = self.jobs[key] = {'bytes_hashed': 0, 'done': False, 'updated': now}
                def progress(count):
                    with self.lock:
                        job['bytes_hashed'] = count
                def run():
                    try:
                        self._compute(path, name, current, progress)
                    except (OSError, ValueError):
                        with self.lock:
                            job['error'] = True
                    finally:
                        with self.lock:
                            job['done'], job['updated'] = True, time.monotonic()
                        _hash_slots.release()
                try:
                    threading.Thread(target=run, name='retroone-rom-hash', daemon=True).start()
                except RuntimeError:
                    del self.jobs[key]
                    _hash_slots.release()
                    raise ValueError('分片任务暂时无法启动，请重试')
            return {'status': 'preparing', 'name': name, 'size': size, 'revision': current,
                    'bytes_hashed': job['bytes_hashed'] if job else 0, 'total_bytes': size,
                    'retry_after': 2}
