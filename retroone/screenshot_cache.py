"""Bounded same-origin cache for official game screenshots under COEP.

The caller selects an index from trusted metadata, never a user-supplied URL.
Only approved public HTTPS image hosts are fetched; redirects are revalidated.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import fcntl
import http.client
import ipaddress
import json
import os
import re
from pathlib import Path
import socket
import ssl
import stat
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler

from .game_details import public_url

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MISS_TTL = 15 * 60
DNS_PROVIDERS = {
    'cloudflare': ('cloudflare-dns.com', ('1.1.1.1', '1.0.0.1'), '/dns-query'),
    'google': ('dns.google', ('8.8.8.8', '8.8.4.4'), '/resolve'),
}


class UnsafeAddress(ValueError):
    """An address-policy failure is not proof that the image is absent."""


def public_addresses(addresses):
    result = tuple(dict.fromkeys(addresses))
    if not result or any(not ipaddress.ip_address(address).is_global for address in result):
        raise UnsafeAddress('screenshot hostname resolved to a non-public address')
    return result


def pinned_https(host, addresses, context, timeout):
    """Connect only to validated numeric IPs; authenticate the original hostname.

    Setting the already-connected TLS socket avoids a second hostname lookup
    between the SSRF check and connection. Ambient proxy variables are ignored.
    """
    addresses = public_addresses(addresses)
    deadline = time.monotonic() + timeout
    last_error = None
    for address in addresses[:4]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock = None
        try:
            sock = socket.create_connection((address, 443), timeout=remaining)
            sock.settimeout(max(.1, deadline - time.monotonic()))
            wrapped = context.wrap_socket(sock, server_hostname=host)
            connection = http.client.HTTPSConnection(host, timeout=remaining, context=context)
            connection.sock = wrapped
            return connection
        except OSError as error:
            last_error = error
            if sock is not None:
                sock.close()
    raise last_error or TimeoutError('screenshot connection deadline exceeded')


class PublicResolver:
    """Opt-in DoH with fixed public bootstrap addresses, never arbitrary endpoints."""
    def __init__(self, context, mode='system'):
        if mode not in {'system', *DNS_PROVIDERS}:
            raise ValueError('RETROONE_SCREENSHOT_DNS must be system, cloudflare or google')
        self.context, self.mode = context, mode
        self.cache = {}
        self.lock = threading.Lock()

    def addresses(self, host):
        # Even resolver queries are limited to approved image hostnames.
        public_url('https://' + host + '/', image=True)
        if self.mode == 'system':
            return public_addresses(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        with self.lock:
            cached = self.cache.get(host)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        resolver_host, bootstrap, path = DNS_PROVIDERS[self.mode]
        deadline = time.monotonic() + 6
        connection = pinned_https(resolver_host, bootstrap, self.context, 6)
        try:
            transport = connection.sock
            connection.request('GET', path + '?' + urlencode({'name': host, 'type': 'A'}),
                               headers={'Accept': 'application/dns-json'})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise OSError('public screenshot DNS request failed')
                body = bytearray()
                while len(body) <= 65536:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('public screenshot DNS deadline exceeded')
                    transport.settimeout(remaining)
                    chunk = response.read1(65537 - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
                if len(body) > 65536:
                    raise OSError('public screenshot DNS response too large')
            try:
                data = json.loads(body)
                questions = data.get('Question', [])
                if (data.get('Status') != 0 or not isinstance(questions, list) or
                        not any(q.get('name', '').rstrip('.').lower() == host.lower() and
                                q.get('type') == 1 for q in questions if isinstance(q, dict))):
                    raise ValueError('unexpected DNS question or status')
                answers = data.get('Answer', [])
                records = [r for r in answers if isinstance(r, dict) and r.get('type') == 1]
                addresses = public_addresses(r['data'] for r in records)
                ttls = [r.get('TTL', 0) for r in answers if isinstance(r, dict)]
                ttl = min([300, *(max(0, value) for value in ttls if type(value) is int)])
            except UnsafeAddress:
                raise
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise OSError('invalid public screenshot DNS response') from error
        finally:
            connection.close()
        with self.lock:
            if len(self.cache) >= 128:
                self.cache.clear()
            self.cache[host] = (time.monotonic() + ttl, addresses)
        return addresses


def _image_signature(prefix, tail, size):
    if size > 32 and prefix.startswith(b'\x89PNG\r\n\x1a\n') and tail[-12:-8] == b'\0\0\0\0' and tail[-8:-4] == b'IEND':
        return 'image/png'
    if size > 32 and prefix.startswith(b'\xff\xd8\xff') and tail.endswith(b'\xff\xd9'):
        return 'image/jpeg'
    if size > 32 and prefix[:4] == b'RIFF' and prefix[8:12] == b'WEBP' and int.from_bytes(prefix[4:8], 'little') + 8 == size:
        return 'image/webp'
    raise ValueError('unsupported or incomplete screenshot')


def image_type(data):
    return _image_signature(data[:32], data[-12:], len(data))


def checked_target(url):
    url = public_url(url, image=True)
    addresses = socket.getaddrinfo(urlsplit(url).hostname, 443, type=socket.SOCK_STREAM)
    public_addresses(item[4][0] for item in addresses)
    return url


class SafeRedirect(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ScreenshotCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.inflight = {}
        self.inflight_lock = threading.Lock()
        # A normal six/eight-image gallery must survive waiting for the first
        # four downloads (each bounded to 20s), even on a slow cold cache.
        self.pending = threading.BoundedSemaphore(32)
        self.queue_timeout = 45
        self.slots = threading.BoundedSemaphore(4)
        self.cache_limit = 512 * 1024 * 1024
        system_ca = os.getenv('SSL_CERT_FILE') or ('/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').is_file() else None)
        self.context = ssl.create_default_context(cafile=system_ca)
        self.resolver = PublicResolver(self.context, os.getenv('RETROONE_SCREENSHOT_DNS', 'system').strip().lower())

    @contextmanager
    def _queued_key(self, key, deadline):
        """Bound queued requests and coalesce only the *same* image download.

        Reference counts include waiters, so a lock is not replaced while a
        matching request is still waiting. Entries disappear after completion.
        """
        if not self.pending.acquire(timeout=max(0, deadline - time.monotonic())):
            yield False
            return
        acquired = False
        with self.inflight_lock:
            entry = self.inflight.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            acquired = entry[0].acquire(timeout=max(0, deadline - time.monotonic()))
            yield acquired
        finally:
            if acquired:
                entry[0].release()
            with self.inflight_lock:
                entry[1] -= 1
                if entry[1] == 0:
                    del self.inflight[key]
            self.pending.release()

    def _directory(self):
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise ValueError('screenshot cache cannot traverse symlinks')
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cached(path):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or not 32 < info.st_size <= MAX_IMAGE_BYTES:
                    return None
                prefix = handle.read(32)
                handle.seek(-12, os.SEEK_END)
                kind = _image_signature(prefix, handle.read(12), info.st_size)
                # Implicit inode timestamps can repeat within a kernel clock
                # tick. Keep explicit nanoseconds for both fast and queued hits;
                # use the validated descriptor rather than re-following a path.
                accessed = time.time_ns()
                os.utime(handle.fileno(), ns=(accessed, accessed))
                return path, kind
        except (OSError, ValueError):
            return None

    def _download(self, url):
        deadline = time.monotonic() + 20
        for redirects in range(4):
            url = public_url(url, image=True)
            target = urlsplit(url)
            addresses = self.resolver.addresses(target.hostname)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('screenshot download deadline exceeded')
            connection = pinned_https(target.hostname, addresses, self.context, min(8, remaining))
            try:
                transport = connection.sock
                path = (target.path or '/') + ('?' + target.query if target.query else '')
                connection.request('GET', path, headers={
                    'User-Agent': 'RetroOne/1.0 official-screenshot-cache',
                    'Accept': 'image/png,image/jpeg,image/webp'})
                with connection.getresponse() as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirects == 3 or not response.headers.get('Location'):
                            raise ValueError('too many or invalid screenshot redirects')
                        url = public_url(urljoin(url, response.headers['Location']), image=True)
                        continue
                    if response.status != 200:
                        raise HTTPError(url, response.status, 'image request did not succeed', response.headers, None)
                    length = response.headers.get('Content-Length')
                    if length and int(length) > MAX_IMAGE_BYTES:
                        raise ValueError('screenshot is too large')
                    body = bytearray()
                    while len(body) <= MAX_IMAGE_BYTES:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('screenshot download deadline exceeded')
                        transport.settimeout(min(8, remaining))
                        chunk = response.read1(min(65536, MAX_IMAGE_BYTES + 1 - len(body)))
                        if not chunk:
                            break
                        body.extend(chunk)
                    if len(body) > MAX_IMAGE_BYTES:
                        raise ValueError('screenshot is too large')
                    image_type(body)
                    return bytes(body)
            finally:
                connection.close()

    def _publish(self, key, data):
        """Keep the shared cache below 512 MiB across all player processes.

        Only digest-named cache files can be evicted. ROMs, metadata and unknown
        files are never candidates. The lock covers cleanup and publication.
        """
        lock_path = self.root / '.cache.lock'
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a+b') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise ValueError('invalid screenshot cache lock')
            deadline = time.monotonic() + 3
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('screenshot cache is busy')
                    time.sleep(.01)
            path = self.root / (key + '.image')
            images, total = [], 0
            for candidate in self.root.iterdir():
                if not re.fullmatch(r'[0-9a-f]{64}\.image', candidate.name):
                    continue
                info = candidate.lstat()
                if stat.S_ISREG(info.st_mode) and candidate != path:
                    images.append((info.st_mtime_ns, candidate, info.st_size))
                    total += info.st_size
            for _, candidate, size in sorted(images):
                if total + len(data) <= self.cache_limit:
                    break
                candidate.unlink()
                total -= size
            if total + len(data) > self.cache_limit:
                raise OSError('screenshot exceeds cache budget')
            path = self.root / (key + '.image')
            with tempfile.NamedTemporaryFile(prefix=key + '.', dir=self.root, delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    handle.write(data)
                    handle.flush()
                    published = time.time_ns()
                    os.utime(handle.fileno(), ns=(published, published))
                    os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            return path

    def get(self, url):
        """Return (path, content_type), or None after a bounded download failure."""
        try:
            public_url(url, image=True)
            self._directory()
            key = hashlib.sha256(url.encode('utf-8')).hexdigest()
            path = self.root / (key + '.image')
            cached = self._cached(path)
            if cached:
                return cached
            # A previous fake-DNS failure must not suppress a corrected policy.
            failure_key = hashlib.sha256((url + '\npublic-address-v2:' + self.resolver.mode).encode()).hexdigest()
            miss = self.root / (failure_key + '.miss')
            if miss.is_symlink():
                raise ValueError('invalid screenshot miss marker')
            if miss.is_file() and time.time() - miss.stat().st_mtime < MISS_TTL:
                return None
            deadline = time.monotonic() + self.queue_timeout
            with self._queued_key(key, deadline) as acquired:
                if not acquired:
                    return None
                try:
                    cached = self._cached(path)
                    if cached:
                        return cached
                    if miss.is_file() and time.time() - miss.stat().st_mtime < MISS_TTL:
                        return None
                    if not self.slots.acquire(timeout=max(0, deadline - time.monotonic())):
                        return None
                    try:
                        data = self._download(url)
                    finally:
                        self.slots.release()
                    path = self._publish(key, data)
                    return path, image_type(data)
                except (OSError, ValueError, HTTPError, URLError, http.client.HTTPException) as error:
                    # Cache permanent absence/bad content; transient network or
                    # queue contention must remain immediately retryable.
                    if (isinstance(error, ValueError) and not isinstance(error, UnsafeAddress) or
                            isinstance(error, HTTPError) and error.code in {404, 410}):
                        descriptor = os.open(miss, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                        os.close(descriptor)
                    return None
        except (OSError, ValueError):
            return None
