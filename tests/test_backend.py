"""Meaningful safety and library contract tests; no emulator is mocked as playable."""
from __future__ import annotations

import json
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from uuid import uuid4
from unittest.mock import patch
from retroone.artwork import ArtworkCache, thumbnail_names

os.environ.setdefault('CONFIG_ROOT', tempfile.mkdtemp(prefix='retroone-import-test-'))
from fastapi.testclient import TestClient
from retroone.api import create_app
from retroone.catalog import BROWSER_LIMIT, Catalog, inspect_archive, safe_archive_name, safe_core_hint


class FakeRunner:
    def __init__(self):
        self.active = None

    def capabilities(self):
        return {'available': True, 'platforms': {'dos': {'available': True}, 'psx': {'available': True}, 'nes': {'available': True}}, 'max_sessions': 1}

    def status(self):
        return self.active

    def start(self, game):
        if self.active:
            raise RuntimeError('already running')
        self.active = {'id': 'test-session', 'game_id': game['id']}
        return self.active

    def stop(self):
        self.active = None
        return {'stopped': True}


class LibraryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.roms = self.root / 'one'
        self.config = self.root / 'config'
        self.library = Catalog(self.roms, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def collection(self, platform='nes', name='FC'):
        directory = self.roms / 'library' / platform / 'new' / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def archive(path, entries):
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)

    def test_archive_threshold_uses_all_unpacked_members(self):
        directory = self.collection()
        self.archive(directory / 'compact.zip', {'first.nes': b'0' * (11 * 1024 * 1024), 'second.nes': b'1' * (11 * 1024 * 1024)})
        self.library.scan()
        game = self.library.list()['games'][0]
        self.assertLess(game['size'], BROWSER_LIMIT)
        self.assertEqual(game['unpacked_size'], 22 * 1024 * 1024)
        self.assertEqual(game['mode'], 'stream')
        self.assertFalse(game['browser_eligible'])

    def test_boundary_inclusive(self):
        directory = self.collection()
        (directory / 'edge.nes').write_bytes(b'0' * BROWSER_LIMIT)
        self.library.scan()
        self.assertTrue(self.library.list()['games'][0]['browser_eligible'])

    def test_zip_traversal_and_symlinks_rejected(self):
        directory = self.collection()
        self.archive(directory / 'unsafe.zip', {'../secret.nes': b'bad'})
        self.assertIsNotNone(inspect_archive(directory / 'unsafe.zip')[1])
        link = zipfile.ZipInfo('linked.nes')
        link.external_attr = 0o120777 << 16
        with zipfile.ZipFile(directory / 'symlink.zip', 'w') as archive:
            archive.writestr(link, '/etc/passwd')
        self.assertIsNotNone(inspect_archive(directory / 'symlink.zip')[1])
        for value in ('../../escape', '/etc/passwd', 'C:\\secret', 'folder\\..\\secret'):
            self.assertFalse(safe_archive_name(value))

    def test_metadata_cover_search_favorite_survive_scan(self):
        directory = self.collection()
        self.archive(directory / 'mario.zip', {'mario.nes': b'rom'})
        cover = directory / 'media/mario/boxFront.jpg'
        cover.parent.mkdir(parents=True)
        cover.write_bytes(b'cover')
        (directory / 'metadata.pegasus.txt').write_text('collection: FC\ngame: 超级马里奥\nfile: mario.zip\nsort-by: 001\ndescription: 童年的游戏\\n第二行\n', encoding='utf-8')
        self.library.scan()
        game = self.library.list(q='马里奥')['games'][0]
        self.assertEqual(game['description'], '童年的游戏\n第二行')
        self.assertTrue(game['cover_url'].endswith('/media/cover'))
        self.library.set_favorite(game['id'], True)
        self.library.scan()
        self.assertEqual(self.library.list(favorite=True)['total'], 1)
        self.assertEqual(game['id'], self.library.list()['games'][0]['id'])

    def test_multidisc_playlist_counted_once_and_preserves_parents(self):
        directory = self.collection('psx', 'PS1')
        disc = directory / '传奇'
        disc.mkdir()
        (disc / 'game.m3u').write_text('disc1.cue\ndisc2.cue\n')
        (disc / 'disc1.cue').write_text('FILE "disc1.bin" BINARY\n TRACK 01 MODE2/2352\n')
        (disc / 'disc2.cue').write_text('FILE "disc2.bin" BINARY\n TRACK 01 MODE2/2352\n')
        (disc / 'disc1.bin').write_bytes(b'1' * 100)
        (disc / 'disc2.bin').write_bytes(b'2' * 200)
        self.library.scan()
        games = self.library.list()['games']
        self.assertEqual(len(games), 1)
        self.assertEqual(len(games[0]['files']), 5)
        self.assertGreater(games[0]['size'], 300)
        self.assertFalse(games[0]['browser_eligible'])

    def test_missing_cue_track_unavailable(self):
        directory = self.collection('psx', 'PS1')
        (directory / 'broken.cue').write_text('FILE "absent.bin" BINARY\n')
        self.library.scan()
        self.assertEqual(self.library.list()['games'][0]['mode'], 'unavailable')

    def test_metadata_cannot_reference_outside_library(self):
        directory = self.collection()
        secret = self.root / 'secret.nes'
        secret.write_bytes(b'secret')
        (directory / 'metadata.pegasus.txt').write_text(f'game: secret\nfile: {secret}\n')
        (directory / 'link.nes').symlink_to(secret)
        self.library.scan()
        self.assertEqual(self.library.count(), 0)

    def test_windows_helpers_not_games(self):
        directory = self.collection('windows', 'PC')
        for file in ('PAL/start.bat', 'PAL/driver/setup.exe', 'PAL/driver/service.exe', 'PAL/game.exe'):
            path = directory / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x')
        (directory / 'metadata.pegasus.txt').write_text('game: 仙剑98\nfile: PAL/start.bat\n')
        self.library.scan()
        self.assertEqual(self.library.count(), 1)
        self.assertEqual(self.library.list()['games'][0]['title'], '仙剑98')

    def test_windows_metadata_case_mismatch_resolved_without_duplicate(self):
        directory = self.collection('windows', 'PC')
        folder = directory / '幻世录II'
        folder.mkdir()
        (folder / 'HSLPRE.EXE').write_bytes(b'program')
        (directory / 'metadata.pegasus.txt').write_text('game: 幻世录II\nfile: 幻世录II/HSLPRE.exe\n')
        status = self.library.scan()
        self.assertEqual(status['errors'], 0)
        self.assertEqual(self.library.count(), 1)
        self.assertTrue(Path(self.library.list()['games'][0]['rom_path']).samefile(folder / 'HSLPRE.EXE'))

    def test_stale_metadata_reference_reported_separately(self):
        directory = self.collection()
        (directory / 'metadata.pegasus.txt').write_text('game: Not collected\nfile: absent.zip\n')
        status = self.library.scan()
        self.assertEqual(status['error_types']['metadata_reference_missing_or_unsafe'], 1)
        self.assertEqual(status['warning_samples'][0]['reference'], 'absent.zip')
        self.assertEqual(self.library.count(), 1)
        missing = self.library.list()['games'][0]
        self.assertEqual(missing['title'], 'Not collected')
        self.assertEqual(missing['mode'], 'unavailable')
        self.assertEqual(missing['launch_config']['status'], 'blocked')

    def test_slow_runtime_refresh_does_not_block_other_requests(self):
        app = create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False)
        entered, release = threading.Event(), threading.Event()

        def slow_refresh(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test refresh was not released')

        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(app.state.catalog, 'apply_runtime', side_effect=slow_refresh) as refresh:
                first = pool.submit(client.get, '/api/health')
                self.assertTrue(entered.wait(3))
                try:
                    response = pool.submit(client.get, '/api/session').result(timeout=2)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(refresh.call_count, 1)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3).status_code, 200)

    def test_missing_bios_is_not_walked_on_every_refresh(self):
        (self.roms / 'firmware').mkdir(parents=True)
        app = create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False)
        with TestClient(app) as client, patch('retroone.api.os.walk', wraps=os.walk) as walk:
            self.assertEqual(client.get('/api/health').status_code, 200)
            first_count = walk.call_count
            self.assertGreater(first_count, 0)
            app.state.catalog.scan_status['finished_at'] = 'new scan invalidates mode cache'
            self.assertEqual(client.get('/api/health').status_code, 200)
            self.assertEqual(walk.call_count, first_count)

    def test_teknoparrot_directory_entry(self):
        directory = self.collection('teknoparrot', 'TeknoParrot')
        (directory / 'Ikaruga/system/drivers').mkdir(parents=True)
        (directory / 'Ikaruga/system/drivers/setup.exe').write_bytes(b'x')
        (directory / 'metadata.pegasus.txt').write_text('game: 斑鸠\nfile: Ikaruga\\\n')
        self.library.scan()
        game = self.library.list()['games'][0]
        self.assertEqual(self.library.count(), 1)
        self.assertFalse(game['size_complete'])
        self.assertFalse(game['browser_eligible'])

    def test_only_platform_extensions_indexed(self):
        directory = self.collection()
        (directory / 'mario.nes').write_bytes(b'rom')
        (directory / 'setup.exe').write_bytes(b'helper')
        self.library.scan()
        self.assertEqual(self.library.count(), 1)

    def test_core_hint_is_allowlisted_never_launch_executable(self):
        self.assertEqual(safe_core_hint('"C:\\retroarch.exe" -L "cores\\fbneo_libretro.dll" "{file.path}"'), 'fbneo')
        self.assertIsNone(safe_core_hint('/bin/sh -c "evil_libretro.so"'))
        directory = self.collection('mame', 'MAME')
        self.archive(directory / 'game.zip', {'cpu.bin': b'rom'})
        (directory / 'metadata.pegasus.txt').write_text('launch: retroarch -L mame2010_libretro.dll\ngame: Game\nfile: game.zip\n')
        self.library.scan()
        game = self.library.list()['games'][0]
        self.assertEqual(game['native_core'], 'mame2010')
        self.assertFalse(game['browser_eligible'])

    def test_missing_library_keeps_existing_index(self):
        directory = self.collection()
        (directory / 'mario.nes').write_bytes(b'rom')
        self.library.scan()
        (self.roms / 'library').rename(self.roms / 'offline')
        result = self.library.scan()
        self.assertIsNotNone(result['last_error'])
        self.assertEqual(self.library.count(), 1)

    def test_api_download_rechecks_archive_and_rejects_session_takeover(self):
        directory = self.collection()
        rom = directory / 'mario.zip'
        self.archive(rom, {'mario.nes': b'rom'})
        large_dir = self.collection('dos', 'DOS')
        self.archive(large_dir / 'pal.zip', {'pal.exe': b'0' * (21 * 1024 * 1024)})
        vendor = self.root / 'vendor'
        vendor.mkdir()
        (vendor / 'loader.js').write_text('/* fixture, not an emulator */')
        (vendor / 'cores').mkdir()
        (vendor / 'cores/fceumm-wasm.data').write_bytes(b'test core availability marker')
        app = create_app(self.roms, self.config, runner=FakeRunner(), browser_data=vendor, auto_scan=False)
        app.state.catalog.scan()
        with TestClient(app) as client:
            games = client.get('/api/games').json()['games']
            small = next(g for g in games if g['platform'] == 'nes')
            large = next(g for g in games if g['platform'] == 'dos')
            self.assertNotIn('rom_path', small)
            self.assertIn(f"/rom/{small['id']}.nes", client.get(f"/play/{small['id']}").text)
            self.assertEqual(client.get(f"/api/games/{small['id']}/rom/{small['id']}.nes").content, b'rom')
            self.assertEqual(client.get(f"/api/games/{small['id']}/rom/mario.zip").status_code, 200)
            self.assertEqual(client.get(f"/api/games/{large['id']}/rom/pal.zip").status_code, 403)
            response = client.post(f"/api/games/{large['id']}/launch", json={})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['mode'], 'stream')
            self.assertEqual(client.post(f"/api/games/{large['id']}/launch", json={}).status_code, 200)
            self.assertEqual(client.post(f"/api/games/{small['id']}/launch", json={'mode': 'stream'}).status_code, 409)
            self.assertEqual(client.post('/api/session/stop').status_code, 200)
            self.archive(rom, {'mario.nes': b'0' * (21 * 1024 * 1024)})
            self.assertEqual(client.get(f"/api/games/{small['id']}/rom/mario.zip").status_code, 403)

    def test_api_media_and_save_are_id_scoped_and_origin_protected(self):
        directory = self.collection()
        (directory / 'mario.nes').write_bytes(b'rom')
        app = create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False)
        app.state.catalog.scan()
        game = app.state.catalog.list()['games'][0]
        url = f"/api/games/{game['id']}/save-state"
        with TestClient(app) as client:
            self.assertEqual(client.put(url, content=b'state', headers={'Origin': 'https://evil.example'}).status_code, 403)
            self.assertEqual(client.put(url, content=b'state').status_code, 200)
            self.assertEqual(client.get(url).content, b'state')
            self.assertEqual(client.get('/api/games/unknown/save-state').status_code, 404)
            self.assertEqual(client.get(f"/api/games/{game['id']}/media/rom_path").status_code, 404)
            self.assertEqual(client.get('/api/games?limit=9999').status_code, 422)
            self.assertEqual(client.get('/').headers['Cross-Origin-Opener-Policy'], 'same-origin')

    def test_two_player_configs_keep_saves_favorites_history_and_sessions_independent(self):
        directory = self.collection()
        (directory / 'mario.nes').write_bytes(b'rom')
        vendor = self.root / 'player-vendor'
        (vendor / 'cores').mkdir(parents=True)
        (vendor / 'loader.js').write_text('/* fixture */')
        (vendor / 'cores/fceumm-wasm.data').write_bytes(b'core-marker')
        apps = [create_app(self.roms, self.root / f'player{number}', runner=FakeRunner(), browser_data=vendor, auto_scan=False,
                          profile_id=f'player{number}', base_path=f'/p/player{number}') for number in (1, 2)]
        for app in apps:
            app.state.catalog.scan()
        game = apps[0].state.catalog.list()['games'][0]
        game_id = game['id']
        old_save = self.root / 'player1/browser-saves' / f'{game_id}.state'
        old_save.parent.mkdir()
        old_save.write_bytes(b'existing player one state')
        with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
            route = f'/api/games/{game_id}'
            self.assertEqual(first.get(route + '/save-state').content, b'existing player one state')
            self.assertEqual(second.get(route + '/save-state').status_code, 404)
            self.assertEqual(second.put(route + '/save-state', content=b'player two state').status_code, 200)
            self.assertEqual(first.get(route + '/save-state').content, b'existing player one state')
            first.post(route + '/favorite', json={'favorite': True})
            self.assertEqual(first.get('/api/games?favorite=true').json()['total'], 1)
            self.assertEqual(second.get('/api/games?favorite=true').json()['total'], 0)
            self.assertEqual(first.post(route + '/launch', json={'mode': 'browser'}).json()['play_url'], f'/p/player1/play/{game_id}')
            self.assertEqual(first.get('/api/games?recent=true').json()['total'], 1)
            self.assertEqual(second.get('/api/games?recent=true').json()['total'], 0)
            for number, client in [(1, first), (2, second)]:
                result = client.post(route + '/launch', json={'mode': 'stream'}).json()
                self.assertEqual(result['play_url'], f'/p/player{number}/stream/')
                self.assertEqual(result['session']['profile_id'], f'player{number}')
            self.assertEqual(first.post(route + '/launch', json={'mode': 'stream'}).status_code, 200)
            first.post('/api/session/stop')
            self.assertIsNotNone(second.get('/api/session').json()['session'])
            first_page = first.get(f'/play/{game_id}').text
            second_page = second.get(f'/play/{game_id}').text
            self.assertIn('"EJS_gameName": "mario"', first_page)
            self.assertIn('const storageNamespace=""', first_page)
            self.assertIn('RetroOne-player2-', second_page)
            self.assertIn(f'player2_{game_id}_mario', second_page)
            self.assertIn(f'/p/player2/api/games/{game_id}/save-state', second_page)
            self.assertIn('/p/player2/emulatorjs/data/loader.js', second_page)

    def test_player_identity_is_configurable_but_route_and_avatar_are_allowlisted(self):
        app = create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False)
        with TestClient(app) as client:
            initial = client.get('/api/profiles').json()['profiles']
            self.assertFalse(initial[0]['configured'])
            result = client.patch('/api/profiles/player1', json={'name': '  爸爸  ', 'avatar': 'cat'})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), {'id': 'player1', 'name': '爸爸', 'avatar': 'cat', 'configured': True, 'base_path': '/p/player1', 'status': 'ready', 'error': None})
            self.assertEqual(client.patch('/api/profiles/player2', json={'name': ' ', 'avatar': 'cat'}).status_code, 422)
            self.assertEqual(client.patch('/api/profiles/player2', json={'name': '孩子', 'avatar': '../secret'}).status_code, 422)
            self.assertEqual(client.patch('/api/profiles/unknown', json={'name': '孩子', 'avatar': 'star'}).status_code, 404)
            self.assertEqual(client.patch('/api/profiles/player1', json={'name': 'changed', 'avatar': 'star'}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        reopened = create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False)
        with TestClient(reopened) as client:
            self.assertEqual(client.get('/api/profiles').json()['profiles'][0]['name'], '爸爸')

    def test_recent_sorts_last_played_first_and_preserves_filters(self):
        directory = self.collection()
        for name in ('a', 'b', 'never-played'):
            (directory / f'{name}.nes').write_bytes(b'rom')
        self.library.scan()
        games = {game['title']: game for game in self.library.list()['games']}
        with patch('retroone.catalog.time.time', return_value=100):
            self.library.mark_played(games['b']['id'])
        with patch('retroone.catalog.time.time', return_value=200):
            self.library.mark_played(games['a']['id'])
        self.assertEqual([game['title'] for game in self.library.list(recent=True)['games']], ['a', 'b'])
        self.assertEqual(self.library.list(recent=True, q='never')['total'], 0)

    def test_dynamic_profile_api_creation_status_and_retry_are_honest(self):
        app = create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False)
        with TestClient(app) as client:
            body = {'name': '新成员', 'avatar': 'cat', 'request_id': str(uuid4())}
            created = client.post('/api/profiles', json=body)
            self.assertEqual(created.status_code, 202)
            self.assertEqual((created.json()['id'], created.json()['status']), ('player3', 'provisioning'))
            self.assertEqual(client.post('/api/profiles', json=body).json()['id'], 'player3')
            self.assertEqual(client.post('/api/profiles', json=dict(body, container_name='escape')).status_code, 422)
            self.assertEqual(client.post('/api/profiles', json=dict(body, request_id='../../escape')).status_code, 422)
            self.assertEqual(client.post('/api/profiles', json=body, headers={'Origin': 'https://evil.example'}).status_code, 403)
            listing = client.get('/api/profiles').json()
            self.assertEqual(listing['capacity']['total'], 3)
            self.assertFalse(listing['profiles'][-1]['presence']['online'])
            state = json.loads((self.config / 'profiles.json').read_text())
            job = state['profiles']['player3']['job_id']
            result = self.config / 'profile-control/results' / (job + '.json')
            result.write_text(json.dumps({'version': 1, 'job_id': job, 'profile_id': 'player3', 'status': 'error', 'error': '运行环境暂时不可用'}))
            self.assertEqual(client.get('/api/profiles').json()['profiles'][-1]['status'], 'error')
            self.assertEqual(client.post('/api/profiles/player3/retry', json={}).json()['status'], 'provisioning')
            self.assertEqual(client.patch('/api/profiles/player3', json={'name': '改名', 'avatar': 'star'}).json()['name'], '改名')
            (self.config / 'profiles.json').write_text('{corrupted')
            self.assertEqual(client.get('/api/profiles').status_code, 503)
            self.assertEqual(client.post('/api/profiles', json=dict(body, request_id=str(uuid4()))).status_code, 503)

    def test_presence_tracks_live_tabs_not_a_running_cloud_session(self):
        directory = self.collection(); (directory / 'mario.nes').write_bytes(b'rom')
        runner = FakeRunner()
        app = create_app(self.roms, self.config, runner=runner, auto_scan=False)
        app.state.catalog.scan(); game = app.state.catalog.list()['games'][0]
        with TestClient(app) as client:
            runner.start(game)
            self.assertFalse(client.get('/api/profiles').json()['profiles'][0]['presence']['online'])
            first, second = str(uuid4()), str(uuid4())
            playing = {'profile_id': 'player1', 'client_id': first, 'activity': 'stream', 'game_id': game['id']}
            self.assertEqual(client.post('/api/presence', json=playing).status_code, 200)
            self.assertEqual(client.post('/api/presence', json=dict(playing, client_id=second, activity='library', game_id=None)).status_code, 200)
            presence = client.get('/api/profiles').json()['profiles'][0]['presence']
            self.assertEqual((presence['clients'], presence['activity'], presence['game_title']), (2, 'stream', 'mario'))
            client.post('/api/presence/leave', json={'profile_id': 'player1', 'client_id': first})
            self.assertTrue(client.get('/api/profiles').json()['profiles'][0]['presence']['online'])
            with patch('retroone.presence.time.time', return_value=__import__('time').time() + 76):
                self.assertFalse(client.get('/api/profiles').json()['profiles'][0]['presence']['online'])
            self.assertIsNotNone(runner.status())
            self.assertEqual(client.post('/api/presence', json=dict(playing, game_id='missing')).status_code, 422)
            self.assertEqual(client.post('/api/presence', json=dict(playing, profile_id='player999')).status_code, 404)

    def test_late_presence_leave_does_not_remove_a_tab_after_profile_switch(self):
        app = create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False)
        with TestClient(app) as client:
            body = {'profile_id': 'player1', 'client_id': str(uuid4()), 'activity': 'library'}
            client.post('/api/presence', json=body)
            client.post('/api/presence', json=dict(body, profile_id='player2'))
            client.post('/api/presence/leave', json={k: body[k] for k in ('profile_id', 'client_id')})
            profiles = client.get('/api/profiles').json()['profiles']
            self.assertFalse(profiles[0]['presence']['online'])
            self.assertTrue(profiles[1]['presence']['online'])

    def test_generated_player_route_uses_distinct_storage_namespace(self):
        directory = self.collection(); (directory / 'mario.nes').write_bytes(b'rom')
        vendor = self.root / 'dynamic-vendor'; (vendor / 'cores').mkdir(parents=True)
        (vendor / 'loader.js').write_text('fixture'); (vendor / 'cores/fceumm-wasm.data').write_bytes(b'fixture')
        app = create_app(self.roms, self.config, runner=FakeRunner(), browser_data=vendor, auto_scan=False, profile_id='player17', base_path='/p/player17')
        app.state.catalog.scan(); game = app.state.catalog.list()['games'][0]
        with TestClient(app) as client:
            page = client.get('/play/' + game['id'])
            self.assertEqual(page.status_code, 200)
            self.assertIn('RetroOne-player17-', page.text)
            self.assertIn('/p/player17/api/games/', page.text)
        with self.assertRaises(ValueError):
            create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False, profile_id='../../escape')

    def test_html_versions_actual_assets_to_avoid_stale_proxy_cache(self):
        web = self.root / 'web'; web.mkdir()
        (web / 'index.html').write_text('<script src="/app.js?v=old"></script><link href="/styles.css">')
        (web / 'app.js').write_text('first build'); (web / 'styles.css').write_text('styles')
        with patch('retroone.api.ROOT', self.root):
            app = create_app(self.roms, self.config, runner=FakeRunner(), auto_scan=False)
        with TestClient(app) as client:
            response = client.get('/?game=some-link')
            first = hashlib.sha256(b'first build').hexdigest()[:16]
            self.assertIn('/app.js?v=' + first, response.text)
            self.assertIn('no-cache', response.headers['Cache-Control'])
            self.assertIn('no-cache', client.get('/app.js').headers['Cache-Control'])
            (web / 'app.js').write_text('next build')
            response = client.get('/index.html')
            self.assertNotIn(first, response.text)
            self.assertIn('/app.js?v=' + hashlib.sha256(b'next build').hexdigest()[:16], response.text)

    def test_cloud_switch_requires_exact_current_session_and_valid_new_game(self):
        directory = self.collection()
        for name in ('first', 'next', 'missing'): (directory / (name + '.nes')).write_bytes(b'rom')
        runner = FakeRunner()
        app = create_app(self.roms, self.config, runner=runner, auto_scan=False)
        app.state.catalog.scan(); games = {g['title']: g for g in app.state.catalog.list()['games']}
        with TestClient(app) as client:
            route = lambda name: '/api/games/' + games[name]['id'] + '/launch'
            current = client.post(route('first'), json={'mode': 'stream'}).json()['session']
            conflict = client.post(route('next'), json={'mode': 'stream'})
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()['detail']['code'], 'session_conflict')
            self.assertEqual(conflict.json()['detail']['game']['title'], 'first')
            runner.active['id'] = 'newer-session'
            self.assertEqual(client.post(route('next'), json={'mode': 'stream', 'replace_session_id': current['id']}).status_code, 409)
            self.assertEqual(runner.active['id'], 'newer-session')
            (directory / 'missing.nes').unlink()
            self.assertEqual(client.post(route('missing'), json={'mode': 'stream', 'replace_session_id': 'newer-session'}).status_code, 409)
            self.assertEqual(runner.active['game_id'], games['first']['id'])
            runner.capabilities = lambda: {'platforms': {'nes': {'available': False, 'reason': 'core unavailable'}}}
            self.assertEqual(client.post(route('next'), json={'mode': 'stream', 'replace_session_id': 'newer-session'}).status_code, 409)
            self.assertEqual(runner.active['game_id'], games['first']['id'])
            runner.capabilities = FakeRunner().capabilities
            switched = client.post(route('next'), json={'mode': 'stream', 'replace_session_id': 'newer-session'})
            self.assertEqual(switched.status_code, 200)
            self.assertEqual(runner.active['game_id'], games['next']['id'])

    def test_missing_browser_core_falls_back_to_native(self):
        directory = self.collection('dos', 'DOS')
        self.archive(directory / 'small.zip', {'game.exe': b'rom'})
        self.library.scan()
        self.library.apply_runtime({'platforms': {'dos': {'available': True}}}, True, {}, {'fceumm'})
        game = self.library.list()['games'][0]
        self.assertTrue(game['browser_eligible'])
        self.assertEqual(game['mode'], 'stream')

    def test_bios_missing_marks_browser_unavailable(self):
        directory = self.collection('fds', 'FDS')
        (directory / 'game.fds').write_bytes(b'rom')
        self.library.scan()
        self.library.apply_runtime({}, True, {'fds': False})
        game = self.library.list()['games'][0]
        self.assertEqual(game['mode'], 'unavailable')
        self.assertIn('BIOS', game['reason'])

    def test_sdlpal_exact_resource_detection(self):
        directory = self.collection('windows', 'PC')
        folder = directory / 'PAL98'
        folder.mkdir()
        for name in ('PAL.EXE', 'ABC.MKF', 'DATA.MKF', 'M.MSG', 'WORD.DAT'):
            (folder / name).write_bytes(b'resource')
        (directory / 'metadata.pegasus.txt').write_text('game: 仙剑98\nfile: PAL98/PAL.EXE\n')
        self.library.scan()
        self.library.apply_runtime({'engines': {'sdlpal': {'available': True}}}, False)
        game = self.library.list()['games'][0]
        self.assertEqual(game['engine_hint'], 'sdlpal')
        self.assertEqual(game['mode'], 'stream')

    def test_wine_only_marks_allowlisted_complete_game_layouts(self):
        directory = self.collection('windows', 'PC')
        metadata = []
        for name, entry, files in [
            ('Rich4', 'rich4.bat', ('rich4.bat', 'RICH4.EXE', 'Data.mkf', 'Effect.mkf', 'Panel.mkf')),
            ('HSL', 'hsl01.bat', ('hsl01.bat', 'hsl01.exe', 'hsl.pak', 'movie.pak')),
            ('Incomplete', 'rich4.exe', ('rich4.exe',)),
            ('Unverified', 'helper.exe', ('helper.exe', 'rich4.exe', 'data.mkf', 'effect.mkf', 'panel.mkf')),
        ]:
            folder = directory / name
            folder.mkdir()
            for filename in files:
                (folder / filename).write_bytes(b'fixture')
            metadata.append(f'game: {name}\nfile: {name}/{entry}\n')
        (directory / 'metadata.pegasus.txt').write_text('\n'.join(metadata))
        self.library.scan()
        self.library.apply_runtime({'engines': {'wine': {'available': True}}}, False)
        by_title = {game['title']: game for game in self.library.list()['games']}
        for title in ('Rich4', 'HSL'):
            self.assertEqual(by_title[title]['engine_hint'], 'wine')
            self.assertEqual(by_title[title]['mode'], 'stream')
            self.assertTrue(by_title[title]['rom_path'].endswith('.bat'))
        for title in ('Incomplete', 'Unverified'):
            self.assertIsNone(by_title[title]['engine_hint'])
            self.assertEqual(by_title[title]['mode'], 'unavailable')

    def test_artwork_matches_exact_name_and_caches_source(self):
        game = {'id': 'one', 'platform': 'nes', 'rom_path': '/roms/Super Mario Bros. (World).zip', 'title': '超级马里奥'}
        self.assertEqual(thumbnail_names(game)[0], 'Super Mario Bros. (World)')
        cache = ArtworkCache(self.config)
        data = b'\x89PNG\r\n\x1a\n' + b'fixture'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return 'https://raw.githubusercontent.com/exact.png'
            def read(self, limit): return data
        try:
            with patch('retroone.artwork.urlopen', return_value=Response()) as request:
                path = cache.get(game)
                self.assertEqual(path.read_bytes(), data)
                self.assertIn('Super%20Mario%20Bros.%20%28World%29.png', request.call_args.args[0].full_url)
                self.assertEqual(cache.get(game), path)
                self.assertEqual(request.call_count, 1)
                self.assertTrue(path.with_suffix('.json').is_file())
        finally:
            cache.close()

    def test_pagination_and_literal_search(self):
        directory = self.collection()
        for name in ('a', 'b', '50%_done'):
            (directory / f'{name}.nes').write_bytes(b'rom')
        self.library.scan()
        self.assertEqual(self.library.list(q='%_')['total'], 1)
        result = self.library.list(offset=1, limit=1)
        self.assertEqual(result['total'], 3)
        self.assertEqual(len(result['games']), 1)


if __name__ == '__main__':
    unittest.main()
