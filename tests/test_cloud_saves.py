"""Synthetic PSP directories only; no player progress or commercial assets."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from retroone.cloud_saves import CloudSaveStore, CloudSaveError, normalize_files, MAX_BYTES
from test_browser_experiments import environment

GAME = {'id': 'a' * 24, 'platform': 'psp'}


def files(value=b'generated game progress'):
    return [{'path': 'TEST00001SAVE00/DATA.BIN', 'data': base64.b64encode(value).decode()}]


def native(root, game=GAME):
    return root / 'saves' / game['id'] / 'PPSSPP/PSP/SAVEDATA'


def write_native(root, value, game=GAME):
    path = native(root, game) / 'TEST00001SAVE00/DATA.BIN'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def test_roundtrip_native_to_browser_to_native_with_backups(tmp_path):
    write_native(tmp_path, b'cloud progress')
    store = CloudSaveStore(tmp_path)
    lease = store.acquire(GAME, uuid4())
    assert lease['files'] == files(b'cloud progress')
    result = store.upload(GAME, lease['lease_id'], lease['revision'], files(b'browser progress'))
    assert result['revision'] == 1
    assert (native(tmp_path) / files()[0]['path']).read_bytes() == b'browser progress'
    with sqlite3.connect(store.db) as db:
        assert db.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 2
    # Retrying an upload whose successful response was lost is idempotent.
    assert store.upload(GAME, lease['lease_id'], 0, files(b'browser progress'))['revision'] == 1
    store.release(GAME, lease['lease_id'])
    store.assert_native_available(GAME)
    write_native(tmp_path, b'cloud played again')
    assert store.acquire(GAME, uuid4())['files'] == files(b'cloud played again')


def test_profile_and_game_isolation(tmp_path):
    one, two = tmp_path / 'one', tmp_path / 'two'
    one.mkdir(); two.mkdir()
    stores = [CloudSaveStore(one), CloudSaveStore(two)]
    other = dict(GAME, id='b' * 24)
    lease = stores[0].acquire(GAME, uuid4())
    stores[0].upload(GAME, lease['lease_id'], 0, files())
    assert stores[0].acquire(other, uuid4())['files'] == []
    assert stores[1].acquire(GAME, uuid4())['files'] == []


def test_lease_expiry_heartbeat_and_native_protection(tmp_path):
    now = [100.0]
    store = CloudSaveStore(tmp_path, clock=lambda: now[0])
    client = uuid4(); lease = store.acquire(GAME, client)
    assert store.acquire(GAME, client)['lease_id'] == lease['lease_id']
    with pytest.raises(CloudSaveError, match='another browser'): store.acquire(GAME, uuid4())
    with pytest.raises(CloudSaveError, match='open in a browser'): store.assert_native_available(GAME)
    now[0] += 80
    store.heartbeat(GAME, lease['lease_id'])
    now[0] += 80
    with pytest.raises(CloudSaveError): store.assert_native_available(GAME)
    now[0] += 11
    store.assert_native_available(GAME)
    with pytest.raises(CloudSaveError, match='expired'): store.upload(GAME, lease['lease_id'], 0, files())


def test_cas_conflict_preserves_native_and_local_request(tmp_path):
    path = write_native(tmp_path, b'one')
    store = CloudSaveStore(tmp_path); lease = store.acquire(GAME, uuid4())
    path.write_bytes(b'unexpected external update')
    request = files(b'local unsent')
    with pytest.raises(CloudSaveError) as caught: store.upload(GAME, lease['lease_id'], 0, request)
    assert caught.value.code == 'revision_conflict'
    assert path.read_bytes() == b'unexpected external update'
    assert request == files(b'local unsent')


@pytest.mark.parametrize('path', ['../keys/prod.keys', '/etc/passwd', 'slot/../../keys', 'slot\\file', 'slot/file.state', 'slot/file.keys', 'slot/./data', 'slot/a/b/c/d', 'file.bin'])
def test_bad_paths_rejected(path):
    with pytest.raises(CloudSaveError): normalize_files([{'path': path, 'data': 'YQ=='}])


def test_size_types_duplicates_and_conflicting_paths():
    with pytest.raises(CloudSaveError): normalize_files(files() * 2)
    with pytest.raises(CloudSaveError): normalize_files([{'path': 'slot/file', 'data': '!bad'}])
    with pytest.raises(CloudSaveError): normalize_files([{'path': 'slot/file', 'data': 'YQ==', 'command': 'ignored?'}])
    with pytest.raises(CloudSaveError): normalize_files([{'path': 'slot/file', 'data': 'YQ=='}, {'path': 'slot/file/child', 'data': 'YQ=='}])
    with pytest.raises(CloudSaveError): normalize_files(files(b'x' * (MAX_BYTES + 1)))


@pytest.mark.parametrize('level', ['file', 'slot', 'SAVEDATA', 'PSP', 'saves'])
def test_symlinks_cannot_expose_private_files(tmp_path, level):
    root = tmp_path / 'profile'; root.mkdir()
    path = write_native(root, b'original')
    outside = tmp_path / 'private'; outside.mkdir(); (outside / 'DATA.BIN').write_bytes(b'private keys')
    if level == 'file':
        path.unlink(); path.symlink_to(outside / 'DATA.BIN')
    else:
        target = {'slot': path.parent, 'SAVEDATA': native(root), 'PSP': native(root).parent, 'saves': root / 'saves'}[level]
        target.rename(target.with_name('old-' + target.name)); target.symlink_to(outside, target_is_directory=True)
    store = CloudSaveStore(root)
    with pytest.raises(CloudSaveError): store.acquire(GAME, uuid4())


def test_pending_materialization_recovers_after_restart(tmp_path, monkeypatch):
    write_native(tmp_path, b'old')
    store = CloudSaveStore(tmp_path); lease = store.acquire(GAME, uuid4())
    def fail(*args): raise OSError('simulated disk interruption')
    monkeypatch.setattr(store, '_apply', fail)
    with pytest.raises(OSError): store.upload(GAME, lease['lease_id'], 0, files(b'new'))
    assert (native(tmp_path) / files()[0]['path']).read_bytes() == b'old'
    replacement = CloudSaveStore(tmp_path)
    # Same lease survives process restarts; recovery cannot be bypassed by a stale client.
    replacement.heartbeat(GAME, lease['lease_id'])
    assert replacement.upload(GAME, lease['lease_id'], 0, files(b'new'))['revision'] == 1
    assert (native(tmp_path) / files()[0]['path']).read_bytes() == b'new'


def test_two_store_instances_serialize_lease_acquisition(tmp_path):
    stores = [CloudSaveStore(tmp_path), CloudSaveStore(tmp_path)]
    def acquire(store):
        try: return store.acquire(GAME, uuid4())
        except CloudSaveError: return None
    with ThreadPoolExecutor(2) as pool: result = list(pool.map(acquire, stores))
    assert sum(bool(item) for item in result) == 1


def test_api_native_active_rejects_and_does_not_stop(environment):
    build, *_ = environment
    app, client, game, path = build(platform='psp', suffix='.iso', size=1024)
    url = '/api/games/' + game['id'] + '/cloud-save'
    app.state.runner.status = lambda: {'game_id': game['id'], 'id': 'active'}
    response = client.post(url + '/lease', json={'client_id': str(uuid4())})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'native_active'
    assert app.state.runner.stops == 0
    app.state.runner.status = lambda: None
    lease = client.post(url + '/lease', json={'client_id': str(uuid4())}).json()
    response = client.put(url, json={'lease_id': lease['lease_id'], 'base_revision': lease['revision'], 'files': files()})
    assert response.status_code == 200
    app.state.runner.status = lambda: {'game_id': game['id'], 'id': 'active'}
    assert client.put(url, json={'lease_id': lease['lease_id'], 'base_revision': 1, 'files': files(b'bad')}).status_code == 409
    app.state.runner.status = lambda: None
    assert client.get(url).json()['snapshot']['revision'] == 1
    assert app.state.runner.stops == 0


def test_play_psp_only_injects_namespaced_cloud_adapter(environment):
    build, *_ = environment
    _, client, game, _ = build(platform='psp', suffix='.iso', size=1024)
    result = client.get('/play/' + game['id'] + '?experimental=true')
    assert result.status_code == 200
    assert '/cloud-save-sync.js?v=' in result.text
    assert 'RetroOne-player1-' + game['id'] + '-' in result.text
    assert '"EJS_gameName": "Synthetic game"' in result.text
    _, other_client, other, _ = build(platform='n64', size=1024)
    assert '/cloud-save-sync.js' not in other_client.get('/play/' + other['id'] + '?experimental=true').text


def test_browser_lease_blocks_new_native_launch_without_stopping_anything(environment):
    build, *_ = environment
    app, client, game, _ = build(platform='psp', suffix='.iso', size=1024)
    app.state.runner.capabilities = lambda: {'available': True, 'platforms': {'psp': {'available': True}}}
    lease = client.post('/api/games/' + game['id'] + '/cloud-save/lease', json={'client_id': str(uuid4())})
    assert lease.status_code == 200
    response = client.post('/api/games/' + game['id'] + '/launch', json={'mode': 'stream'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'browser_active'
    assert app.state.runner.stops == 0


def test_upload_rejects_untyped_token_and_unexpected_fields(environment):
    build, *_ = environment
    _, client, game, _ = build(platform='psp', suffix='.iso', size=1024)
    url = '/api/games/' + game['id'] + '/cloud-save'
    for token in (4, {}, None):
        assert client.put(url, json={'lease_id': token, 'base_revision': 0, 'files': []}).status_code == 422
    assert client.put(url, json={'lease_id': str(uuid4()), 'base_revision': 0, 'files': [], 'path': '/firmware'}).status_code == 422


def test_directory_swap_interruption_recovers_without_deleting_old_copy(tmp_path, monkeypatch):
    import os
    write_native(tmp_path, b'old complete save')
    store = CloudSaveStore(tmp_path); lease = store.acquire(GAME, uuid4())
    rename = os.rename
    def interrupted(source, destination, *args, **kwargs):
        if str(source).startswith('SAVEDATA.stage-') and destination == 'SAVEDATA':
            raise OSError('interrupted between the two renames')
        return rename(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'rename', interrupted)
    with pytest.raises(OSError): store.upload(GAME, lease['lease_id'], 0, files(b'new complete save'))
    backups = list(native(tmp_path).parent.glob('SAVEDATA.previous-*'))
    assert len(backups) == 1
    assert (backups[0] / files()[0]['path']).read_bytes() == b'old complete save'
    monkeypatch.setattr(os, 'rename', rename)
    assert CloudSaveStore(tmp_path).upload(GAME, lease['lease_id'], 0, files(b'new complete save'))['revision'] == 1
    assert (native(tmp_path) / files()[0]['path']).read_bytes() == b'new complete save'
    assert backups[0].exists()  # Recovery preserves interrupted old copies for inspection.
