"""Personal preferences, durable storage and next-session-only application."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
from typing import get_args
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('CONFIG_ROOT', tempfile.mkdtemp(prefix='retroone-display-import-'))
from retroone.api import create_app
from retroone.settings import DISPLAY_RESOLUTIONS, DisplaySettings, DisplaySettingsError, StreamResolution, display_view


class DisplayRunner:
    def __init__(self):
        self.active = None
        self.starts = []
        self.stops = 0

    def capabilities(self):
        return {'platforms': {'nes': {'available': True}}}

    def display_capabilities(self):
        return {'available': True, 'reason': None}

    def status(self):
        return self.active

    def start(self, game):
        assert self.active is None
        self.starts.append(game)
        applied = dict(game['_display'])
        self.active = {'session_id': str(uuid4()), 'game_id': game['id'],
                       'display': {'settings': applied, **DISPLAY_RESOLUTIONS[applied['stream_resolution']]}}
        return self.active

    def stop(self):
        self.stops += bool(self.active)
        self.active = None
        return True


@pytest.fixture
def environment(tmp_path):
    roms = tmp_path / 'roms'
    collection = roms / 'library/nes/new/FC'
    collection.mkdir(parents=True)
    for name in ('first', 'second'):
        (collection / (name + '.nes')).write_bytes(b'fixture')
    vendor = tmp_path / 'vendor'
    (vendor / 'cores').mkdir(parents=True)
    (vendor / 'loader.js').write_text('fixture')
    (vendor / 'cores/fceumm-wasm.data').write_bytes(b'fixture')
    runner = DisplayRunner()
    app = create_app(roms, tmp_path / 'config', runner=runner, browser_data=vendor, auto_scan=False)
    app.state.catalog.scan()
    games = {g['title']: g['id'] for g in app.state.catalog.list()['games']}
    with TestClient(app) as client:
        yield app, client, runner, games, roms


def test_api_defaults_enums_and_legacy_session_are_honest(environment):
    app, client, runner, _, _ = environment
    original = client.get('/api/settings/display').json()
    assert original['settings'] == {'stream_resolution': '720p'}
    assert original['capabilities']['stream_resolution'] == [{'value': key, **size} for key, size in DISPLAY_RESOLUTIONS.items()]
    assert set(get_args(StreamResolution)) == set(DISPLAY_RESOLUTIONS)
    assert original['applies_on'] == 'next_launch' and original['active'] is None and not original['pending']
    for invalid in ({}, {'stream_resolution': '4k'}, {'stream_resolution': '1080P'}, {'stream_resolution': 1080},
                    {'stream_resolution': None}, {'stream_resolution': '720p', 'width': 1920},
                    {'stream_resolution': '720p', 'image_scaling': 'smooth'}, {'profile_id': 'player2', 'stream_resolution': '1080p'}):
        assert client.patch('/api/settings/display', json=invalid).status_code == 422
    assert not app.state.display_settings.path.exists()
    runner.active = {'session_id': 'old-session', 'game_id': 'old-game'}
    result = client.patch('/api/settings/display', json={'stream_resolution': '1080p'}).json()
    assert result['settings']['stream_resolution'] == '1080p'
    assert result['active'] is None and result['active_unknown'] and result['pending']
    assert runner.stops == 0 and runner.starts == []
    assert DisplaySettings(app.state.display_settings.path.parent).get() == result['settings']


def test_saved_setting_only_applies_to_new_cloud_session_not_continuation(environment):
    app, client, runner, games, _ = environment
    route = lambda key: '/api/games/' + games[key] + '/launch'
    first = client.post(route('first'), json={'mode': 'stream'}).json()['session']
    assert runner.starts[0]['_display'] == {'stream_resolution': '720p'}
    result = client.patch('/api/settings/display', json={'stream_resolution': '1080p'}).json()
    assert result['active'] == {'session_id': first['id'], 'settings': {'stream_resolution': '720p'}, 'width': 1280, 'height': 720}
    assert result['pending'] and not result['active_unknown'] and runner.stops == 0
    assert client.post(route('first'), json={'mode': 'stream'}).json()['session']['id'] == first['id']
    assert len(runner.starts) == 1
    assert client.post(route('second'), json={'mode': 'stream', '_display': {'stream_resolution': '720p'}}).status_code == 422
    switched = client.post(route('second'), json={'mode': 'stream', 'replace_session_id': first['id']})
    assert switched.status_code == 200
    assert runner.stops == 1 and runner.starts[1]['_display'] == {'stream_resolution': '1080p'}
    now = client.get('/api/settings/display').json()
    assert now['active']['width'] == 1920 and now['active']['height'] == 1080 and not now['pending']


def test_preferences_do_not_leak_to_other_worker_or_browser(environment, tmp_path):
    app, client, runner, games, roms = environment
    client.patch('/api/settings/display', json={'stream_resolution': '1080p'})
    other_runner = DisplayRunner()
    other = create_app(roms, tmp_path / 'player2', runner=other_runner, auto_scan=False, profile_id='player2', base_path='/p/player2')
    with TestClient(other) as second:
        response = second.get('/api/settings/display').json()
        assert response['settings'] == {'stream_resolution': '720p'} and response['profile_id'] == 'player2'
        second.patch('/api/settings/display', json={'stream_resolution': '720p'})
        assert client.get('/api/settings/display').json()['settings']['stream_resolution'] == '1080p'
    browser = client.post('/api/games/' + games['first'] + '/launch', json={'mode': 'browser'})
    assert browser.status_code == 200 and browser.json()['mode'] == 'browser'
    assert runner.starts == [] and runner.stops == 0


def test_corrupt_settings_cannot_silently_reset_or_stop_a_confirmed_session(environment):
    app, client, runner, games, _ = environment
    first = client.post('/api/games/' + games['first'] + '/launch', json={'mode': 'stream'}).json()['session']
    path = app.state.display_settings.path
    path.write_text('{corrupt document')
    assert client.get('/api/settings/display').status_code == 503
    assert client.patch('/api/settings/display', json={'stream_resolution': '1080p'}).status_code == 503
    assert path.read_text() == '{corrupt document'
    continued = client.post('/api/games/' + games['first'] + '/launch', json={'mode': 'stream'})
    assert continued.status_code == 200  # Existing session does not read new settings.
    switch = client.post('/api/games/' + games['second'] + '/launch', json={'mode': 'stream', 'replace_session_id': first['id']})
    assert switch.status_code == 503 and runner.stops == 0 and len(runner.starts) == 1


def test_invalid_or_inaccurate_runner_snapshot_never_appears_as_active_resolution():
    for snapshot in (None, {'settings': {'stream_resolution': '1080p'}, 'width': 1280, 'height': 720},
                     {'settings': {'stream_resolution': '4k'}, 'width': 3840, 'height': 2160}):
        result = display_view({'stream_resolution': '720p'}, {'session_id': 'active', 'display': snapshot}, {'available': False, 'reason': 'not installed'})
        assert result['active'] is None and result['active_unknown'] and result['pending']
        assert not result['capabilities']['available'] and result['capabilities']['reason'] == 'not installed'


def test_failed_atomic_write_retains_previous_preferences(tmp_path, monkeypatch):
    store = DisplaySettings(tmp_path)
    store.update({'stream_resolution': '720p'})
    def fail(*args): raise OSError('simulated disk error')
    with monkeypatch.context() as context:
        context.setattr('retroone.profiles.os.replace', fail)
        with pytest.raises(DisplaySettingsError): store.update({'stream_resolution': '1080p'})
    assert store.get() == {'stream_resolution': '720p'}
    assert list(tmp_path.glob('*.tmp')) == []


def _process_updates(directory, resolution):
    store = DisplaySettings(Path(directory))
    for _ in range(12):
        store.update({'stream_resolution': resolution})
        assert store.get()['stream_resolution'] in DISPLAY_RESOLUTIONS


def test_processes_publish_only_complete_documents_and_do_not_follow_symlinks(tmp_path):
    context = multiprocessing.get_context('spawn')
    processes = [context.Process(target=_process_updates, args=(str(tmp_path), value)) for value in ['720p', '1080p'] * 2]
    for process in processes: process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    store = DisplaySettings(tmp_path)
    assert json.loads(store.path.read_text())['settings']['stream_resolution'] in DISPLAY_RESOLUTIONS
    original = store.path.read_text()
    outside = tmp_path / 'unrelated.json'; outside.write_text(original)
    store.path.unlink(); store.path.symlink_to(outside)
    with pytest.raises(DisplaySettingsError): store.update({'stream_resolution': '1080p'})
    assert outside.read_text() == original
