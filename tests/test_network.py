"""Room state and session safety; these tests do not assert emulator compatibility."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('CONFIG_ROOT', tempfile.mkdtemp(prefix='retroone-network-import-'))
from retroone.api import create_app
from retroone.network import NetworkError, NetworkStore, RIDGE_RACER_2, WorkerGateway


class RecordingRunner:
    def __init__(self):
        self.active = None
        self.started = []
        self.stopped = 0

    def capabilities(self):
        return {'platforms': {'psp': {'available': True}}}

    def status(self):
        return self.active

    def start(self, game):
        assert self.active is None
        self.started.append(game)
        self.active = {'session_id': str(uuid4()), 'game_id': game['id'], 'network': game.get('network')}
        return self.active

    def stop(self):
        self.stopped += bool(self.active)
        self.active = None
        return True


@pytest.fixture
def environment(tmp_path):
    roms = tmp_path / 'roms'
    collection = roms / 'library/psp/new/PSP'
    collection.mkdir(parents=True)
    (collection / 'ridge.iso').write_bytes(b'test fixture, not a playable game')
    runner = RecordingRunner()
    app = create_app(roms, tmp_path / 'config', runner=runner, auto_scan=False)
    app.state.catalog.scan()
    game = app.state.catalog.list()['games'][0]
    game['id'] = RIDGE_RACER_2
    with app.state.catalog.connect() as db:
        db.execute('UPDATE games SET id=?,payload=?', (RIDGE_RACER_2, json.dumps(game)))
    app.state.worker_gateway.session = lambda profile: None
    with TestClient(app) as client:
        client.post('/api/presence', json={'profile_id': 'player2', 'client_id': str(uuid4()), 'activity': 'library'})
        yield app, client, runner, roms


def invite_body(**values):
    return {'from_profile_id': 'player1', 'to_profile_id': 'player2', 'game_id': RIDGE_RACER_2,
            'request_id': str(uuid4()), **values}


def accept(client):
    invitation = client.post('/api/network/invites', json=invite_body())
    assert invitation.status_code == 201, invitation.text
    response = client.post('/api/network/invites/' + invitation.json()['id'] + '/accept', json={'profile_id': 'player2'})
    assert response.status_code == 200, response.text
    return response.json()


def test_only_verified_game_and_online_ready_members_can_be_invited(environment):
    app, client, runner, _ = environment
    capabilities = client.get('/api/network/capabilities').json()
    assert capabilities['available'] and capabilities['games'][0]['game_id'] == RIDGE_RACER_2
    assert client.post('/api/network/invites', json=invite_body(game_id='other')).status_code == 409
    assert client.post('/api/network/invites', json=invite_body(to_profile_id='player999')).status_code == 409
    assert client.post('/api/network/invites', json=invite_body(to_profile_id='player1')).status_code == 422
    with app.state.presence.connection() as db:
        db.execute('DELETE FROM clients')
    response = client.post('/api/network/invites', json=invite_body())
    assert response.json()['detail']['code'] == 'profile_offline'
    assert runner.started == [] and runner.stopped == 0


def test_invite_retry_accept_and_explicit_launch_close_do_not_implicitly_stop(environment):
    app, client, runner, _ = environment
    body = invite_body()
    first = client.post('/api/network/invites', json=body).json()
    assert client.post('/api/network/invites', json=body).json()['id'] == first['id']
    assert client.post('/api/network/invites/' + first['id'] + '/accept', json={'profile_id': 'player1'}).status_code == 403
    accepted = client.post('/api/network/invites/' + first['id'] + '/accept', json={'profile_id': 'player2'}).json()
    room = accepted['room']
    assert room['status'] == 'ready' and len(room['members']) == 2
    assert runner.started == []
    duplicate = client.post('/api/network/invites/' + first['id'] + '/accept', json={'profile_id': 'player2'}).json()
    assert duplicate['room']['id'] == room['id']
    route = '/api/games/' + RIDGE_RACER_2 + '/launch'
    started = client.post(route, json={'mode': 'stream', 'network_room_id': room['id']})
    assert started.status_code == 200, started.text
    assert runner.started[0]['network'] == {'room_id': room['id'], 'host_profile_id': 'player1', 'role': 'host', 'nickname': '玩家1'}
    assert started.json()['session']['id'] == runner.active['session_id']
    assert client.post(route, json={'mode': 'stream', 'network_room_id': room['id']}).status_code == 200
    assert len(runner.started) == 1
    assert client.post('/api/network/rooms/' + room['id'] + '/close', json={'profile_id': 'player9'}).status_code == 403
    closed = client.post('/api/network/rooms/' + room['id'] + '/close', json={'profile_id': 'player2'})
    assert closed.json()['status'] == 'closed' and runner.stopped == 0 and runner.active
    listed = client.get('/api/network/invites?profile_id=player1').json()['invites'][0]
    assert listed['status'] == 'cancelled' and listed['room_status'] == 'closed'
    assert client.post(route, json={'mode': 'stream', 'network_room_id': room['id']}).json()['detail']['code'] == 'room_not_ready'


def test_busy_accept_and_network_launch_never_replace_any_session(environment):
    app, client, runner, _ = environment
    invitation = client.post('/api/network/invites', json=invite_body()).json()
    runner.active = {'session_id': 'started-meanwhile', 'game_id': 'other'}
    response = client.post('/api/network/invites/' + invitation['id'] + '/accept', json={'profile_id': 'player2'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'profile_busy'
    assert runner.stopped == 0
    runner.active = None
    room = client.post('/api/network/invites/' + invitation['id'] + '/accept', json={'profile_id': 'player2'}).json()['room']
    runner.active = {'session_id': 'solo-even-same-game', 'game_id': RIDGE_RACER_2, 'network': None}
    route = '/api/games/' + RIDGE_RACER_2 + '/launch'
    conflict = client.post(route, json={'mode': 'stream', 'network_room_id': room['id']})
    assert conflict.status_code == 409 and conflict.json()['detail']['code'] == 'session_conflict'
    assert conflict.json()['detail']['session']['id'] == 'solo-even-same-game'
    assert client.post(route, json={'mode': 'stream', 'network_room_id': room['id'], 'replace_session_id': 'solo-even-same-game'}).status_code == 422
    assert runner.stopped == 0
    assert client.post('/api/session/stop', json={'expected_session_id': 'stale-id'}).status_code == 409
    assert runner.stopped == 0
    assert client.post('/api/session/stop', json={'expected_session_id': 'solo-even-same-game'}).status_code == 200
    assert runner.stopped == 1
    assert client.post('/api/session/stop', json={'expected_session_id': 'solo-even-same-game'}).status_code == 200
    assert runner.stopped == 1
    assert client.post(route, json={'mode': 'stream', 'network_room_id': room['id']}).status_code == 200
    assert client.post('/api/session/stop').status_code == 200  # Old scripts stay compatible.


def test_guest_fetches_only_coordinator_room_and_cannot_supply_network_parameters(environment, tmp_path):
    app, client, _, roms = environment
    room = accept(client)['room']
    guest = RecordingRunner()
    worker = create_app(roms, tmp_path / 'guest', runner=guest, auto_scan=False, profile_id='player2', base_path='/p/player2')
    worker.state.catalog.scan()
    game = worker.state.catalog.list()['games'][0]; game['id'] = RIDGE_RACER_2
    with worker.state.catalog.connect() as db:
        db.execute('UPDATE games SET id=?,payload=?', (RIDGE_RACER_2, json.dumps(game)))
    queried = []
    def fetch(identifier):
        queried.append(identifier)
        return client.get('/api/network/rooms/' + identifier).json()
    worker.state.worker_gateway.room = fetch
    route = '/api/games/' + RIDGE_RACER_2 + '/launch'
    with TestClient(worker) as guest_client:
        malicious = {'mode': 'stream', 'network_room_id': room['id'], 'network': {'host_ip': '10.0.0.1'}}
        assert guest_client.post(route, json=malicious).status_code == 422
        response = guest_client.post(route, json={'mode': 'stream', 'network_room_id': room['id']})
        assert response.status_code == 200, response.text
        assert queried == [room['id']]
        assert guest.started[0]['network']['role'] == 'guest'
        assert guest.started[0]['network']['host_profile_id'] == 'player1'
        assert response.json()['play_url'] == '/p/player2/stream/'


def test_store_serializes_competing_invites_and_request_ids(tmp_path):
    store = NetworkStore(tmp_path)
    nonce = str(uuid4())
    with ThreadPoolExecutor(max_workers=12) as pool:
        records = list(pool.map(lambda _: NetworkStore(tmp_path).create('player1', 'player2', RIDGE_RACER_2, nonce), range(24)))
    assert len({row['id'] for row in records}) == 1
    with pytest.raises(NetworkError, match='另一份|另一'):
        store.create('player1', 'player3', RIDGE_RACER_2, nonce)
    def compete(target):
        try:
            store.create('player3', target, RIDGE_RACER_2, str(uuid4()))
            return True
        except NetworkError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(compete, ['player4', 'player5'])) == 1


def test_store_expiration_decline_and_close_release_room_reservations(tmp_path, monkeypatch):
    store = NetworkStore(tmp_path)
    monkeypatch.setattr('retroone.network.time.time', lambda: 10000)
    first = store.create('player1', 'player2', RIDGE_RACER_2, str(uuid4()))
    assert store.respond(first['id'], 'player2', 'decline')['status'] == 'declined'
    assert store.respond(first['id'], 'player2', 'decline')['status'] == 'declined'
    pending = store.create('player1', 'player2', RIDGE_RACER_2, str(uuid4()))
    monkeypatch.setattr('retroone.network.time.time', lambda: 10000 + store.INVITE_TTL + 1)
    assert store.invite(pending['id'])['status'] == 'expired'
    with pytest.raises(NetworkError): store.respond(pending['id'], 'player2', 'accept')
    pending = store.create('player1', 'player2', RIDGE_RACER_2, str(uuid4()))
    accepted = store.respond(pending['id'], 'player2', 'accept')
    with pytest.raises(NetworkError): store.create('player1', 'player3', RIDGE_RACER_2, str(uuid4()))
    monkeypatch.setattr('retroone.network.time.time', lambda: 10000 + store.INVITE_TTL + store.ROOM_TTL + 2)
    assert store.room(accepted['room_id'])['status'] == 'expired'
    assert store.invite(accepted['id'])['status'] == 'expired'
    assert store.create('player1', 'player3', RIDGE_RACER_2, str(uuid4()))['status'] == 'pending'


def test_gateway_paths_reject_client_controlled_addresses():
    gateway = WorkerGateway()
    captured = []
    gateway.get = lambda url: captured.append(url) or {'session': None}
    for invalid in ('../../other', 'http://example.com', 'player0', 'player1?secret=1'):
        with pytest.raises(NetworkError): gateway.session(invalid)
    with pytest.raises(NetworkError): gateway.room('../../etc/passwd')
    assert captured == []
    gateway.session('player12')
    assert captured == ['http://retroone:3000/p/player12/api/session']
