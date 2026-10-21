"""Protocol-contract tests use a local HTTP/WS peer, never a player runtime."""

import asyncio
import io
import json
from contextlib import asynccontextmanager

import pytest
pytest.importorskip('aiohttp', reason='Install requirements-mcp.txt for runtime adapter tests')
pytest.importorskip('PIL', reason='Install requirements-mcp.txt for runtime adapter tests')
from aiohttp import web
from PIL import Image

from retroone_mcp.runtime import AXES, BUTTONS, RuntimeClient, RuntimeFailure


@asynccontextmanager
async def peer(**options):
    state = {"session": "session-a", "requests": [], "messages": [], "connections": 0,
             "closed": asyncio.Event(), "pressed": asyncio.Event(), **options}
    app = web.Application()

    async def status(request):
        state["requests"].append((request.method, request.path, dict(request.query), None))
        state["status_calls"] = state.get("status_calls", 0) + 1
        if state.get("status_delay_after") and state["status_calls"] >= state["status_delay_after"]:
            await asyncio.sleep(0.25)
        if state.get("status_error_after") and state["status_calls"] >= state["status_error_after"]:
            return web.Response(status=503, text="secret backend details")
        return web.json_response({"session": {"session_id": state["session"]} if state["session"] else None,
                                  "runtime": {"available": True}, "game": {"id": "fixture"}})

    async def generic(request):
        body = await request.json() if request.method == "POST" else None
        state["requests"].append((request.method, request.path, dict(request.query), body))
        if request.path.endswith("/launch") and state.get("launch_delay"):
            await asyncio.sleep(state["launch_delay"])
            state["session"] = "cold-start-session"
        if state.get("http_error"):
            return web.Response(status=state["http_error"], text="http://private/token=secret /private/config")
        return web.json_response({"ok": True, "games": []})

    async def screenshot(request):
        state["screenshot_auth"] = request.headers.get("Authorization")
        if state.get("invalid_image"):
            return web.Response(body=b"not a PNG")
        output = io.BytesIO()
        Image.new("RGB", (120, 80), (32, 64, 128)).save(output, format="PNG")
        return web.Response(body=output.getvalue(), content_type="image/png")

    async def controller(request):
        state["connections"] += 1
        state["ws_query"] = dict(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if not state.get("no_handshake"):
            if request.query.get("token"):
                await ws.send_str("AUTH_SUCCESS," + json.dumps({"role": "viewer", "slot": state.get("slot", 2)}))
            await ws.send_str("MODE websockets")
            await ws.send_json({"type": "server_settings", "settings": {
                "gamepad_enabled": {"value": state.get("gamepad_enabled", True)}}})
        try:
            async for message in ws:
                text = message.data
                state["messages"].append(text)
                if text.startswith("js,b,") and text.endswith(",1"):
                    state["pressed"].set()
                    if state.get("replace_after_press"):
                        state["session"] = "session-b"
                    if state.get("disconnect_after_press"):
                        await ws.close()
                    if state.get("revoke_slot_after_press"):
                        await ws.send_str('ROLE_UPDATE,{"role":"viewer","slot":3}')
        finally:
            state["closed"].set()
        return ws

    app.router.add_get("/profile/api/session", status)
    app.router.add_get("/profile/api/games", generic)
    app.router.add_get("/api/profiles", generic)
    app.router.add_post("/api/profiles", generic)
    app.router.add_post("/profile/api/games/{game}/launch", generic)
    app.router.add_post("/profile/api/session/stop", generic)
    app.router.add_get("/profile/stream/api/screenshot", screenshot)
    app.router.add_get("/profile/stream/api/websockets", controller)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    config = {"api_url": f"http://127.0.0.1:{port}/profile/",
              "selkies_url": f"http://127.0.0.1:{port}/profile/stream/",
              "selkies_tokens": {"2": "slot-two-secret"}}
    runtime = RuntimeClient({"ai": config}, timeout=state.get("timeout", 1))
    runtime._reconnect_interval = 0
    runtime._poll_interval = 0.01
    try:
        yield runtime, state, config
    finally:
        await runner.cleanup()


def run(coroutine):
    return asyncio.run(coroutine)


def test_http_routes_session_preconditions_and_query_encoding():
    async def check():
        async with peer() as (runtime, state, _):
            assert (await runtime.status("ai"))["session"]["session_id"] == "session-a"
            await runtime.games("ai", "A & B", "snes", 12, 3)
            await runtime.launch("ai", "fixture", "stream", "session-a")
            await runtime.stop("ai", "session-a")
            assert state["requests"][1][2] == {"q": "A & B", "platform": "snes", "limit": "12", "offset": "3"}
            assert state["requests"][2][3] == {"mode": "stream", "replace_session_id": "session-a"}
            assert state["requests"][3][3] == {"expected_session_id": "session-a"}
            with pytest.raises(RuntimeFailure, match="streaming"):
                await runtime.launch("ai", "fixture", "browser")
            with pytest.raises(RuntimeFailure, match="expected session"):
                await runtime.stop("ai", "")
    run(check())


def test_cold_launch_can_outlast_regular_read_timeout_without_retry():
    async def check():
        async with peer(timeout=0.03, launch_delay=0.08, status_delay_after=1) as (runtime, state, _):
            assert runtime.launch_timeout == 120.0
            assert (await runtime.launch("ai", "fixture", "stream"))["ok"]
            assert state["session"] == "cold-start-session"
            assert len([r for r in state["requests"] if r[1].endswith("/launch")]) == 1
            with pytest.raises(RuntimeFailure) as error:
                await runtime.status("ai")
            assert error.value.code == "runtime_timeout"  # Reads keep their short deadline.
    run(check())


def test_launch_timeout_explains_uncertain_outcome_and_does_not_replay():
    async def check():
        async with peer(launch_delay=0.08) as (runtime, state, _):
            runtime.launch_timeout = 0.02
            with pytest.raises(RuntimeFailure) as error:
                await runtime.launch("ai", "fixture", "stream")
            assert error.value.code == "runtime_timeout"
            assert "inspect sessions" in str(error.value)
            assert "may still be starting" in str(error.value)
            # The worker may finish after the client disconnects. No second
            # launch is sent; inspecting sessions reveals the actual outcome.
            await asyncio.sleep(0.1)
            assert (await runtime.status("ai"))["session"]["session_id"] == "cold-start-session"
            assert len([r for r in state["requests"] if r[1].endswith("/launch")]) == 1
    run(check())


@pytest.mark.parametrize("status,code", [(401, "runtime_auth_failed"), (403, "runtime_auth_failed"),
                                         (409, "session_conflict"), (503, "runtime_unavailable")])
def test_backend_error_bodies_are_never_exposed(status, code):
    async def check():
        async with peer(http_error=status) as (runtime, _, __):
            with pytest.raises(RuntimeFailure) as error:
                await runtime.games("ai")
            assert error.value.code == code
            assert not any(word in str(error.value) for word in ("secret", "private", "http://"))
    run(check())


def test_screenshot_uses_png_endpoint_and_resizes_without_upscaling():
    async def check():
        async with peer() as (runtime, state, config):
            data, mime, width, height = await runtime.screenshot("ai", 60)
            assert (mime, width, height) == ("image/png", 60, 40)
            assert Image.open(io.BytesIO(data)).size == (60, 40)
            assert state["screenshot_auth"] == "Bearer slot-two-secret"
            config.pop("selkies_tokens")  # Family legacy observation is read-only.
            assert (await runtime.screenshot("ai", 200))[2:] == (120, 80)
            assert state["screenshot_auth"] is None
            assert state["connections"] == 0
    run(check())


def test_invalid_screenshot_and_unknown_profile_fail_safely():
    async def check():
        async with peer(invalid_image=True) as (runtime, _, __):
            with pytest.raises(RuntimeFailure) as error:
                await runtime.screenshot("ai", 100)
            assert error.value.code == "runtime_response_invalid"
            with pytest.raises(RuntimeFailure) as error:
                await runtime.status("family-not-configured")
            assert error.value.code == "unknown_profile"
    run(check())


def test_secure_slot_input_uses_standard_protocol_and_always_releases():
    async def check():
        async with peer() as (runtime, state, _):
            result = await runtime.perform("ai", 2, ["a", "lt", "up"], {"lx": -0.75, "ry": 0.5}, 35, "session-a")
            # perform() confirms local submission, not completion of this peer's
            # receive loop. Its finally event marks the complete message list.
            await asyncio.wait_for(state["closed"].wait(), 0.2)
            assert state["ws_query"] == {"token": "slot-two-secret"}
            assert state["messages"][0].startswith("js,c,1,")
            assert state["messages"][0].endswith(",4,17")
            for text in ("js,b,1,0,1", "js,b,1,6,1", "js,b,1,12,1", "js,a,1,0,-0.75", "js,a,1,3,0.5"):
                assert text in state["messages"]
            assert state["messages"][-1] == "js,d,1"
            assert all(f"js,b,1,{index},0" in state["messages"] for index in BUTTONS.values())
            assert all(f"js,a,1,{index},0" in state["messages"] for index in AXES.values())
            assert result["delivery"] == "submitted" and result["submitted_messages"] == 5
            assert result["neutralization_submitted"] and not result["game_effect_confirmed"]
            assert all(text.startswith("js,") for text in state["messages"])
    run(check())


@pytest.mark.parametrize("options,code", [({"slot": 3}, "controller_slot_denied"),
                                          ({"gamepad_enabled": False}, "runtime_input_unavailable"),
                                          ({"no_handshake": True, "timeout": 0.04}, "runtime_timeout")])
def test_refused_handshake_never_associates_or_sends_input(options, code):
    async def check():
        async with peer(**options) as (runtime, state, _):
            with pytest.raises(RuntimeFailure) as error:
                await runtime.perform("ai", 2, ["a"], {}, 20, "session-a")
            assert error.value.code == code
            await asyncio.wait_for(state["closed"].wait(), 0.2)
            assert not state["messages"]
    run(check())


@pytest.mark.parametrize("change", ["session", "slot", "transport", "status_error"])
def test_mid_action_failure_stops_and_closes(change):
    async def check():
        options = {"session": {"replace_after_press": True},
                   "slot": {"revoke_slot_after_press": True},
                   "transport": {"disconnect_after_press": True},
                   "status_error": {"status_error_after": 3}}[change]
        async with peer(**options) as (runtime, state, _):
            with pytest.raises(RuntimeFailure) as error:
                await runtime.perform("ai", 2, ["a"], {}, 1000, "session-a")
            assert error.value.code == {"session": "session_conflict", "slot": "controller_slot_denied",
                                        "transport": "runtime_disconnected", "status_error": "runtime_unavailable"}[change]
            await asyncio.wait_for(state["closed"].wait(), 0.2)
            if change != "transport":
                assert state["messages"][-1] == "js,d,1"
                # Once the state changed, no second pressed-state injection occurs.
                assert state["messages"].count("js,b,1,0,1") == 1
    run(check())


def test_action_deadline_is_independent_of_a_slow_status_poll():
    async def check():
        async with peer(status_delay_after=3) as (runtime, state, _):
            result = await runtime.perform("ai", 2, ["a"], {}, 35, "session-a")
            assert result["elapsed_ms"] < 200
            await asyncio.wait_for(state["closed"].wait(), 0.2)
            assert state["messages"][-1] == "js,d,1"
    run(check())


def test_task_cancellation_releases_before_propagating():
    async def check():
        async with peer() as (runtime, state, _):
            task = asyncio.create_task(runtime.perform("ai", 2, ["a"], {"lx": 1}, 1000, "session-a"))
            await asyncio.wait_for(state["pressed"].wait(), 0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(state["closed"].wait(), 0.2)
            assert state["messages"][-1] == "js,d,1"
    run(check())


def test_session_mismatch_and_missing_token_make_no_controller_connection():
    async def check():
        async with peer() as (runtime, state, config):
            with pytest.raises(RuntimeFailure) as error:
                await runtime.perform("ai", 2, ["a"], {}, 20, "wrong-session")
            assert error.value.code == "session_conflict"
            config.pop("selkies_tokens")
            with pytest.raises(RuntimeFailure) as error:
                await runtime.perform("ai", 2, ["a"], {}, 20, "session-a")
            assert error.value.code == "runtime_configuration"
            assert state["connections"] == 0
    run(check())


@pytest.mark.parametrize("slot,buttons,axes,duration", [
    (0, ["a"], {}, 10), (True, ["a"], {}, 10), (2, ["shell"], {}, 10),
    (2, ["a", "a"], {}, 10), (2, ["a"], {"lx": float("nan")}, 10),
    (2, ["a"], {"lx": 10 ** 400}, 10), (2, ["a"], {"lx": True}, 10),
    (2, ["a"], {"keycode": 10}, 10), (2, ["a"], {}, 6000),
])
def test_invalid_controls_never_reach_runtime(slot, buttons, axes, duration):
    async def check():
        async with peer() as (runtime, state, _):
            with pytest.raises(RuntimeFailure) as error:
                await runtime.perform("ai", slot, buttons, axes, duration, "session-a")
            assert error.value.code == "invalid_argument"
            assert not state["requests"] and state["connections"] == 0
    run(check())


def test_profile_creation_adapter_sends_only_identity_and_stable_uuid():
    async def run():
        async with peer() as (runtime, state, _):
            from urllib.parse import urlsplit
            parsed = urlsplit(runtime.profiles['ai']['api_url'])
            gateway = f'{parsed.scheme}://{parsed.netloc}'
            await runtime.create_profile(gateway, 'Agent', 'cat', '12345678-1234-5678-1234-567812345678')
            await runtime.list_profiles(gateway)
            assert state['requests'] == [
                ('POST', '/api/profiles', {}, {'name': 'Agent', 'avatar': 'cat', 'request_id': '12345678-1234-5678-1234-567812345678'}),
                ('GET', '/api/profiles', {}, None),
            ]
    asyncio.run(run())
