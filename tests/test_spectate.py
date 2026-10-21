"""The family gateway cannot acquire any of the controller's authorities."""
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip('websockets.asyncio.client')
pytest.importorskip('httpx')
pytest.importorskip('starlette')
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from retroone_mcp import spectate

GRANT = 'w_' + 'abcXYZ09' * 5
BASE = f'/spectate/{GRANT}'
ORIGIN = {'Origin': 'http://testserver'}


class Service:
    def __init__(self):
        self.active = True
        self.minted = []
        self.resolves = 0

    async def family_sessions(self):
        return {'sessions': [{'profile_id': 'player1', 'session': {'id': 'session-A', 'game_title': 'PSP title'}}]}

    async def mint_family_watch(self, **data):
        self.minted.append(data)
        return {'watch_url': f'{BASE}/'}

    async def resolve_watch(self, grant):
        self.resolves += 1
        if grant != GRANT or not self.active:
            raise ValueError('Expired')
        return {'profile_id': 'player1', 'session_id': 'session-A', 'profile_name': '<Player>',
                'title': 'PSP title', 'stream_base_url': 'http://worker:3000/p/player1/stream/'}


@pytest.fixture
def setup():
    service = Service()
    app = Starlette(routes=spectate.spectate_routes(service, allowed_hosts=('testserver',)))
    with TestClient(app) as client:
        yield service, client


def test_family_session_and_mint_boundaries(setup):
    service, client = setup
    assert client.get('/spectate/api/sessions').json()['sessions'][0]['session']['id'] == 'session-A'
    data = {'viewer_profile_id': 'player2', 'target_profile_id': 'player1', 'expected_session_id': 'session-A'}
    assert client.post('/spectate/api/watch', json=data).status_code == 403
    assert client.post('/spectate/api/watch', json=data, headers={'Origin': 'http://evil.test'}).status_code == 403
    assert client.post('/spectate/api/watch', json={**data, 'role': 'controller'}, headers=ORIGIN).status_code == 400
    assert client.post('/spectate/api/watch?role=viewer', json=data, headers=ORIGIN).status_code == 400
    assert client.post('/spectate/api/watch', json=data, headers=ORIGIN).json()['watch_url'] == BASE + '/'
    assert service.minted == [data]
    assert client.get('/spectate/api/sessions', headers={'Host': 'evil.test'}).status_code == 421
    assert client.get('/spectate/api/sessions', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.post('/spectate/api/watch', content=b'x' * 2049,
                       headers={**ORIGIN, 'Content-Type': 'application/json'}).status_code == 413


def test_public_assets_only_and_no_controller_adapter(setup, monkeypatch):
    _, client = setup
    requested = []

    class UpstreamHTTP:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            requested.append(url)
            return spectate.httpx.Response(200, text='<html><head><script src="./retroone-input.js?v=1"></script><script type="module" src="./assets/index-abc.js"></script></head><body></body></html>')

    monkeypatch.setattr(spectate.httpx, 'AsyncClient', UpstreamHTTP)
    page = client.get(BASE + '/stream/?role=controller&slot=1&token=caller')
    assert page.status_code == 200
    assert 'retroone-input.js' not in page.text
    assert "location.pathname + '#shared'" in page.text
    assert './assets/index-abc.js' in page.text
    assert requested == ['http://worker:3000/p/player1/stream/index.html']
    assert client.get(BASE + '/stream/api/status').json() == {'current_mode': 'websockets', 'enable_dual_mode': False}
    for path in ['api/switch', 'api/files/', 'api/command', 'api/turn', 'api/tokens', 'src/selkies-core.js', 'retroone-input.js', 'api/session/stop']:
        assert client.get(BASE + '/stream/' + path).status_code == 403
        assert client.post(BASE + '/stream/' + path).status_code == 405
    assert len(requested) == 1
    assert '&lt;Player&gt;' in client.get(BASE + '/').text
    assert 'Permissions-Policy' in page.headers
    assert page.headers['referrer-policy'] == 'no-referrer'


def fake_upstream(monkeypatch, received, urls):
    class Upstream:
        def __init__(self): self.queue = asyncio.Queue()
        def __aiter__(self): return self
        async def __anext__(self): return await self.queue.get()
        async def send(self, value):
            received.append(value)
            self.queue.put_nowait(b'encoded-video-and-audio')

    @asynccontextmanager
    async def connection(url, **kwargs):
        urls.append(url)
        assert kwargs['compression'] is None
        yield Upstream()

    monkeypatch.setattr(spectate, 'connect', connection)


def test_ws_caller_cannot_change_role_backend_or_send_inputs(setup, monkeypatch):
    service, client = setup
    received, urls = [], []
    fake_upstream(monkeypatch, received, urls)
    with client.websocket_connect(BASE + '/stream/api/websockets?role=controller&slot=4&token=bad&mk=1', headers=ORIGIN) as ws:
        for value in ['kd,65', 'ku,65', 'js,a,1', 'mouse,1', 'SETTINGS,{"displayId":"display2"}', 'STOP_AUDIO', 'START_VIDEO\nkd,65', '_gz,1', 'clipboard,secret']:
            ws.send_text(value)
        ws.send_bytes(b'\x05compressed-input')
        ws.send_bytes(b'\x02microphone')
        ws.send_text('START_VIDEO')
        assert ws.receive_bytes() == b'encoded-video-and-audio'
        assert received == ['START_VIDEO']
        parsed = urlsplit(urls[0])
        assert parsed.netloc == 'worker:3000'
        assert parsed.path == '/p/player1/stream/api/websockets'
        assert parse_qs(parsed.query) == {'role': ['viewer']}
        assert service.resolves >= 2
    assert 'STOP_AUDIO' not in received


def test_session_change_terminates_live_view(setup, monkeypatch):
    service, client = setup
    received, urls = [], []
    fake_upstream(monkeypatch, received, urls)
    with client.websocket_connect(BASE + '/stream/api/websockets', headers=ORIGIN) as ws:
        ws.send_text('START_VIDEO')
        assert ws.receive_bytes()
        service.active = False
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert client.get(BASE + '/').status_code == 410
    assert client.get(BASE + '/stream/api/status').status_code == 410


def test_unknown_grant_and_missing_ws_origin_fail_closed(setup):
    _, client = setup
    assert client.get('/spectate/not-a-grant/').status_code == 410
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(BASE + '/stream/api/websockets'):
            pass


@pytest.mark.parametrize('message', ['STOP_AUDIO', 'SETTINGS,{}', b'START_VIDEO', {'role': 'controller'}, 'START_VIDEO ', 'START_VIDEO\x00', '_gz,1'])
def test_input_filter_is_an_exact_string_allowlist(message):
    assert spectate.viewer_message(message) is None
