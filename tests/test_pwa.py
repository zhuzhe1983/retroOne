"""Public PWA resources and privacy-preserving cache version integration."""
import hashlib
import json
from pathlib import Path
import shutil
import struct

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from retroone.pwa import OFFLINE_ASSETS, register_pwa

WEB = Path(__file__).resolve().parents[1] / 'web'


@pytest.fixture
def environment(tmp_path):
    web = tmp_path / 'web'
    for name in (*OFFLINE_ASSETS, 'manifest.webmanifest', 'service-worker.js', 'pwa-install.js'):
        target = web / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(WEB / name, target)
    app = FastAPI()
    register_pwa(app, web)
    with TestClient(app) as client:
        yield client, web


def test_manifest_root_scope_and_icons_are_installable_owned_ui_assets(environment):
    client, _ = environment
    response = client.get('/manifest.webmanifest')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/manifest+json')
    manifest = response.json()
    assert manifest['id'] == manifest['scope'] == manifest['start_url'] == '/'
    assert manifest['display'] == 'standalone' and manifest['orientation'] == 'any'
    assert set(item['purpose'] for item in manifest['icons']) == {'any', 'maskable'}
    for icon in manifest['icons']:
        response = client.get(icon['src'])
        assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
        size = int(icon['sizes'].split('x')[0])
        assert response.content[:8] == b'\x89PNG\r\n\x1a\n'
        assert struct.unpack('>II', response.content[16:24]) == (size, size)
    provenance = json.loads((WEB / 'icons/manifest.json').read_text())
    assert provenance['source'] == 'web/favicon.svg'
    assert provenance['source_sha256'] == hashlib.sha256((WEB / 'favicon.svg').read_bytes()).hexdigest()
    for item in provenance['files']:
        assert hashlib.sha256((WEB / 'icons' / item['path']).read_bytes()).hexdigest() == item['sha256']


def test_worker_has_root_scope_correct_mime_revalidation_and_content_version(environment):
    client, web = environment
    original = client.get('/service-worker.js')
    assert original.status_code == 200
    assert original.headers['service-worker-allowed'] == '/'
    assert original.headers['content-type'].startswith('text/javascript')
    assert 'must-revalidate' in original.headers['cache-control']
    assert '__RETROONE_PWA_VERSION__' not in original.text
    assert client.get('/service-worker.js').text == original.text
    (web / 'offline.html').write_text((web / 'offline.html').read_text() + '\n<!-- reviewed offline change -->')
    assert client.get('/service-worker.js').text != original.text


def test_private_data_cannot_change_pwa_version_or_be_exposed_as_resources(environment):
    client, web = environment
    original = client.get('/service-worker.js').text
    (web.parent / 'personal-save.state').write_bytes(b'private synthetic save')
    for route in ('/api/profiles', '/p/player1/api/session', '/p/player1/stream/', '/play/test', '/personal-save.state'):
        assert client.get(route).status_code == 404
    assert client.get('/service-worker.js').text == original


def test_offline_notice_is_honest_and_contains_no_player_or_game_data(environment):
    client, _ = environment
    response = client.get('/offline.html')
    assert response.status_code == 200
    assert 'Playing requires a connection to your game server' in response.text
    assert 'Games, player details and saves are not cached.' in response.text
    assert 'fetch(' not in response.text and 'localStorage' not in response.text
    assert 'no-cache' in response.headers['cache-control']


def test_missing_or_symlinked_resources_fail_closed(environment, tmp_path):
    client, web = environment
    secret = tmp_path / 'secret'; secret.write_text('private')
    (web / 'offline.html').unlink()
    (web / 'offline.html').symlink_to(secret)
    assert client.get('/offline.html').status_code == 404
    assert client.get('/service-worker.js').status_code == 404


def test_install_module_is_revalidated(environment):
    client, _ = environment
    response = client.get('/pwa-install.js')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/javascript')
    assert response.headers['cache-control'] == 'no-cache, max-age=0, must-revalidate'
