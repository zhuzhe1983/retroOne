"""Concurrent browsing shares work without caching stale duplicate proofs."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

import pytest

from retroone.catalog import Catalog


@pytest.fixture
def reviewed_catalog(tmp_path, monkeypatch):
    roms = tmp_path / 'roms'
    folder = roms / 'library/nes'
    folder.mkdir(parents=True)
    for name, content in [('Original', b'synthetic same'), ('Copy', b'synthetic same'), ('Variant', b'synthetic different')]:
        (folder / (name + '.nes')).write_bytes(content)
    monkeypatch.setenv('RETROONE_LIBRARY_DATA_ROOT', str(tmp_path / 'shared'))
    catalog = Catalog(roms, tmp_path / 'config')
    catalog.scan()
    games = {g['title']: g for g in catalog.list()['games']}
    entries = {}
    for name in ('Original', 'Copy'):
        game = games[name]
        source = Path(game['rom_path'])
        info = source.stat()
        entries[game['id']] = {
            **{key: game.get(key) for key in ('platform', 'native_core', 'browser_core', 'engine_hint')},
            'sources': [{'relative_path': source.relative_to(roms).as_posix(),
                         **{key: getattr(info, 'st_' + key) for key in ('size', 'mtime_ns', 'ctime_ns', 'ino')}}],
            'fingerprint': {'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                            'scope': 'single_cartridge_payload', 'complete': True},
            'verification': {'status': 'fingerprinted'},
        }
    report = {'schema_version': 1, 'games': entries,
              'groups': [{'id': 'a' * 24, 'scope': 'single_cartridge_payload',
                          'auto_merge_safe': True, 'game_ids': list(entries)}]}
    catalog._reviews.path.write_text(json.dumps(report))
    assert catalog.list()['total'] == 2
    return catalog, games


class AnnouncedLock:
    """Signal arrivals before acquiring, so concurrency tests need no sleeps."""
    def __init__(self, lock, arrivals):
        self.lock = lock
        self.guard = threading.Lock()
        self.arrivals = arrivals
        self.count = 0
        self.ready = threading.Event()

    def __enter__(self):
        with self.guard:
            self.count += 1
            if self.count >= self.arrivals:
                self.ready.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


def test_overlapping_reads_share_validation_but_next_read_detects_changed_alias(reviewed_catalog, monkeypatch):
    catalog, games = reviewed_catalog
    entered, release = threading.Event(), threading.Event()
    original = catalog._reviews.changed_groups
    calls = []

    def validate():
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original()

    gate = AnnouncedLock(catalog._review_lock, arrivals=3)
    monkeypatch.setattr(catalog, '_review_lock', gate)
    monkeypatch.setattr(catalog._reviews, 'changed_groups', validate)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(catalog._sync_reviews)
        try:
            assert entered.wait(5)
            overlapping = [pool.submit(catalog._sync_reviews) for _ in range(2)]
            assert gate.ready.wait(5)
        finally:
            release.set()
        for future in [first, *overlapping]:
            future.result(timeout=5)
    assert len(calls) == 1
    Path(games['Copy']['rom_path']).write_bytes(b'synthetic edit')
    assert catalog.list()['total'] == 3
    assert len(calls) == 2


def test_failed_validation_is_not_reused_by_a_waiting_request(reviewed_catalog, monkeypatch):
    catalog, _ = reviewed_catalog
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = catalog._reviews.changed_groups

    def validate():
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            raise OSError('synthetic read failure')
        return original()

    gate = AnnouncedLock(catalog._review_lock, arrivals=2)
    monkeypatch.setattr(catalog, '_review_lock', gate)
    monkeypatch.setattr(catalog._reviews, 'changed_groups', validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(catalog._sync_reviews)
        try:
            assert entered.wait(5)
            waiting = pool.submit(catalog._sync_reviews)
            assert gate.ready.wait(5)
        finally:
            release.set()
        with pytest.raises(OSError, match='synthetic read failure'):
            first.result(timeout=5)
        waiting.result(timeout=5)
    assert len(calls) == 2


def test_waiting_request_loads_a_new_report_before_reusing_validation(reviewed_catalog, monkeypatch):
    catalog, _ = reviewed_catalog
    entered, release = threading.Event(), threading.Event()
    original = catalog._reviews.changed_groups

    def validate():
        entered.set()
        assert release.wait(5)
        return original()

    gate = AnnouncedLock(catalog._review_lock, arrivals=2)
    monkeypatch.setattr(catalog, '_review_lock', gate)
    monkeypatch.setattr(catalog._reviews, 'changed_groups', validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(catalog._sync_reviews)
        try:
            assert entered.wait(5)
            waiting = pool.submit(catalog._sync_reviews)
            assert gate.ready.wait(5)
            report = json.loads(catalog._reviews.path.read_text())
            report['groups'] = []
            catalog._reviews.path.write_text(json.dumps(report))
        finally:
            release.set()
        first.result(timeout=5)
        waiting.result(timeout=5)
    assert catalog._reviews.groups == {}
    with catalog.connect() as db:
        assert db.execute('SELECT count(*) FROM game_reviews WHERE group_id IS NOT NULL').fetchone()[0] == 0
    assert catalog.list()['total'] == 3


@pytest.mark.parametrize('filters', [{}, {'q': 'Copy'}, {'favorite': True}, {'recent': True},
                                    {'review_filter': 'pending'}, {'review_filter': 'observed'},
                                    {'platform': 'nes'}, {'mode': 'unavailable'}, {'q': 'absent'}])
def test_count_and_neighbor_totals_match_filtered_representatives(reviewed_catalog, filters):
    catalog, games = reviewed_catalog
    catalog.set_favorite(games['Copy']['id'], True)
    catalog.mark_played(games['Original']['id'])
    result = catalog.list(**filters, sort='popularity', direction='desc')
    assert result['total'] == len(result['games'])
    assert catalog.neighbors('not-present', **filters)['total'] == result['total']
    for index, game in enumerate(result['games']):
        page = catalog.list(**filters, sort='popularity', direction='desc', offset=index, limit=1)
        assert page['total'] == result['total']
        assert page['games'][0]['id'] == game['id']


def test_equal_proofs_in_different_runtime_modes_remain_separate(reviewed_catalog):
    catalog, games = reviewed_catalog
    with catalog.connect() as db:
        db.execute("UPDATE games SET mode='stream' WHERE id=?", (games['Copy']['id'],))
    result = catalog.list()
    assert result['total'] == len(result['games']) == 3
