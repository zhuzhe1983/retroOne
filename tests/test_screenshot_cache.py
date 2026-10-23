"""Screenshot fetching is constrained to trusted metadata and official image hosts."""
import base64
import hashlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import socket
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from retroone.screenshot_cache import (ScreenshotCache, SafeRedirect, checked_target, image_type,
                                      MAX_IMAGE_BYTES, PublicResolver, UnsafeAddress, pinned_https)

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=')
URL = 'https://img-eshop.cdn.nintendo.net/example/screenshot.jpg'


def test_cache_reuses_bytes_without_additional_network(tmp_path, monkeypatch):
    cache = ScreenshotCache(tmp_path / 'cache')
    calls = []
    def download(url): calls.append(url); return PNG
    monkeypatch.setattr(cache, '_download', download)
    path, kind = cache.get(URL)
    assert path.read_bytes() == PNG and kind == 'image/png'
    assert cache.get(URL) == (path, kind) and calls == [URL]
    assert 'nintendo' not in path.name


@pytest.mark.parametrize('url', ['http://img-eshop.cdn.nintendo.net/a', 'https://evil.test/a',
                                'https://www.nintendo.com.evil.test/a', 'https://127.0.0.1/a',
                                'file:///etc/passwd', 'https://user:pass@www.nintendo.com/a'])
def test_unapproved_urls_never_fetch(tmp_path, monkeypatch, url):
    cache = ScreenshotCache(tmp_path / 'cache')
    monkeypatch.setattr(cache, '_download', lambda _: pytest.fail('unapproved network fetch'))
    assert cache.get(url) is None


def test_dns_private_address_and_redirect_are_rejected(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.199.5', 443))])
    with pytest.raises(ValueError): checked_target(URL)
    with pytest.raises(ValueError): SafeRedirect().redirect_request(None, None, 302, '', {}, 'http://127.0.0.1/private')


def test_download_validation_negative_cache_and_symlink_safety(tmp_path, monkeypatch):
    cache = ScreenshotCache(tmp_path / 'cache')
    calls = []
    def fail(url): calls.append(url); raise ValueError('broken image')
    monkeypatch.setattr(cache, '_download', fail)
    assert cache.get(URL) is None and cache.get(URL) is None and calls == [URL]
    for body in (b'<svg>untrusted</svg>', b'<html>error</html>', PNG[:-12], b'\xff\xd8\xffbroken'):
        with pytest.raises(ValueError): image_type(body)
    outside = tmp_path / 'outside'; outside.mkdir()
    link = tmp_path / 'link'; link.symlink_to(outside, target_is_directory=True)
    other = ScreenshotCache(link)
    monkeypatch.setattr(other, '_download', lambda _: pytest.fail('must not follow cache symlink'))
    assert other.get(URL) is None and list(outside.iterdir()) == []


def test_screenshot_api_rewrites_to_same_origin_and_checks_game_index(tmp_path, monkeypatch):
    monkeypatch.setenv('CONFIG_ROOT', str(tmp_path / 'bootstrap'))
    monkeypatch.setenv('ROM_ROOT', str(tmp_path / 'roms'))
    monkeypatch.setenv('AUTO_SCAN', '0')
    from retroone.api import create_app
    collection = tmp_path / 'roms/library/nes/test/Synthetic'
    collection.mkdir(parents=True)
    (collection / 'sample.nes').write_bytes(b'synthetic')
    monkeypatch.setenv('RETROONE_LIBRARY_DATA_ROOT', str(tmp_path / 'shared'))
    app = create_app(tmp_path / 'roms', tmp_path / 'profile', auto_scan=False,
                     profile_id='player2', base_path='/p/player2')
    app.state.catalog.scan()
    game_id = app.state.catalog.list()['games'][0]['id']
    app.state.catalog.game_metadata.import_details_many([{'id': game_id, 'details': {'screenshots': [URL]}}])
    monkeypatch.setattr(app.state.screenshot_cache, '_download', lambda _: PNG)
    with TestClient(app) as client:
        game = client.get('/api/games/' + game_id).json()
        route = f'/api/games/{game_id}/screenshots/0'
        version = hashlib.sha256(URL.encode()).hexdigest()[:16]
        assert game['details']['screenshots'] == ['/p/player2' + route + '?v=' + version]
        assert client.get(route + '?v=' + version).status_code == 200
        assert client.get(route + '?v=' + '0' * 16).status_code == 404
        image = client.get(route)
        assert image.status_code == 200 and image.content == PNG and image.headers['content-type'] == 'image/png'
        assert image.headers['cross-origin-resource-policy'] == 'same-origin'
        assert client.get(f'/api/games/{game_id}/screenshots/-1').status_code == 404
        assert client.get(f'/api/games/{game_id}/screenshots/1').status_code == 404
        assert client.get('/api/games/unknown/screenshots/0').status_code == 404
        # Client URLs are not accepted as proxy targets.
        assert client.get(route + '?url=http://127.0.0.1').content == PNG


def test_transient_network_failure_can_be_retried_immediately(tmp_path, monkeypatch):
    cache = ScreenshotCache(tmp_path / 'cache')
    attempts = []
    def download(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise TimeoutError('temporary upstream delay')
        return PNG
    monkeypatch.setattr(cache, '_download', download)
    assert cache.get(URL) is None
    assert cache.get(URL)[0].read_bytes() == PNG
    assert len(attempts) == 2


def test_cache_budget_evicts_only_generated_images(tmp_path, monkeypatch):
    cache = ScreenshotCache(tmp_path / 'cache')
    cache.cache_limit = len(PNG) * 2
    monkeypatch.setattr(cache, '_download', lambda _: PNG)
    first = cache.get(URL)[0]
    unrelated = cache.root / 'family-notes.txt'; unrelated.write_text('keep me')
    second = cache.get(URL + '?second=1')[0]
    # Accessing the first image makes it the recent one to retain.
    assert cache.get(URL)[0] == first
    third = cache.get(URL + '?third=1')[0]
    assert first.exists() and third.exists() and not second.exists()
    assert unrelated.read_text() == 'keep me'
    assert sum(path.stat().st_size for path in cache.root.glob('*.image')) <= cache.cache_limit


@pytest.mark.parametrize('queued_hit', [False, True])
def test_cache_recency_survives_coarse_default_timestamps_and_float_ties(tmp_path, monkeypatch, queued_hit):
    import retroone.screenshot_cache as module
    cache = ScreenshotCache(tmp_path)
    cache.cache_limit = len(PNG) * 2
    first = tmp_path / (hashlib.sha256(URL.encode()).hexdigest() + '.image')
    second = tmp_path / (hashlib.sha256((URL + '?second=1').encode()).hexdigest() + '.image')
    base = 1_700_000_000_000_000_000
    for offset, path in enumerate((first, second), start=1):
        path.write_bytes(PNG)
        os.utime(path, ns=(base + offset, base + offset))
    # These are genuinely different file times but indistinguishable floats.
    assert first.stat().st_mtime == second.stat().st_mtime
    assert first.stat().st_mtime_ns < second.stat().st_mtime_ns
    actual_utime = os.utime
    def coarse_default_utime(path, times=None, **kwargs):
        # Linux's implicit current inode time can repeat within one clock tick.
        # Explicit nanosecond timestamps must retain their requested precision.
        if times is None and 'ns' not in kwargs:
            kwargs['ns'] = (base + 2, base + 2)
        return actual_utime(path, times, **kwargs)
    monkeypatch.setattr(module.os, 'utime', coarse_default_utime)
    ticks = iter((base + 3, base + 4))
    monkeypatch.setattr(module.time, 'time_ns', lambda: next(ticks))
    if queued_hit:
        # Another request populated the cache between the fast lookup and the
        # per-image lock. This second hit must refresh recency as well.
        lookup, attempts = cache._cached, []
        def after_queue(path):
            attempts.append(path)
            return None if len(attempts) == 1 else lookup(path)
        monkeypatch.setattr(cache, '_cached', after_queue)
    calls = []
    def download(url): calls.append(url); return PNG
    monkeypatch.setattr(cache, '_download', download)
    assert cache.get(URL)[0] == first
    third = cache.get(URL + '?third=1')[0]
    assert first.exists() and third.exists() and not second.exists()
    assert first.stat().st_mtime_ns == base + 3
    assert third.stat().st_mtime_ns == base + 4
    assert calls == [URL + '?third=1']


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class Connection:
    def __init__(self, response):
        self.response, self.requests, self.closed = response, [], False
        self.sock = self

    def settimeout(self, timeout):
        assert 0 < timeout <= 20

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def test_public_doh_bypasses_fake_system_dns_but_pins_public_tls_target(tmp_path, monkeypatch):
    import retroone.screenshot_cache as module
    monkeypatch.setenv('RETROONE_SCREENSHOT_DNS', 'cloudflare')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('DoH must not use fake system DNS'))
    connections, calls = [], []
    answer = {'Status': 0, 'Question': [{'name': 'assets.nintendo.com.', 'type': 1}],
              'Answer': [{'name': 'assets.nintendo.com.', 'type': 1, 'TTL': 60, 'data': '23.51.137.201'}]}
    def connect(host, addresses, context, timeout):
        calls.append((host, tuple(addresses)))
        response = Response(json.dumps(answer).encode()) if host == 'cloudflare-dns.com' else Response(PNG)
        connection = Connection(response); connections.append(connection); return connection
    monkeypatch.setattr(module, 'pinned_https', connect)
    cache = ScreenshotCache(tmp_path)
    assert cache._download('https://assets.nintendo.com/example.png') == PNG
    assert calls == [('cloudflare-dns.com', ('1.1.1.1', '1.0.0.1')), ('assets.nintendo.com', ('23.51.137.201',))]
    assert all(c.closed for c in connections)
    assert cache.resolver.addresses('assets.nintendo.com') == ('23.51.137.201',)
    assert len(calls) == 2  # Bounded TTL cache avoids repeated public resolver requests.


@pytest.mark.parametrize('address', ['127.0.0.1', '192.168.199.5', '198.18.2.12', '169.254.169.254', '::1'])
def test_public_resolver_still_rejects_nonpublic_dns_answers(monkeypatch, address):
    import retroone.screenshot_cache as module
    answer = {'Status': 0, 'Question': [{'name': 'assets.nintendo.com', 'type': 1}],
              'Answer': [{'type': 1, 'TTL': 60, 'data': address}]}
    connection = Connection(Response(json.dumps(answer).encode()))
    monkeypatch.setattr(module, 'pinned_https', lambda *a, **kw: connection)
    with pytest.raises(UnsafeAddress):
        PublicResolver(None, 'cloudflare').addresses('assets.nintendo.com')
    assert connection.closed


def test_pinned_tls_keeps_original_hostname_and_cannot_dial_private(monkeypatch):
    import retroone.screenshot_cache as module
    calls = []
    class Socket:
        def settimeout(self, timeout): pass
        def close(self): pass
    class Context:
        check_hostname = True
        verify_mode = 2
        def wrap_socket(self, sock, *, server_hostname):
            calls.append(('tls', server_hostname)); return sock
    monkeypatch.setattr(socket, 'create_connection', lambda address, **kw: calls.append(('connect', address)) or Socket())
    # HTTPSConnection only stores the passed context when it is supplied.
    connection = pinned_https('assets.nintendo.com', ['23.51.137.201'], Context(), 1)
    assert calls == [('connect', ('23.51.137.201', 443)), ('tls', 'assets.nintendo.com')]
    connection.close()
    with pytest.raises(UnsafeAddress): pinned_https('assets.nintendo.com', ['127.0.0.1'], Context(), 1)
    assert len(calls) == 2


def test_redirect_cannot_escape_official_hosts_under_doh(tmp_path, monkeypatch):
    import retroone.screenshot_cache as module
    cache = ScreenshotCache(tmp_path)
    monkeypatch.setattr(cache.resolver, 'addresses', lambda host: ('23.51.137.201',))
    calls = []
    def connect(*args):
        calls.append(args)
        return Connection(Response(b'', 302, {'Location': 'https://127.0.0.1/private'}))
    monkeypatch.setattr(module, 'pinned_https', connect)
    assert cache.get(URL) is None and len(calls) == 1


def test_system_mode_remains_default_and_invalid_config_fails(tmp_path, monkeypatch):
    monkeypatch.delenv('RETROONE_SCREENSHOT_DNS', raising=False)
    assert ScreenshotCache(tmp_path).resolver.mode == 'system'
    monkeypatch.setenv('RETROONE_SCREENSHOT_DNS', 'http://192.168.199.1')
    with pytest.raises(ValueError): ScreenshotCache(tmp_path)


def test_dns_policy_failures_and_old_misses_do_not_hide_recovered_images(tmp_path, monkeypatch):
    cache = ScreenshotCache(tmp_path)
    key = hashlib.sha256(URL.encode()).hexdigest()
    (tmp_path / (key + '.miss')).touch()  # Marker from the former resolver policy.
    attempts = []
    def download(url):
        attempts.append(url)
        if len(attempts) == 1: raise UnsafeAddress('temporary fake DNS answer')
        return PNG
    monkeypatch.setattr(cache, '_download', download)
    assert cache.get(URL) is None
    assert cache.get(URL)[0].read_bytes() == PNG and len(attempts) == 2


def test_six_cold_gallery_images_wait_for_four_slots_even_with_old_stripe_collision(tmp_path, monkeypatch):
    cache = ScreenshotCache(tmp_path)
    # These six unrelated URLs all collided in the former striped lock table.
    urls = []
    for index in range(10000):
        candidate = URL + '?gallery=' + str(index)
        if int(hashlib.sha256(candidate.encode()).hexdigest()[:2], 16) % 32 == 0:
            urls.append(candidate)
            if len(urls) == 6: break
    active, maximum, calls = 0, 0, []
    guard, four_active, release = threading.Lock(), threading.Event(), threading.Event()
    def download(url):
        nonlocal active, maximum
        with guard:
            calls.append(url); active += 1; maximum = max(maximum, active)
            if active == 4: four_active.set()
        try:
            assert release.wait(5)
            return PNG
        finally:
            with guard: active -= 1
    monkeypatch.setattr(cache, '_download', download)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(cache.get, url) for url in urls]
        try:
            assert four_active.wait(2), 'unrelated images must not share the same request lock'
            # Survive longer than the previous one-second slot/lock deadline.
            assert not threading.Event().wait(1.1)
            assert not any(future.done() for future in futures)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert all(result and result[0].read_bytes() == PNG for result in results)
    assert len(calls) == 6 and maximum == 4
    assert len({result[0] for result in results}) == 6
    assert not cache.inflight and not list(tmp_path.glob('*.miss'))


def test_matching_cold_requests_share_download_after_waiting_longer_than_one_second(tmp_path, monkeypatch):
    cache = ScreenshotCache(tmp_path)
    started, release, calls = threading.Event(), threading.Event(), []
    def download(url):
        calls.append(url); started.set()
        assert release.wait(5)
        return PNG
    monkeypatch.setattr(cache, '_download', download)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(cache.get, URL) for _ in range(6)]
        try:
            assert started.wait(2)
            threading.Event().wait(1.1)
            assert not any(future.done() for future in futures)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]
    assert calls == [URL]
    assert all(result == results[0] and result is not None for result in results)
    assert not cache.inflight


@pytest.mark.parametrize('resource,capacity', [('slots', 4), ('pending', 32)])
def test_queue_deadline_is_bounded_and_does_not_create_a_missing_image_marker(tmp_path, monkeypatch, resource, capacity):
    cache = ScreenshotCache(tmp_path)
    cache.queue_timeout = .04
    semaphore = getattr(cache, resource)
    for _ in range(capacity): assert semaphore.acquire(blocking=False)
    monkeypatch.setattr(cache, '_download', lambda _: PNG)
    start = time.monotonic()
    try:
        assert cache.get(URL) is None
        assert time.monotonic() - start < .5
        assert not cache.inflight and not list(tmp_path.glob('*.miss'))
    finally:
        for _ in range(capacity): semaphore.release()
    assert cache.get(URL)[0].read_bytes() == PNG
