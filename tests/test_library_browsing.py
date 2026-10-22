"""Persistent shared facts and isolated family browsing, measured without emulation."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('CONFIG_ROOT', tempfile.mkdtemp(prefix='retroone-library-test-'))
from retroone.api import create_app
from retroone.catalog import Catalog
from retroone.game_metadata import GameMetadataError, GameMetadataStore
from retroone.library_settings import LibrarySettings, LibrarySettingsError


@pytest.fixture
def library(tmp_path, monkeypatch):
    roms = tmp_path / 'roms'
    collection = roms / 'library/nes/new/FC'
    collection.mkdir(parents=True)
    for name in ('Alpha', 'Bravo', 'Charlie', 'Delta'):
        (collection / (name + '.nes')).write_bytes(b'synthetic, not a playable cartridge: ' + name.encode())
    monkeypatch.setenv('RETROONE_LIBRARY_DATA_ROOT', str(tmp_path / 'shared'))
    first = Catalog(roms, tmp_path / 'first')
    first.scan()
    second = Catalog(roms, tmp_path / 'second')
    second.scan()
    games = {game['title']: game for game in first.list()['games']}
    return first, second, games


def details(score, release_date):
    return {'release_date': release_date, 'publisher': 'Fixture publisher',
            'screenshots': ['https://www.nintendo.com/example/screenshot.jpg'],
            'sources': [{'name': 'Synthetic unit test facts', 'url': 'https://www.nintendo.com/example'}],
            'popularity': {'score': score, 'source': 'Synthetic unit test ranking',
                           'url': 'https://www.nintendo.com/example', 'kind': 'official_sales_rank',
                           'value': 2, 'unit': 'rank', 'as_of': '2025-12-31'}}


def test_shared_details_survive_scan_and_first_added_is_immutable(library, monkeypatch):
    first, second, games = library
    game = games['Alpha']
    timestamp = game['first_added_at']
    assert timestamp > 0 and timestamp == second.get(game['id'])['added_at']
    first.game_metadata.update_overrides(game['id'], {'title': 'Family title'})
    first.game_configs.update_overrides(game, {'notes': 'Keep runtime choices'})
    config_bytes = (first.game_configs.root / (game['id'] + '.json')).read_bytes()
    version = first.game_metadata.import_details_many([{'id': game['id'], 'details': details(80, '1999-12-01')}])
    assert first.game_metadata.import_details_many([{'id': game['id'], 'details': details(80, '1999-12-01')}]) == version
    monkeypatch.setattr('retroone.game_metadata.time.time', lambda: timestamp + 10000)
    first.scan()
    for catalog in (first, second):
        value = catalog.get(game['id'])
        assert value['title'] == 'Family title'
        assert value['details'] == details(80, '1999-12-01')
        assert value['first_added_at'] == timestamp
        assert value['_save_title'] == 'Alpha'
    assert (first.game_configs.root / (game['id'] + '.json')).read_bytes() == config_bytes
    first.game_metadata.import_details_many([{'id': game['id'], 'details': {'publisher': None}}])
    assert 'publisher' not in second.get(game['id'])['details']
    assert second.get(game['id'])['details']['release_date'] == '1999-12-01'


@pytest.mark.parametrize('sort', ['name', 'popularity', 'release_date', 'added_at', 'play_count', 'last_played'])
@pytest.mark.parametrize('direction', ['asc', 'desc'])
def test_sort_pagination_nulls_and_neighbors_agree(library, sort, direction):
    first, second, games = library
    first.game_metadata.import_details_many([
        {'id': games['Bravo']['id'], 'details': details(80, '2020-01-01')},
        {'id': games['Charlie']['id'], 'details': details(80, '2000-01-01')},
    ])
    first.mark_played(games['Charlie']['id'])
    first.mark_played(games['Bravo']['id'])
    first.mark_played(games['Bravo']['id'])
    whole = first.list(sort=sort, direction=direction)['games']
    pages = [first.list(sort=sort, direction=direction, offset=i, limit=1)['games'][0] for i in range(4)]
    assert [game['id'] for game in whole] == [game['id'] for game in pages]
    for index, game in enumerate(whole):
        neighbors = first.neighbors(game['id'], sort=sort, direction=direction)
        assert neighbors == {'previous_id': whole[index - 1]['id'] if index else None,
                             'next_id': whole[index + 1]['id'] if index < 3 else None,
                             'index': index, 'total': 4}
    if sort in ('popularity', 'release_date', 'last_played'):
        assert {game['title'] for game in whole[-2:]} == {'Alpha', 'Delta'}
    if sort == 'popularity':
        assert [game['title'] for game in whole[:2]] == ['Bravo', 'Charlie']
    assert second.get(games['Bravo']['id'])['play_count'] == 0
    assert first.get(games['Bravo']['id'])['play_count'] == 2
    assert first.platforms()[0]['play_count'] == 3
    assert second.platforms()[0]['play_count'] == 0


def test_filtered_neighbors_and_missing_metric_fallback(library):
    first, _, games = library
    assert [game['title'] for game in first.list(sort='popularity')['games']] == ['Alpha', 'Bravo', 'Charlie', 'Delta']
    first.set_favorite(games['Bravo']['id'], True)
    assert first.neighbors(games['Bravo']['id'], favorite=True) == {'previous_id': None, 'next_id': None, 'index': 0, 'total': 1}
    assert first.neighbors(games['Alpha']['id'], favorite=True) == {'previous_id': None, 'next_id': None, 'index': None, 'total': 1}
    first.mark_played(games['Alpha']['id']); first.mark_played(games['Charlie']['id'])
    assert [game['title'] for game in first.list(recent=True)['games']] == ['Charlie', 'Alpha']
    assert first.neighbors(games['Charlie']['id'], recent=True)['next_id'] == games['Alpha']['id']


def test_launch_counts_are_atomic_and_not_updated_by_reads(library):
    first, _, games = library
    game_id = games['Alpha']['id']
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: first.mark_played(game_id), range(32)))
    for _ in range(3):
        first.get(game_id); first.list(); first.neighbors(game_id); first.platforms()
    assert first.get(game_id)['play_count'] == 32
    first.scan()
    assert first.get(game_id)['play_count'] == 32


def test_v1_migration_preserves_ids_history_overrides_and_unknown_dates(library):
    first, _, games = library
    game_id = games['Alpha']['id']
    first.set_favorite(game_id, True)
    first.game_metadata.update_overrides(game_id, {'title': 'Manual title'})
    with first.game_metadata._connect(write=True) as db:
        db.execute('DROP TABLE game_details')
        db.execute('PRAGMA user_version=1')
    with first.connect() as db:
        db.execute('DROP TABLE history')
        db.execute('CREATE TABLE history (game_id TEXT PRIMARY KEY, played_at REAL NOT NULL)')
        db.execute('INSERT INTO history VALUES (?,?)', (game_id, 12345.0))
    migrated = Catalog(first.rom_root, first.config_root)
    value = migrated.get(game_id)
    assert value['title'] == 'Manual title' and value['favorite'] is True
    assert value['played_at'] == 12345.0 and value['play_count'] == 0
    assert value['first_added_at'] is None and value['added_at'] is None and value['details'] == {}
    migrated.scan()
    assert migrated.get(game_id)['first_added_at'] is None
    new_path = Path(games['Alpha']['rom_path']).with_name('New.nes'); new_path.write_bytes(b'new synthetic')
    migrated.scan()
    assert migrated.list(q='New')['games'][0]['first_added_at'] > 0
    migrated.mark_played(game_id)
    assert migrated.get(game_id)['play_count'] == 1


@pytest.mark.parametrize('bad', [
    {'release_date': '2000'}, {'release_date': '2025-02-30'}, {'players': 4},
    {'official_url': 'javascript:alert(1)'}, {'official_url': 'https://user:secret@example.com/a'},
    {'screenshots': ['https://127.0.0.1/a.jpg']}, {'screenshots': ['https://www.nintendo.com.evil.test/a.jpg']},
    {'screenshots': ['file:///etc/passwd']}, {'screenshots': ['https://nas.local/image.jpg']},
    {'popularity': {'score': 99}}, {'rom_path': '/etc/passwd'},
])
def test_invalid_details_roll_back_the_batch(library, bad):
    first, _, games = library
    before = first.game_metadata.revision()
    with pytest.raises(GameMetadataError) as error:
        first.game_metadata.import_details_many([
            {'id': games['Alpha']['id'], 'details': details(50, '2000-01-01')},
            {'id': games['Bravo']['id'], 'details': bad},
        ])
    assert error.value.status_code == 422 and first.game_metadata.revision() == before
    assert first.get(games['Alpha']['id'])['details'] == {}


class Runner:
    def __init__(self): self.active = None; self.starts = 0
    def capabilities(self): return {'platforms': {'nes': {'available': True}}}
    def status(self): return self.active
    def start(self, game):
        self.starts += 1; self.active = {'id': 'test-session', 'game_id': game['id']}; return self.active
    def stop(self): self.active = None


def test_settings_api_isolated_and_launch_resume_not_counted(library, tmp_path):
    first, second, games = library
    runner = Runner()
    app = create_app(first.rom_root, first.config_root, auto_scan=False, runner=runner)
    with TestClient(app) as client:
        response = client.get('/api/settings/library')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert response.json()['settings'] == {'pinned_platforms': [], 'sort': 'popularity', 'direction': 'desc'}
        value = {'pinned_platforms': ['switch', 'nes'], 'sort': 'popularity', 'direction': 'desc'}
        assert client.put('/api/settings/library', json=value).json()['settings'] == value
        assert LibrarySettings(first.config_root).get() == value
        assert LibrarySettings(second.config_root).get()['pinned_platforms'] == []
        for bad in ({}, dict(value, pinned_platforms=['nes', 'nes']), dict(value, pinned_platforms=['../x']),
                    dict(value, sort='random'), dict(value, profile_id='player2')):
            assert client.put('/api/settings/library', json=bad).status_code == 422
        assert client.get('/api/games?sort=popularity&direction=asc').status_code == 200
        assert client.get('/api/games?sort=sql').status_code == 422
        assert client.get('/api/games?direction=drop').status_code == 422
        game_id = games['Alpha']['id']
        assert client.get(f'/api/games/{game_id}/neighbors?sort=name').json()['index'] == 0
        assert client.get('/api/games/not-present/neighbors').status_code == 404
        launch = client.post(f'/api/games/{game_id}/launch', json={'mode': 'stream'})
        assert launch.status_code == 200, launch.text
        assert client.post(f'/api/games/{game_id}/launch', json={'mode': 'stream'}).status_code == 200
        assert client.get(f'/api/games/{game_id}').json()['play_count'] == 1
        assert runner.starts == 1
        assert client.get('/api/session').status_code == 200
        assert client.get(f'/api/games/{game_id}').json()['play_count'] == 1


def test_corrupt_or_symlinked_settings_never_overwritten(tmp_path):
    store = LibrarySettings(tmp_path / 'player')
    store.path.write_bytes(b'broken JSON')
    with pytest.raises(LibrarySettingsError): store.update({'pinned_platforms': [], 'sort': 'name', 'direction': 'asc'})
    assert store.path.read_bytes() == b'broken JSON'
    store.path.unlink()
    outside = tmp_path / 'outside'; outside.write_bytes(b'original')
    store.path.symlink_to(outside)
    with pytest.raises(LibrarySettingsError): store.get()
    assert outside.read_bytes() == b'original'


def test_mixed_v1_writer_and_details_extension_remain_compatible(library):
    first, second, games = library
    store = first.game_metadata
    game = games['Alpha']
    store.import_details_many([{'id': game['id'], 'details': details(90, '2001-01-01')}])
    # Emulate the original worker's unchanged v1 tables and exact row decoder.
    with sqlite3.connect(store.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        raw = db.execute('SELECT generated FROM game_metadata WHERE game_id=?', (game['id'],)).fetchone()[0]
        generated = json.loads(raw)
        assert set(generated) == {'title', 'description', 'sort_by', 'media'}
        generated['title'] = 'Legacy source update'
        revision = db.execute('SELECT revision FROM metadata_state').fetchone()[0] + 1
        db.execute('UPDATE game_metadata SET generated=?,revision=? WHERE game_id=?', (json.dumps(generated), revision, game['id']))
        db.execute('INSERT INTO game_metadata VALUES (?,?,?,?)', ('legacy-new', json.dumps(generated), '{}', revision))
        db.execute('UPDATE metadata_state SET revision=?', (revision,))
    # An old worker is allowed to introduce a row without extension metadata.
    snapshot = {row['id']: row for row in store.changes_since(-1)[1]}
    assert snapshot['legacy-new']['first_added_at'] is None and snapshot['legacy-new']['details'] == {}
    assert snapshot[game['id']]['details'] == details(90, '2001-01-01')
    assert second.get(game['id'])['title'] == 'Legacy source update'
    store.import_details_many([{'id': 'legacy-new', 'details': {'publisher': 'Known later'}}])
    assert store.apply({'id': 'legacy-new'})['details']['publisher'] == 'Known later'
    # Should an external legacy maintenance tool remove a shared base row, the
    # extension must not resurrect it or appear on its own. Reimporting the same
    # stable ID may restore the previously recorded facts and original timestamp.
    recorded = store.apply(game)['first_added_at']
    with sqlite3.connect(store.db_path) as db:
        db.execute('DELETE FROM game_metadata WHERE game_id=?', (game['id'],))
    assert game['id'] not in {row['id'] for row in store.changes_since(-1)[1]}
    with pytest.raises(GameMetadataError) as error:
        store.import_details_many([{'id': game['id'], 'details': {'publisher': 'must not write'}}])
    assert error.value.status_code == 404
    store.import_many([game])
    assert store.apply(game)['first_added_at'] == recorded
    assert store.apply(game)['details'] == details(90, '2001-01-01')


def test_editorial_description_stays_separate_from_package_notes(library):
    first, _, games = library
    game = games['Alpha']
    original = game['description']
    first.game_metadata.import_details_many([{'id': game['id'], 'details': {'description': '游戏简介第一段。\n第二段。'}}])
    view = first.get(game['id'])
    assert view['details']['description'] == '游戏简介第一段。\n第二段。'
    assert view['description'] == original
    for invalid in ('x' * 8193, 'unsafe\x00text'):
        with pytest.raises(GameMetadataError):
            first.game_metadata.import_details_many([{'id': game['id'], 'details': {'description': invalid}}])
    assert first.get(game['id'])['details']['description'] == view['details']['description']
