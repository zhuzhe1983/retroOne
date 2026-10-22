"""Startup and in-flight scan contracts for large shared family libraries."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import Mock

import pytest

os.environ.setdefault('CONFIG_ROOT', tempfile.mkdtemp(prefix='retroone-scan-test-'))
from fastapi.testclient import TestClient
from retroone.api import create_app
from retroone.catalog import Catalog


class IdleRunner:
    def capabilities(self):
        return {'available': False, 'platforms': {}}

    def status(self):
        return None

    def stop(self):
        return {'stopped': True}


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setenv('RETROONE_FETCH_ARTWORK', '0')
    monkeypatch.delenv('RETROONE_SCAN_ON_STARTUP', raising=False)
    monkeypatch.setenv('RETROONE_LIBRARY_DATA_ROOT', str(tmp_path / 'shared'))
    directory = tmp_path / 'roms/library/nes/new/FC'
    directory.mkdir(parents=True)
    for name in ('alpha', 'beta', 'gamma'):
        (directory / (name + '.nes')).write_bytes(b'synthetic cartridge')
        cover = directory / 'media' / name / 'boxFront.png'
        cover.parent.mkdir(parents=True)
        cover.write_bytes(b'local image reference fixture')
    return Catalog(tmp_path / 'roms', tmp_path / 'profile')


@pytest.mark.parametrize('populated,policy,argument,expected', [
    (False, None, None, True),
    (True, None, None, False),
    (True, '1', None, True),
    (False, '0', None, False),
    (True, 'auto', None, False),
    (True, '1', False, False),
    (True, '0', True, True),
])
def test_startup_reuses_nonempty_catalog_and_scan_policy_is_explicit(library, monkeypatch, populated, policy, argument, expected):
    if populated:
        assert library.scan()['errors'] == 0
    if policy is not None:
        monkeypatch.setenv('RETROONE_SCAN_ON_STARTUP', policy)
    options = {} if argument is None else {'auto_scan': argument}
    app = create_app(library.rom_root, library.config_root, runner=IdleRunner(), **options)
    start = Mock(return_value=True)
    monkeypatch.setattr(app.state.catalog, 'begin_scan', start)
    # A startup readiness check must not walk files or refresh audit groups.
    monkeypatch.setattr(app.state.catalog, '_sync_reviews', Mock(side_effect=AssertionError('startup walked reviews')))
    with TestClient(app):
        assert start.call_count == int(expected)


def test_explicit_scan_still_runs_with_cached_catalog(library, monkeypatch):
    library.scan()
    app = create_app(library.rom_root, library.config_root, runner=IdleRunner())
    start = Mock(return_value=True)
    monkeypatch.setattr(app.state.catalog, 'begin_scan', start)
    with TestClient(app) as client:
        start.assert_not_called()
        assert client.post('/api/scan').status_code == 202
        start.assert_called_once_with()


def raw_payload(catalog, game_id):
    with catalog.connect() as db:
        row = db.execute('SELECT title,payload FROM games WHERE id=?', (game_id,)).fetchone()
        payload = json.loads(row['payload'])
        assert row['title'] == payload['title']
        return payload


def test_committed_scan_batches_keep_details_and_pick_up_edits_during_discovery(library, monkeypatch):
    assert library.scan()['errors'] == 0
    raw = sorted(list(library.discover()), key=lambda game: game['title'])
    first, target = raw[:2]
    screenshot1 = 'https://assets.nintendo.com/image/upload/first.jpg'
    screenshot2 = 'https://assets.nintendo.com/image/upload/updated.jpg'
    for game in (first, target):
        library.game_metadata.update_overrides(game['id'], {'title': 'Curated ' + game['title'], 'description': 'Shared description'})
        library.game_metadata.import_details_many([{'id': game['id'], 'details': {'screenshots': [screenshot1], 'release_date': '2000-01-01'}}])
        library.get(game['id'])
    before = raw_payload(library, target['id'])
    library.set_favorite(target['id'], True)
    library.mark_played(target['id'])
    # Discovery snapshots runtime before a family member edits shared metadata.
    raw[1]['size'] += 123
    entered, resume, second_written, finish = [threading.Event() for _ in range(4)]
    monkeypatch.setattr('retroone.catalog.SCAN_BATCH_SIZE', 1)

    def slow_discovery():
        yield copy.deepcopy(raw[0])
        entered.set()
        assert resume.wait(10)
        yield copy.deepcopy(raw[1])
        second_written.set()
        assert finish.wait(10)
        yield copy.deepcopy(raw[2])

    monkeypatch.setattr(library, 'discover', slow_discovery)
    with ThreadPoolExecutor(max_workers=2) as pool:
        scan = pool.submit(library.scan)
        try:
            assert entered.wait(10)
            # Inspect committed storage before get() can repair it: no empty
            # screenshots/title window is allowed while the scan is unfinished.
            visible = raw_payload(library, first['id'])
            assert visible['title'] == 'Curated alpha'
            assert visible['details']['screenshots'] == [screenshot1]
            assert library.scan_status['running']
            library.game_metadata.update_overrides(target['id'], {'title': 'Newest shared title'})
            library.game_metadata.import_details_many([{'id': target['id'], 'details': {'screenshots': [screenshot2]}}])
            # Must finish even while discovery is paused; no long scan write lock.
            assert pool.submit(library.get, target['id']).result(timeout=2)['title'] == 'Newest shared title'
            resume.set()
            assert second_written.wait(10)
            updated = raw_payload(library, target['id'])
            assert updated['title'] == 'Newest shared title'
            assert updated['details']['screenshots'] == [screenshot2]
            assert updated['description'] == 'Shared description'
            assert updated['media'] == before['media'] and 'cover' in updated['media']
            assert updated['cover_url'] == before['cover_url']
            assert updated['first_added_at'] == before['first_added_at']
            assert updated['_save_title'] == before['_save_title'] == 'beta'
            assert updated['size'] == before['size'] + 123
        finally:
            resume.set(); finish.set()
        assert scan.result(timeout=10)['errors'] == 0
    final = library.get(target['id'])
    assert final['favorite'] and final['play_count'] == 1
    assert final['title'] == 'Newest shared title'


def test_new_player_scan_materializes_existing_shared_facts_in_first_commit(library, monkeypatch):
    library.scan()
    game = library.list()['games'][0]
    library.game_metadata.update_overrides(game['id'], {'title': 'Existing family title'})
    library.game_metadata.import_details_many([{'id': game['id'], 'details': {'publisher': 'Existing publisher'}}])
    other = Catalog(library.rom_root, library.config_root.parent / 'other-player')
    other._sync_metadata()  # Advances its cursor before this game's local row exists.
    source = next(g for g in other.discover() if g['id'] == game['id'])
    monkeypatch.setattr('retroone.catalog.SCAN_BATCH_SIZE', 1)
    observed = []
    def interrupted():
        yield source
        observed.append(raw_payload(other, game['id']))
        raise OSError('synthetic disconnected library')
    monkeypatch.setattr(other, 'discover', interrupted)
    result = other.scan()
    assert 'disconnected' in result['last_error']
    assert observed[0]['title'] == 'Existing family title'
    assert observed[0]['details']['publisher'] == 'Existing publisher'
    assert observed[0]['first_added_at'] == game['first_added_at']
    assert observed[0]['_save_title'] == source['title']
    assert other.get(game['id'])['details']['publisher'] == 'Existing publisher'


def test_metadata_sync_cannot_write_an_old_runtime_snapshot_over_scan_batch(library, monkeypatch):
    library.scan()
    source = next(library.discover())
    other = Catalog(library.rom_root, library.config_root)
    before = raw_payload(library, source['id'])
    library.game_metadata.update_overrides(source['id'], {'title': 'Concurrent edit'})
    reading, resume, imported, written = [threading.Event() for _ in range(4)]
    apply_fields = library._apply_display_fields

    def pause_after_local_row_read(game, fields):
        if game['id'] == source['id']:
            reading.set()
            assert resume.wait(10)
        return apply_fields(game, fields)

    monkeypatch.setattr(library, '_apply_display_fields', pause_after_local_row_read)
    import_many = other.game_metadata.import_many
    def announce_import(*args, **kwargs):
        result = import_many(*args, **kwargs)
        imported.set()
        return result
    monkeypatch.setattr(other.game_metadata, 'import_many', announce_import)
    refreshed = dict(source, size=source['size'] + 77)
    def write_batch():
        other._write_scan_batch([refreshed], 'test-concurrent-scan')
        written.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        syncing = pool.submit(library._sync_metadata)
        try:
            assert reading.wait(10)
            scanning = pool.submit(write_batch)
            assert imported.wait(10)
            # Separate Catalog objects have separate Python locks. SQLite must
            # serialize the payload read/modify/write across those instances.
            assert not written.wait(0.15)
        finally:
            resume.set()
        syncing.result(timeout=10)
        scanning.result(timeout=10)
    after = raw_payload(library, source['id'])
    assert after['size'] == before['size'] + 77
    assert after['title'] == 'Concurrent edit'
    assert after['_save_title'] == before['_save_title']
