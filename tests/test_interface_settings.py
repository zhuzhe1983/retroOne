"""Personal UI language persistence and the browser player's save-name contract."""
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault('CONFIG_ROOT', tempfile.mkdtemp(prefix='retroone-interface-import-'))
from retroone.api import create_app
from retroone.i18n import MESSAGES, emulator_locale, resolve_locale
from retroone.interface_settings import InterfaceSettings, InterfaceSettingsError


class InterfaceRunner:
    def __init__(self):
        self.active = {'id': 'active-session', 'game_id': 'already-playing'}
        self.stops = 0
        self.starts = []

    def capabilities(self):
        return {'platforms': {'nes': {'available': True}}}

    def status(self):
        return self.active

    def start(self, game):
        self.starts.append(game)
        raise AssertionError('language preferences must not start games')

    def stop(self):
        self.stops += 1
        self.active = None


@pytest.fixture
def environment(tmp_path):
    roms = tmp_path / 'roms'
    collection = roms / 'library/nes/new/FC'
    collection.mkdir(parents=True)
    (collection / '原游戏名称.nes').write_bytes(b'fixture, not a playable ROM')
    vendor = tmp_path / 'vendor'
    (vendor / 'cores').mkdir(parents=True)
    (vendor / 'loader.js').write_text('// no emulator execution in API tests')
    (vendor / 'cores/fceumm-wasm.data').write_bytes(b'availability fixture')
    runner = InterfaceRunner()
    app = create_app(roms, tmp_path / 'player1', runner=runner, browser_data=vendor,
                     auto_scan=False, profile_id='player1', base_path='/p/player1')
    app.state.catalog.scan()
    game = app.state.catalog.list()['games'][0]
    with TestClient(app) as client:
        yield app, client, runner, game, roms, vendor


def page_settings(response):
    assert response.status_code == 200, response.text
    return json.loads(re.search(r'Object\.assign\(window,(\{.*?\})\);window\.EJS_threads', response.text).group(1))


def test_interface_api_defaults_and_strict_fields(environment):
    app, client, runner, _, _, _ = environment
    expected = {'settings': {'locale': 'zh-CN'}, 'supported_locales': ['zh-CN', 'en']}
    result = client.get('/api/settings/interface', headers={'Accept-Language': 'en-US'})
    assert result.json() == expected and result.headers['cache-control'] == 'no-store'
    assert not app.state.interface_settings.path.exists()
    for invalid in ({}, {'locale': 'en-US'}, {'locale': 'zh'}, {'locale': False}, {'locale': 1},
                    {'locale': None}, {'locale': []}, {'locale': 'en', 'profile_id': 'player2'},
                    {'locale': 'en', 'stream_resolution': '1080p'}, {'settings': {'locale': 'en'}}):
        assert client.put('/api/settings/interface', json=invalid).status_code == 422
    assert not app.state.interface_settings.path.exists()
    for locale in ('en', 'auto', 'zh-CN'):
        result = client.put('/api/settings/interface', json={'locale': locale})
        assert result.status_code == 200
        assert result.json() == dict(expected, settings={'locale': locale})
    assert runner.stops == 0 and runner.starts == [] and runner.active['id'] == 'active-session'


def test_settings_do_not_touch_display_saves_or_other_profiles(environment, tmp_path):
    app, client, runner, _, roms, vendor = environment
    config = app.state.interface_settings.path.parent
    display = config / 'display-settings.json'
    display.write_text('{"version":1,"settings":{"stream_resolution":"1080p"}}')
    save = config / 'browser-saves/preserved.state'
    save.parent.mkdir(); save.write_bytes(b'unchanged personal save')
    original_display, original_save = display.read_bytes(), save.read_bytes()
    client.put('/api/settings/interface', json={'locale': 'en'})
    assert InterfaceSettings(config).get() == {'locale': 'en'}
    other_runner = InterfaceRunner()
    other = create_app(roms, tmp_path / 'player2', runner=other_runner, browser_data=vendor,
                       auto_scan=False, profile_id='player2', base_path='/p/player2')
    with TestClient(other) as second:
        assert second.get('/api/settings/interface').json()['settings'] == {'locale': 'zh-CN'}
        second.put('/api/settings/interface', json={'locale': 'auto'})
        assert client.get('/api/settings/interface').json()['settings'] == {'locale': 'en'}
        assert other_runner.stops == 0 and not other_runner.starts
    assert display.read_bytes() == original_display and save.read_bytes() == original_save
    assert runner.stops == 0 and not runner.starts


def test_english_browser_page_preserves_game_title_idb_name_and_server_save(environment):
    app, client, runner, game, _, _ = environment
    game_id = game['id']
    chinese_page = client.get('/play/' + game_id)
    chinese = page_settings(chinese_page)
    client.put('/api/settings/interface', json={'locale': 'en'})
    app.state.catalog.game_metadata.update_overrides(game_id, {'title': '人工展示名称'})
    save = client.put('/api/games/' + game_id + '/save-state', content=b'personal-state')
    assert save.status_code == 200
    english_page = client.get('/play/' + game_id + '?restore=true', headers={'Accept-Language': 'zh-CN'})
    english = page_settings(english_page)
    assert '<html lang="en">' in english_page.text
    assert english_page.headers['content-language'] == 'en'
    assert english_page.headers['vary'] == 'Accept-Language' and english_page.headers['cache-control'] == 'no-store'
    for phrase in ('Back to library', 'Load server save', 'Export server save', 'Upload existing save',
                   'Save backed up to the server', 'The emulator did not produce a valid save state'):
        assert phrase in english_page.text
    assert '<h1>人工展示名称</h1>' in english_page.text
    assert english['EJS_language'] == 'en-US' and english['EJS_startButtonName'] == 'Start game'
    assert chinese['EJS_language'] == 'zh-CN' and chinese['EJS_startButtonName'] == '开始游戏'
    for key in ('EJS_gameName', 'EJS_gameID', 'EJS_gameUrl', 'EJS_defaultControls'):
        assert chinese[key] == english[key]
    assert english['EJS_gameName'] == '原游戏名称'
    assert 'const storageNamespace="";' in english_page.text
    assert english['EJS_loadStateURL'] == f'/p/player1/api/games/{game_id}/save-state'
    assert client.get('/api/games/' + game_id + '/save-state').content == b'personal-state'
    assert runner.stops == 0 and not runner.starts


def test_second_player_idb_namespace_stays_identical_when_language_changes(environment, tmp_path):
    _, _, _, game, roms, vendor = environment
    other = create_app(roms, tmp_path / 'player2', runner=InterfaceRunner(), browser_data=vendor,
                       auto_scan=False, profile_id='player2', base_path='/p/player2')
    other.state.catalog.scan()
    with TestClient(other) as client:
        first = client.get('/play/' + game['id'])
        client.put('/api/settings/interface', json={'locale': 'en'})
        second = client.get('/play/' + game['id'])
        assert page_settings(first)['EJS_gameName'] == page_settings(second)['EJS_gameName']
        assert page_settings(second)['EJS_gameName'] == f"player2_{game['id']}_原游戏名称"
        for response in (first, second):
            assert 'const storageNamespace="RetroOne-player2-";' in response.text


def test_auto_locale_uses_request_language_without_persisting_effective_value(environment):
    _, client, _, game, _, _ = environment
    route = '/play/' + game['id']
    client.put('/api/settings/interface', json={'locale': 'auto'})
    assert page_settings(client.get(route, headers={'Accept-Language': 'de,en-GB;q=0.8,zh;q=0.5'}))['EJS_language'] == 'en-US'
    assert page_settings(client.get(route, headers={'Accept-Language': 'zh-TW, en;q=0.9'}))['EJS_language'] == 'zh-CN'
    assert page_settings(client.get(route))['EJS_language'] == 'zh-CN'
    assert client.get('/api/settings/interface').json()['settings'] == {'locale': 'auto'}


@pytest.mark.parametrize('header,expected', [
    ('en-US,en;q=0.9', 'en'), ('zh-Hans-CN,en;q=0.9', 'zh-CN'),
    ('en;q=0.2,zh-CN;q=0.8', 'zh-CN'), ('fr-FR,en-GB;q=0.4', 'en'),
    ('de-DE', 'zh-CN'), ('*', 'zh-CN'), ('zh;q=0,*;q=0.8', 'en'),
    ('en;q=0,*;q=0.8', 'zh-CN'), ('en;q=0,zh;q=0', 'zh-CN'),
    ('en;q=2,zh;q=0.7', 'zh-CN'), ('en;q=NaN', 'zh-CN'),
    ('en;q=0.9;q=0.1,zh', 'zh-CN'), ('en;q=bogus,*', 'zh-CN'),
    ('en;q=.7,zh;q=.2', 'en'), ('zh;q=0.5,en;q=0.5', 'zh-CN'),
    ('en;q=0.5,zh;q=0.5', 'en'), ('', 'zh-CN'),
])
def test_language_negotiation(header, expected):
    assert resolve_locale('auto', header) == expected
    assert resolve_locale('en', header) == 'en'
    assert resolve_locale('zh-CN', header) == 'zh-CN'


def test_player_translation_catalogs_have_same_keys_and_upstream_language_codes():
    assert set(MESSAGES['zh-CN']) == set(MESSAGES['en'])
    assert emulator_locale('en') == 'en-US' and emulator_locale('zh-CN') == 'zh-CN'


def test_server_save_errors_follow_locale_and_corrupt_preferences_do_not_block_save(environment):
    app, client, runner, game, _, _ = environment
    route = '/api/games/' + game['id'] + '/save-state'
    client.put('/api/settings/interface', json={'locale': 'en'})
    assert client.get(route).json()['detail'] == MESSAGES['en']['save_not_found']
    assert client.put(route, content=b'').json()['detail'] == MESSAGES['en']['save_empty']
    path = app.state.interface_settings.path
    path.write_text('{corrupt document')
    result = client.get('/api/settings/interface', headers={'Accept-Language': 'en'})
    assert result.status_code == 503 and result.json()['detail'] == MESSAGES['en']['interface_settings_unavailable']
    assert client.put('/api/settings/interface', json={'locale': 'zh-CN'}).status_code == 503
    assert client.put(route, content=b'current-game-progress').status_code == 200
    assert client.get(route).content == b'current-game-progress'
    assert path.read_text() == '{corrupt document'
    assert runner.stops == 0 and not runner.starts and runner.active['id'] == 'active-session'


@pytest.mark.parametrize('code', ['interface_settings_busy', 'interface_settings_unavailable'])
def test_settings_error_handler_does_not_retry_failed_or_busy_store(environment, monkeypatch, code):
    app, client, _, _, _, _ = environment
    reads = []
    def unavailable():
        reads.append(True)
        raise InterfaceSettingsError('synthetic unavailable preference', code=code)
    monkeypatch.setattr(app.state.interface_settings, 'get', unavailable)
    response = client.get('/api/settings/interface', headers={'Accept-Language': 'en-US,en;q=0.9'})
    assert response.status_code == 503
    assert response.json()['detail'] == MESSAGES['en'][code]
    assert len(reads) == 1  # The async error handler must not retry/block on I/O.


@pytest.mark.parametrize('document', [
    '{invalid', '[]', '{"version":2,"settings":{"locale":"en"}}',
    '{"version":true,"settings":{"locale":"en"}}',
    '{"version":1,"settings":{"locale":"en-US"}}',
    '{"version":1,"settings":{"locale":"en","extra":1}}', 'x' * 4097,
])
def test_corrupt_documents_are_never_replaced(tmp_path, document):
    store = InterfaceSettings(tmp_path)
    store.path.write_text(document)
    for call in (store.get, lambda: store.update({'locale': 'en'})):
        with pytest.raises(InterfaceSettingsError) as failure: call()
        assert failure.value.status_code == 503
        assert store.path.read_text() == document


def test_failed_atomic_write_preserves_previous_settings_and_removes_temporary(tmp_path, monkeypatch):
    store = InterfaceSettings(tmp_path)
    store.update({'locale': 'en'})
    original = store.path.read_bytes()
    def fail(*args): raise OSError('simulated interrupted publish')
    with monkeypatch.context() as context:
        context.setattr('retroone.profiles.os.replace', fail)
        with pytest.raises(InterfaceSettingsError): store.update({'locale': 'auto'})
    assert store.path.read_bytes() == original and store.get() == {'locale': 'en'}
    assert not list(tmp_path.glob('.*.tmp'))


def test_repeated_save_does_not_rewrite_and_symlinks_are_rejected(tmp_path):
    store = InterfaceSettings(tmp_path)
    store.update({'locale': 'en'})
    before = store.path.stat().st_mtime_ns
    store.update({'locale': 'en'})
    assert store.path.stat().st_mtime_ns == before
    other = tmp_path / 'unrelated.json'; other.write_text('do not overwrite')
    store.path.unlink(); store.path.symlink_to(other)
    with pytest.raises(InterfaceSettingsError): store.update({'locale': 'auto'})
    assert other.read_text() == 'do not overwrite'
    store.path.unlink(); store.lock_path.unlink(); store.lock_path.symlink_to(other)
    with pytest.raises(InterfaceSettingsError): store.get()
    assert other.read_text() == 'do not overwrite'


def _write_preferences(directory, locale):
    store = InterfaceSettings(Path(directory))
    for _ in range(12):
        store.update({'locale': locale})
        assert store.get()['locale'] in ('en', 'zh-CN', 'auto')


def test_parallel_processes_publish_only_complete_valid_documents(tmp_path):
    with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context('spawn')) as pool:
        tasks = [pool.submit(_write_preferences, str(tmp_path), locale) for locale in ('en', 'zh-CN', 'auto')]
        for task in tasks: task.result(timeout=30)
    value = json.loads(InterfaceSettings(tmp_path).path.read_text())
    assert value['version'] == 1 and value['settings']['locale'] in ('en', 'zh-CN', 'auto')


def test_index_versions_both_language_assets_and_revalidates_them(environment, tmp_path, monkeypatch):
    _, client, _, _, _, _ = environment
    web = tmp_path / 'web'; web.mkdir()
    scripts = {'app.js': '// app', 'i18n.js': '// locale controller', 'i18n-en.js': '// English dictionary'}
    for filename, source in scripts.items():
        (web / filename).write_text(source)
    (web / 'index.html').write_text('<script src="/app.js?v=old"></script><script src="/i18n.js"></script><script src="/i18n-en.js?v=old"></script>')
    # versioned_index captures ROOT/web at create_app time; use a second app with
    # a tiny task-owned web fixture, not mutations of the real frontend files.
    monkeypatch.setattr('retroone.api.ROOT', tmp_path)
    app = create_app(tmp_path / 'empty-roms', tmp_path / 'static-config', runner=InterfaceRunner(), auto_scan=False)
    with TestClient(app) as other:
        index = other.get('/')
        for filename, source in scripts.items():
            expected = hashlib.sha256(source.encode()).hexdigest()[:16]
            assert f'/{filename}?v={expected}' in index.text
            assert other.get('/' + filename).headers['cache-control'] == 'no-cache, max-age=0, must-revalidate'
