"""Protocol integration tests use the official SDK over actual HTTP requests.

Install requirements-mcp.txt plus pytest to run these optional sidecar tests.
The emulator backend's dependency set intentionally does not include MCP.
"""

from contextlib import asynccontextmanager
import base64
import json
import os
import socket
import sys
from types import ModuleType

import anyio
import pytest

pytest.importorskip("mcp", reason="Install requirements-mcp.txt for MCP transport tests")
httpx = pytest.importorskip("httpx2")

from mcp.client import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client

from retroone_mcp.server import TOOL_SPECS, create_app


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jKz0AAAAASUVORK5CYII="
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "retroone-test", "version": "1.0"},
}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeService:
    def __init__(self):
        self.calls = []
        self.tokens = {"controller-token": "controller", "watcher-token": "watcher"}
        self.authentications = []
        self.closed = False

    def authenticate(self, token):
        self.authentications.append(token)
        return self.tokens.get(token)

    def tool_names(self, principal):
        if principal == "controller":
            return set(TOOL_SPECS)
        return {"retroone_capabilities", "retroone_sessions", "retroone_watch", "retroone_observe"}

    async def call(self, principal, name, arguments):
        self.calls.append((principal, name, arguments))
        if name == "observe":
            return {"observation_id": "obs-1", "session_id": "s-1", "_image": {"data": PNG, "mimeType": "image/png"}}
        return {"ok": True, "tool": name, "arguments": arguments}

    async def close(self):
        self.closed = True


@asynccontextmanager
async def http_client(service, token="controller-token", **app_options):
    app = create_app(service, **app_options)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"},
        ) as client:
            yield client


@asynccontextmanager
async def mcp_client(service, token="controller-token", **app_options):
    async with http_client(service, token, **app_options) as http:
        async with streamable_http_client("http://localhost/mcp", http_client=http) as streams:
            async with ClientSession(*streams) as client:
                result = await client.initialize()
                assert result.server_info.name == "RetroOne"
                yield client, http


@pytest.mark.anyio
async def test_official_client_initialize_list_call_and_image():
    service = FakeService()
    async with mcp_client(service) as (client, _):
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {"retroone_" + name for name in TOOL_SPECS}
        for tool in tools:
            assert tool.input_schema["additionalProperties"] is False
        observe_tool = next(tool for tool in tools if tool.name == "retroone_observe")
        assert observe_tool.annotations.read_only_hint is True
        games = await client.call_tool("retroone_list_games", {"profile_id": "ai1"})
        assert games.is_error is False
        assert games.structured_content["arguments"]["limit"] == 20
        observed = await client.call_tool("retroone_observe", {"handle": "h-1"})
        assert observed.is_error is False
        assert observed.structured_content == {"observation_id": "obs-1", "session_id": "s-1"}
        assert [item.type for item in observed.content] == ["text", "image"]
        assert json.loads(observed.content[0].text) == observed.structured_content
        assert observed.content[1].mime_type == "image/png"
        assert base64.b64decode(observed.content[1].data).startswith(b"\x89PNG\r\n\x1a\n")
    assert service.closed
    assert all(principal == "controller" for principal, _, _ in service.calls)
    assert len(service.authentications) >= 5  # initialize, initialized, list, and both calls


@pytest.mark.anyio
async def test_official_modern_client_discovery_and_call():
    service = FakeService()
    async with http_client(service) as http:
        transport = streamable_http_client("http://localhost/mcp", http_client=http)
        async with Client(transport, mode="auto") as client:
            assert client.protocol_version == "2026-07-28"
            assert len((await client.list_tools()).tools) == 12
            assert (await client.call_tool("retroone_capabilities", {})).structured_content["ok"]


@pytest.mark.anyio
async def test_create_profile_protocol_defaults_strict_fields_and_observer_denial():
    service = FakeService()
    async with mcp_client(service) as (client, _):
        created = await client.call_tool('retroone_create_profile', {'name': 'My Agent', 'request_id': 'profile-1'})
        assert not created.is_error
        assert created.structured_content['arguments']['avatar'] == 'rocket'
        before = len(service.calls)
        for fields in ({'profile_id': 'player1'}, {'api_url': 'http://evil.invalid'}, {'avatar': 'invalid'}, {'name': 'x' * 21}):
            result = await client.call_tool('retroone_create_profile', {'name': 'Agent', 'request_id': 'new', **fields})
            assert result.is_error
        assert len(service.calls) == before
    async with mcp_client(FakeService(), token='watcher-token') as (client, _):
        assert 'retroone_create_profile' not in {tool.name for tool in (await client.list_tools()).tools}
        denied = await client.call_tool('retroone_create_profile', {'name': 'Agent', 'request_id': 'new'})
        assert denied.is_error


@pytest.mark.anyio
async def test_official_v1_client_over_tcp(tmp_path):
    """Opt-in separate-interpreter check: MCP_V1_CLIENT_PYTHON=/path/to/python."""
    python = os.environ.get("MCP_V1_CLIENT_PYTHON")
    if not python:
        pytest.skip("Set MCP_V1_CLIENT_PYTHON to an interpreter with mcp==1.30.0")
    import uvicorn

    script = tmp_path / "official_v1_client.py"
    script.write_text('''
import asyncio, sys
from importlib.metadata import version
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def run():
    assert version("mcp") == "1.30.0"
    async with streamablehttp_client(sys.argv[1], headers={"Authorization": "Bearer controller-token"}) as (read, write, _):
        async with ClientSession(read, write) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == "RetroOne"
            assert len((await client.list_tools()).tools) == 12
            result = await client.call_tool("retroone_observe", {"handle": "h-1"})
            assert result.isError is False
            assert result.content[1].type == "image"
            assert result.content[1].mimeType == "image/png"
            assert result.structuredContent["observation_id"] == "obs-1"
    print("official mcp 1.30.0 initialize/list/call/image succeeded")

asyncio.run(run())
''')
    service = FakeService()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=port, access_log=False, log_level="error"))

    async def serve():
        await server.serve(sockets=[listener])

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(serve)
            try:
                with anyio.fail_after(10):
                    while not server.started:
                        await anyio.sleep(0.01)
                with anyio.fail_after(20):
                    result = await anyio.run_process([python, str(script), f"http://127.0.0.1:{port}/mcp"])
                assert b"succeeded" in result.stdout
            finally:
                server.should_exit = True
    finally:
        listener.close()
    assert service.closed


@pytest.mark.anyio
async def test_tools_filtered_and_calls_authorized_per_principal():
    service = FakeService()
    async with mcp_client(service, "watcher-token") as (client, _):
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == service.tool_names("watcher")
        denied = await client.call_tool("retroone_stop", {"profile_id": "ai1", "expected_session_id": "s-1", "request_id": "r-1"})
        assert denied.is_error
        assert denied.structured_content["error"]["code"] == "tool_not_available"
        assert not service.calls


@pytest.mark.anyio
@pytest.mark.parametrize("arguments", [
    {"profile_id": "ai1", "limit": 101},
    {"profile_id": "ai1", "limit": "20"},
    {"profile_id": "ai1", "offset": -1},
    {"profile_id": "../ai1"},
    {"profile_id": "ai1", "extra": "not allowed"},
])
async def test_invalid_arguments_never_reach_service(arguments):
    service = FakeService()
    async with mcp_client(service) as (client, _):
        result = await client.call_tool("retroone_list_games", arguments)
        assert result.is_error
        assert result.structured_content["error"]["code"] == "invalid_arguments"
    assert not service.calls


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    {"duration_ms": 1001}, {"duration_ms": True}, {"buttons": ["quit"]},
    {"buttons": ["a", "a"]}, {"axes": {"lx": 1.01}}, {"axes": {"other": 0.0}},
])
async def test_controller_bounds(change):
    service = FakeService()
    args = {"lease_id": "l", "expected_session_id": "s", "action_id": "a", "observation_id": "o", **change}
    async with mcp_client(service) as (client, _):
        assert (await client.call_tool("retroone_act", args)).is_error
    assert not service.calls


@pytest.mark.anyio
async def test_bearer_required_for_handshake_and_every_later_request():
    service = FakeService()
    async with http_client(service) as client:
        for authorization in ["", "Bearer invalid", "Basic controller-token", "Bearer  controller-token"]:
            response = await client.post("/mcp", json=INIT, headers={"Authorization": authorization})
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
        assert (await client.post("/mcp", json=INIT)).status_code == 200
        service.tokens.pop("controller-token")
        for method in ["POST", "GET", "DELETE", "OPTIONS"]:
            response = await client.request(method, "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert response.status_code == 401
        health = await client.get("/healthz", headers={"Authorization": ""})
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "retroone-mcp"}
    assert not service.calls


@pytest.mark.anyio
async def test_no_query_tokens_and_exact_origin_host_checks():
    service = FakeService()
    async with http_client(service, allowed_origins=("https://viewer.example",)) as client:
        assert (await client.post("/mcp?token=controller-token", json=INIT)).status_code == 400
        for origin in ["https://evil.example", "null", "https://viewer.example.evil", "https://viewer.example/"]:
            assert (await client.post("/mcp", json=INIT, headers={"Origin": origin})).status_code == 403
        assert (await client.post("/mcp", json=INIT, headers={"Origin": "https://viewer.example"})).status_code == 200
        assert (await client.post("/mcp", json=INIT, headers={"Host": "attacker.example"})).status_code == 421
        assert (await client.post("/mcp", json=INIT, headers={"Host": "localhost:8090"})).status_code == 200


@pytest.mark.parametrize("options", [
    {"allowed_origins": ("*",)}, {"allowed_origins": ("https://*.example",)},
    {"allowed_origins": ("https://viewer.example/path",)}, {"allowed_hosts": ("*",)},
    {"allowed_hosts": ()},
])
def test_wildcard_or_invalid_transport_configuration_rejected(options):
    with pytest.raises(ValueError):
        create_app(FakeService(), **options)


@pytest.mark.anyio
async def test_malformed_and_oversized_http_requests_rejected():
    service = FakeService()
    async with http_client(service) as client:
        malformed = await client.post("/mcp", content="{", headers={"Content-Type": "application/json"})
        assert malformed.status_code == 400
        assert (await client.post("/mcp", content="x" * 70000, headers={"Content-Type": "application/json"})).status_code == 413
    assert not service.calls


@pytest.mark.anyio
async def test_caller_identity_cannot_be_injected_in_arguments():
    service = FakeService()
    async with mcp_client(service, "watcher-token") as (client, _):
        result = await client.call_tool("retroone_capabilities", {"principal": "controller"})
        assert result.is_error
        assert not service.calls


@pytest.mark.anyio
async def test_spectator_routes_have_an_exact_separate_boundary():
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def separate_auth(request):
        return JSONResponse({"spectator": True})

    routes = [Route(path, separate_auth) for path in ("/spectate", "/spectate/status", "/spectateevil")]
    async with http_client(FakeService(), extra_routes=routes) as client:
        headers = {"Authorization": ""}
        assert (await client.get("/spectate", headers=headers)).status_code == 200
        assert (await client.get("/spectate/status", headers=headers)).status_code == 200
        assert (await client.get("/spectateevil", headers=headers)).status_code == 401
        assert (await client.post("/mcp", json=INIT, headers=headers)).status_code == 401


@pytest.mark.anyio
async def test_safe_service_failures_and_unexpected_errors(monkeypatch):
    # Keep this a transport unit boundary; no runtime imports or live service.
    module = ModuleType("retroone_mcp.service")

    class ServiceFailure(Exception):
        def __init__(self, code, message):
            self.code, self.message = code, message

    module.ServiceFailure = ServiceFailure
    monkeypatch.setitem(sys.modules, "retroone_mcp.service", module)

    class FailingService(FakeService):
        async def call(self, principal, name, arguments):
            if name == "capabilities":
                raise ServiceFailure("stale_session", "Session has changed")
            raise RuntimeError("a-secret-that-must-never-be-returned")

    async with mcp_client(FailingService()) as (client, _):
        expected = await client.call_tool("retroone_capabilities", {})
        assert expected.is_error
        assert expected.structured_content["error"] == {"code": "stale_session", "message": "Session has changed"}
        unexpected = await client.call_tool("retroone_sessions", {})
        assert unexpected.is_error
        assert unexpected.structured_content["error"]["code"] == "internal_error"
        assert "a-secret" not in unexpected.model_dump_json()


@pytest.mark.anyio
async def test_authentication_backend_errors_fail_closed():
    class FailingService(FakeService):
        def authenticate(self, token):
            raise RuntimeError("private config contents")

    service = FailingService()
    async with http_client(service) as client:
        response = await client.post("/mcp", json=INIT)
        assert response.status_code == 503
        assert response.json() == {"error": "authentication_unavailable"}
    assert not service.calls
