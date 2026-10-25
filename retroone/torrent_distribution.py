"""Private, single-file WebTorrent manifests backed by the existing HTTP server.

No ROM is copied or published. Only piece hashes are cached; every download is
resolved again through the game's browser eligibility checks in the API.
"""
from __future__ import annotations

import base64
import copy
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
MAX_SOURCE_BYTES = 1024**3
MAX_CACHE_BYTES = 64 * 1024
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


class TorrentDistribution:
    def __init__(self, data_root: Path, *, secret_file: Path | None = None):
        self.ice_servers = ice_servers_from_env()
        self.cache_root = data_root / 'metadata' / 'webtorrent'
        self.secret_file = secret_file or Path(os.getenv('RETROONE_TORRENT_SECRET_FILE', str(data_root / 'metadata' / '.webtorrent-secret')))
        # A single installation key lets every player join the gateway's swarm.
        self.secret = installation_secret(self.secret_file)
        self.lock = threading.Lock()

    def info(self, path: Path, name: str, expected_revision: str | None = None):
        source_stat = _regular_stat(path)
        current = _revision(source_stat)
        if expected_revision and current != expected_revision:
            raise ValueError('游戏文件已变化，请刷新后重新下载')
        size = source_stat.st_size
        if not 0 < size <= MAX_SOURCE_BYTES:
            raise ValueError('Game exceeds the browser distribution limit')
        if not isinstance(name, str) or not name or name in {'.', '..'} or any(
                character in '/\\:' or ord(character) < 32 for character in name):
            raise ValueError('Invalid distribution filename')
        cache_key = hashlib.sha256((str(path) + ':' + current).encode()).hexdigest()
        cache = self.cache_root / (cache_key + '.json')
        with self.lock:
            pieces = None
            try:
                fd = os.open(cache, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, 'r', encoding='ascii') as stream:
                    cache_stat = os.fstat(stream.fileno())
                    if not stat.S_ISREG(cache_stat.st_mode) or cache_stat.st_size > MAX_CACHE_BYTES:
                        raise ValueError('Invalid piece cache')
                    record = json.loads(stream.read(MAX_CACHE_BYTES + 1))
                candidate = base64.b64decode(record['pieces'], validate=True)
                if record['revision'] == current and len(candidate) == ((size + PIECE_LENGTH - 1) // PIECE_LENGTH) * 20:
                    pieces = candidate
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if pieces is None:
                with _hash_slots:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, 'rb') as stream:
                        opened = os.fstat(stream.fileno())
                        if not stat.S_ISREG(opened.st_mode) or _revision(opened) != current:
                            raise ValueError('游戏文件已变化，请刷新后重新下载')
                        hashes = []
                        remaining = size
                        while remaining:
                            chunk = stream.read(min(PIECE_LENGTH, remaining))
                            if not chunk:
                                raise ValueError('游戏文件读取不完整，请刷新后重新下载')
                            remaining -= len(chunk)
                            hashes.append(hashlib.sha1(chunk).digest())
                        if _revision(os.fstat(stream.fileno())) != current:
                            raise ValueError('游戏文件已变化，请刷新后重新下载')
                        pieces = b''.join(hashes)
                if revision(path) != current:
                    raise ValueError('游戏文件已变化，请刷新后重新下载')
                self.cache_root.mkdir(parents=True, exist_ok=True)
                temporary = cache.with_suffix('.' + secrets.token_hex(8) + '.tmp')
                try:
                    temporary.write_text(json.dumps({'revision': current, 'pieces': base64.b64encode(pieces).decode('ascii')}), 'ascii')
                    temporary.replace(cache)
                finally:
                    temporary.unlink(missing_ok=True)
        if revision(path) != current:
            raise ValueError('游戏文件已变化，请刷新后重新下载')
        info = {'length': size, 'name': name, 'piece length': PIECE_LENGTH, 'pieces': pieces, 'private': 1}
        return info, hashlib.sha1(bencode(info)).hexdigest(), current

    def manifest(self, path: Path, name: str, *, download_url: str, expected_revision: str | None = None):
        info, info_hash, current = self.info(path, name, expected_revision)
        # Valid while the browser is playing/seeding; a reload obtains a fresh ticket.
        expires = str(int(time.time()) + 12 * 60 * 60)
        signature = hmac.new(self.secret, (info_hash + ':' + expires).encode('ascii'), hashlib.sha256).hexdigest()
        torrent = bencode({'created by': 'RetroOne', 'info': info})
        return {'name': name, 'size': info['length'], 'revision': current,
                'info_hash': info_hash, 'torrent_base64': base64.b64encode(torrent).decode('ascii'),
                'download_url': download_url, 'tracker_path': '/rom-tracker?token=' + expires + '.' + signature,
                'piece_length': PIECE_LENGTH, 'ice_servers': copy.deepcopy(self.ice_servers), 'private': True}
