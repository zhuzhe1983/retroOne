"""Artwork path regression and shared-cache publication boundaries."""
import fcntl
import json
import os
import time
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from retroone.artwork import ArtworkCache, NEGATIVE_TTL
from retroone.catalog import Catalog


def test_lowercase_boxfront_and_nested_collection_media(tmp_path):
    roms = tmp_path / 'one'
    collection = roms / 'library/nes/new/FC'
    (collection / 'nested').mkdir(parents=True)
    (collection / 'nested/Mario.nes').write_bytes(b'rom')
    cover = collection / 'media/Mario/boxfront.png'
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b'image')
    catalog = Catalog(roms, tmp_path / 'config')
    catalog.scan()
    game = catalog.get(catalog.list()['games'][0]['id'])
    assert Path(game['media']['cover']).samefile(cover)


def test_shared_cache_local_manifest_scoped_to_platform_and_rom_root(tmp_path, monkeypatch):
    root = tmp_path / 'shared'
    monkeypatch.setenv('RETROONE_ARTWORK_ROOT', str(root))
    roms = tmp_path / 'one'
    cover = roms / 'library/nes/new/FC/media/Mario/boxfront.jpg'
    cover.parent.mkdir(parents=True)
    cover.write_bytes(b'image')
    game = {'id': 'one', 'platform': 'nes', 'rom_path': '/roms/library/nes/old/Mario.zip', 'title': 'Mario'}
    caches = [ArtworkCache(tmp_path / name, enabled=False, rom_root=roms) for name in ('player1', 'player2')]
    def manifest(path, platform='nes'):
        (root / 'local-overrides.json').write_text(json.dumps({'version': 1, 'games': {'one': {'platform': platform, 'source_path': path}}}))
    try:
        manifest('/roms/library/nes/new/FC/media/Mario/boxfront.jpg')
        assert all(cache.get(game) == cover for cache in caches)
        assert all(cache.supports(game) for cache in caches)
        for invalid in ('/etc/passwd', '/roms/library/nes/../../secret.jpg', '/roms/library/snes/new/cover.jpg'):
            manifest(invalid)
            assert caches[0].get(game) is None
        manifest('/roms/library/nes/new/FC/media/Mario/boxfront.jpg', platform='snes')
        assert caches[0].get(game) is None
        outside = tmp_path / 'outside.jpg'; outside.write_bytes(b'outside')
        cover.unlink(); cover.symlink_to(outside)
        manifest('/roms/library/nes/new/FC/media/Mario/boxfront.jpg')
        assert caches[0].get(game) is None
    finally:
        for cache in caches: cache.close()


def test_busy_shared_download_does_not_duplicate_network_or_fail_request(tmp_path):
    cache = ArtworkCache(tmp_path)
    game = {'id': 'one', 'platform': 'nes', 'rom_path': '/roms/Mario.zip', 'title': 'Mario'}
    key = cache._key(game)
    try:
        with (cache.root / (key + '.lock')).open('w') as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            with patch('retroone.artwork.urlopen') as network:
                assert cache.get(game) is None
                network.assert_not_called()
        # A failed fetch remains an optional cover, not an HTTP 500.
        with patch('retroone.artwork.os.open', side_effect=PermissionError('fixture')):
            cache.jobs.clear()
            assert cache.get(game) is None
    finally:
        cache.close()


def test_preverified_cache_is_served_without_online_platform_support(tmp_path):
    cache = ArtworkCache(tmp_path, enabled=False)
    game = {'id': 'one', 'platform': 'pico', 'rom_path': '/roms/Pico.bin', 'title': 'Pico'}
    target = cache.root / (cache._key(game) + '.png')
    target.write_bytes(b'\x89PNG\r\n\x1a\nverified-offline-fixture')
    try:
        with patch('retroone.artwork.urlopen') as network:
            assert cache.supports(game)
            assert cache.get(game) == target
            network.assert_not_called()
    finally:
        cache.close()


def test_expired_negative_cover_retries_without_visiting_another_game(tmp_path):
    cache = ArtworkCache(tmp_path)
    game = {'id': 'one', 'platform': 'nes', 'rom_path': '/roms/Mario.zip', 'title': 'Mario'}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return 'https://raw.githubusercontent.com/test.png'
        def read(self, limit): return b'\x89PNG\r\n\x1a\nfixture'
    try:
        with patch('retroone.artwork.urlopen', side_effect=HTTPError('fixture', 404, 'missing', {}, None)):
            assert cache.get(game) is None
        marker = cache.root / (cache._key(game) + '.miss')
        old = time.time() - NEGATIVE_TTL - 1
        os.utime(marker, (old, old))
        with patch('retroone.artwork.urlopen', return_value=Response()) as network:
            assert cache.get(game) is not None
            assert network.call_count == 1
    finally:
        cache.close()
