"""Service policy tests with a fake runtime; these do not prove game behavior."""

import asyncio
import base64
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import time

import pytest

pytest.importorskip("mcp", reason="Install requirements-mcp.txt for optional MCP service tests")

from retroone_mcp.service import READ_TOOLS, Service, ServiceFailure


TOKENS = {name: (name + "-test-credential-").ljust(48, "x") for name in ("observer", "alice", "bob")}
SCREEN = b"fake screenshot bytes for the service boundary"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeRuntime:
    def __init__(self):
        self.sessions = {profile: self.session(profile, "session-" + profile) for profile in ("player1", "ai1", "ai2")}
        self.calls = []
        self.events = []
        self.screenshot_hook = None
        self.perform_started = asyncio.Event()
        self.perform_continue = None
        self.launch_started = asyncio.Event()
        self.launch_continue = None
        self.closed = False

    @staticmethod
    def session(profile, identifier, game="nes-fixture"):
        return {"id": identifier, "game_id": game, "title": "NES fixture", "platform": "nes", "mode": "stream",
                "profile_id": profile, "rom_path": "/private/roms/fixture.nes", "stream_url": "http://private-runtime.invalid/stream/",
                "token": "private-runtime-token"}

    async def status(self, profile):
        self.calls.append(("status", profile))
        return {"session": deepcopy(self.sessions[profile]), "api_url": "http://private-runtime.invalid/api",
                "runtime": {"platforms": {"nes": {"available": True, "engine": "retroarch", "reason": "Installed",
                                                    "binary": "/private/bin/retroarch", "token": "private-runtime-token"}}}}

    async def games(self, profile, **arguments):
        self.calls.append(("games", profile, arguments))
        return {"games": [{"id": "nes-fixture", "title": "NES fixture", "platform": "nes", "mode": "browser",
                           "rom_path": "/private/roms/fixture.nes", "download_url": "http://private-runtime.invalid/rom"}], "total": 1}

    async def screenshot(self, profile, max_width):
        self.calls.append(("screenshot", profile, max_width))
        if self.screenshot_hook:
            await self.screenshot_hook()
        return SCREEN, "image/png", min(320, max_width), 240

    async def launch(self, profile, game, mode, replace):
        self.calls.append(("launch", profile, game, mode, replace))
        self.launch_started.set()
        if self.launch_continue:
            await self.launch_continue.wait()
        self.sessions[profile] = self.session(profile, "launched-" + profile, game)
        return {"session": deepcopy(self.sessions[profile])}

    async def perform(self, profile, slot, buttons, axes, duration, expected):
        self.calls.append(("perform", profile, slot, buttons, axes, duration, expected))
        self.events.append("act-start")
        self.perform_started.set()
        if self.perform_continue:
            await self.perform_continue.wait()
        self.events.append("act-end")
        return {"input_released": True}

    async def stop(self, profile, expected):
        self.calls.append(("stop", profile, expected))
        self.events.append("stop")
        self.sessions[profile] = None
        return {"stopped": True}

    async def close(self):
        self.closed = True


@dataclass
class Rig:
    config_path: object
    state_path: object
    config: dict
    runtime: FakeRuntime
    service: Service
    opened: list = field(default_factory=list)

    def write(self):
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)

    def principal(self, name="alice"):
        result = self.service.authenticate(TOKENS[name])
        assert result is not None
        return result

    async def restart(self):
        old = self.service
        await old.close()
        self.opened.remove(old)
        runtime = FakeRuntime()
        runtime.sessions = deepcopy(self.runtime.sessions)
        self.runtime = runtime
        self.service = Service(self.config_path, self.state_path, runtime)
        self.opened.append(self.service)


@pytest.fixture
async def rig(tmp_path):
    profiles = {profile: {
        "api_url": "http://private-runtime.invalid/" + profile + "/api",
        "selkies_url": "http://private-runtime.invalid/" + profile + "/stream",
        "name": profile, "control_enabled": profile != "player1",
        "selkies_tokens": {"1": "private-slot-one-token", "2": "private-slot-two-token"},
        "selkies_viewer_token": "private-viewer-token",
    } for profile in ("player1", "ai1", "ai2")}
    credentials = []
    for name in TOKENS:
        profiles_allowed = ["player1"] if name == "observer" else ["ai1"]
        credential = {"id": name, "token_sha256": hashlib.sha256(TOKENS[name].encode()).hexdigest(),
                      "observe_profiles": profiles_allowed}
        if name != "observer":
            credential.update({key + "_profiles": ["ai1"] for key in ("control", "launch", "stop")})
            credential['slots'] = [1, 2] if name == 'bob' else [1]
        credentials.append(credential)
    config = {"profiles": profiles, "credentials": credentials,
              "family_watch": {"enabled": True, "profiles": ["player1", "ai1"]}}
    path, state = tmp_path / "private-config.json", tmp_path / "state" / "requests.sqlite"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    runtime = FakeRuntime()
    service = Service(path, state, runtime)
    result = Rig(path, state, config, runtime, service, [service])
    try:
        yield result
    finally:
        for opened in result.opened:
            await opened.close()


async def rejected(code, operation):
    with pytest.raises(ServiceFailure) as caught:
        await operation
    assert caught.value.code == code


async def controls(rig, name="alice", slot=1):
    actor = rig.principal(name)
    expected = rig.runtime.sessions["ai1"]["id"]
    watched = await rig.service.call(actor, "watch", {"profile_id": "ai1", "expected_session_id": expected})
    observed = await rig.service.call(actor, "observe", {"handle": watched["handle"]})
    lease = await rig.service.call(actor, "claim_control", {"profile_id": "ai1", "expected_session_id": expected, "slot": slot})
    return actor, watched, observed, lease


def action(observation, lease, identifier="action-one"):
    return {"lease_id": lease["lease_id"], "expected_session_id": lease["session_id"], "action_id": identifier,
            "observation_id": observation["observation_id"], "buttons": ["a"], "axes": {}, "duration_ms": 100}


@pytest.mark.anyio
async def test_observer_tools_and_scoped_read_results_do_not_leak_runtime_configuration(rig):
    observer = rig.principal("observer")
    assert rig.service.tool_names(observer) == READ_TOOLS
    capabilities = await rig.service.call(observer, "capabilities", {})
    assert [profile["id"] for profile in capabilities["profiles"]] == ["player1"]
    assert capabilities["profiles"][0]["permissions"] == ["observe"]
    sessions = await rig.service.call(observer, "sessions", {})
    assert [entry["profile_id"] for entry in sessions["sessions"]] == ["player1"]
    games = await rig.service.call(observer, "list_games", {"profile_id": "player1"})
    rendered = json.dumps([capabilities, sessions, games])
    for private in ("private-runtime.invalid", "/private/", "private-runtime-token", "private-slot", "token_sha256", "api_url", "selkies_url", "rom_path"):
        assert private not in rendered
    assert {call[1] for call in rig.runtime.calls} == {"player1"}
    await rejected("forbidden", rig.service.call(observer, "list_games", {"profile_id": "ai1"}))


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["launch", "claim_control", "act", "release_control", "stop"])
async def test_observer_cannot_call_mutations_even_with_direct_service_access(rig, name):
    await rejected("forbidden", rig.service.call(rig.principal("observer"), name, {}))
    assert not rig.runtime.calls


@pytest.mark.anyio
async def test_token_revocation_and_scope_changes_apply_to_existing_principals(rig):
    actor = rig.principal()
    rig.config["credentials"][1]["control_profiles"] = []
    rig.write()
    assert "act" not in rig.service.tool_names(actor)
    await rejected("forbidden", rig.service.call(actor, "claim_control", {}))
    rig.config["credentials"][1]["enabled"] = False
    rig.write()
    assert rig.service.authenticate(TOKENS["alice"]) is None
    await rejected("unauthorized", rig.service.call(actor, "capabilities", {}))
    assert not rig.runtime.calls


@pytest.mark.anyio
async def test_expired_and_malformed_credentials_fail_closed(rig):
    rig.config["credentials"][1]["expires_at"] = time.time() - 1
    rig.write()
    assert rig.service.authenticate(TOKENS["alice"]) is None
    assert rig.service.authenticate("short") is None
    rig.config_path.write_text("{")
    assert rig.service.authenticate(TOKENS["bob"]) is None


@pytest.mark.anyio
@pytest.mark.parametrize("name,arguments", [
    ("watch", {}), ("claim_control", {}), ("stop", {"request_id": "stop-stale"}),
])
async def test_stale_expected_session_is_rejected_before_mutation(rig, name, arguments):
    await rejected("session_changed", rig.service.call(rig.principal(), name, {
        "profile_id": "ai1", "expected_session_id": "old-session", **arguments,
    }))
    assert not [call for call in rig.runtime.calls if call[0] in {"stop", "launch", "perform"}]


@pytest.mark.anyio
async def test_capture_checks_session_both_before_and_after_runtime_image(rig):
    actor = rig.principal()
    grant = await rig.service.call(actor, "watch", {"profile_id": "ai1", "expected_session_id": "session-ai1"})
    rig.runtime.sessions["ai1"]["id"] = "replacement"
    await rejected("session_changed", rig.service.call(actor, "observe", {"handle": grant["handle"]}))
    assert not [call for call in rig.runtime.calls if call[0] == "screenshot"]
    rig.runtime.sessions["ai1"]["id"] = "session-ai1"

    async def replace_during_capture():
        rig.runtime.sessions["ai1"]["id"] = "replacement"

    rig.runtime.screenshot_hook = replace_during_capture
    await rejected("session_changed", rig.service.call(actor, "observe", {"handle": grant["handle"]}))
    assert len([call for call in rig.runtime.calls if call[0] == "screenshot"]) == 1
    assert not rig.service.observations


@pytest.mark.anyio
async def test_capture_rechecks_token_after_runtime_image(rig):
    actor = rig.principal()
    grant = await rig.service.call(actor, "watch", {"profile_id": "ai1", "expected_session_id": "session-ai1"})

    async def revoke_during_capture():
        rig.config["credentials"][1]["enabled"] = False
        rig.write()

    rig.runtime.screenshot_hook = revoke_during_capture
    await rejected("unauthorized", rig.service.call(actor, "observe", {"handle": grant["handle"]}))
    assert not rig.service.observations


@pytest.mark.anyio
async def test_handles_leases_and_observations_cannot_cross_principals(rig):
    alice, watch, observed, lease = await controls(rig)
    bob = rig.principal("bob")
    await rejected("invalid_handle", rig.service.call(bob, "observe", {"handle": watch["handle"]}))
    await rejected("invalid_lease", rig.service.call(bob, "act", action(observed, lease)))
    await rejected("invalid_lease", rig.service.call(bob, "release_control", {"lease_id": lease["lease_id"]}))
    await rejected("slot_busy", rig.service.call(bob, "claim_control", {"profile_id": "ai1", "expected_session_id": "session-ai1", "slot": 1}))
    _, _, _, bob_lease = await controls(rig, "bob", slot=2)
    await rejected("stale_observation", rig.service.call(bob, "act", action(observed, bob_lease)))
    assert not [call for call in rig.runtime.calls if call[0] == "perform"]


@pytest.mark.anyio
async def test_actions_require_fresh_observation_and_after_action_binding(rig):
    actor, watched, observation, lease = await controls(rig)
    missing = dict(observation, observation_id="missing")
    await rejected("stale_observation", rig.service.call(actor, "act", action(missing, lease, "missing-image")))
    first = await rig.service.call(actor, "act", action(observation, lease))
    assert first["sequence"] == 1
    assert first["delivery"] == "submitted"
    assert first["game_effect"] == "verify_with_next_observation"
    await rejected("stale_observation", rig.service.call(actor, "act", action(observation, lease, "old-image")))
    await rejected("action_not_completed", rig.service.call(actor, "observe", {"handle": watched["handle"], "after_action_id": "unknown"}))
    next_image = await rig.service.call(actor, "observe", {"handle": watched["handle"], "after_action_id": "action-one"})
    assert base64.b64decode(next_image["_image"]["data"]) == SCREEN
    assert next_image["last_action_sequence"] == 1
    assert (await rig.service.call(actor, "act", action(next_image, lease, "action-two")))["sequence"] == 2
    assert len([call for call in rig.runtime.calls if call[0] == "perform"]) == 2


@pytest.mark.anyio
async def test_expired_observation_is_not_accepted(rig):
    actor, _, observation, lease = await controls(rig)
    rig.service.observations[observation["observation_id"]]["expires_at"] = time.time() - 1
    await rejected("stale_observation", rig.service.call(actor, "act", action(observation, lease)))
    assert not [call for call in rig.runtime.calls if call[0] == "perform"]


@pytest.mark.anyio
async def test_action_retries_replay_outcome_and_divergent_request_is_denied(rig):
    actor, _, observation, lease = await controls(rig)
    arguments = action(observation, lease)
    first = await rig.service.call(actor, "act", arguments)
    retry = await rig.service.call(actor, "act", arguments)
    assert retry == dict(first, replayed=True)
    await rejected("request_reused", rig.service.call(actor, "act", dict(arguments, buttons=["b"])))
    assert len([call for call in rig.runtime.calls if call[0] == "perform"]) == 1


@pytest.mark.anyio
async def test_action_cannot_target_a_replaced_session(rig):
    actor, _, observed, lease = await controls(rig)
    rig.runtime.sessions["ai1"]["id"] = "replacement"
    await rejected("session_changed", rig.service.call(actor, "act", action(observed, lease)))
    assert not [call for call in rig.runtime.calls if call[0] == "perform"]


@pytest.mark.anyio
async def test_replacement_during_action_is_not_reported_as_a_completed_effect(rig):
    actor, watched, observed, lease = await controls(rig)
    rig.runtime.perform_continue = asyncio.Event()
    arguments = action(observed, lease)
    running = asyncio.create_task(rig.service.call(actor, "act", arguments))
    try:
        await asyncio.wait_for(rig.runtime.perform_started.wait(), 1)
        rig.runtime.sessions["ai1"]["id"] = "replacement"
    finally:
        rig.runtime.perform_continue.set()
    await rejected("session_changed", running)
    await rejected("session_changed", rig.service.call(actor, "act", arguments))
    await rejected("action_not_completed", rig.service.call(actor, "observe", {
        "handle": watched["handle"], "after_action_id": arguments["action_id"],
    }))
    assert len([call for call in rig.runtime.calls if call[0] == "perform"]) == 1


@pytest.mark.anyio
async def test_profile_control_opt_in_is_required_even_when_token_has_permission(rig):
    credential = rig.config["credentials"][1]
    for name in ("observe", "control", "launch", "stop"):
        credential[name + "_profiles"].append("player1")
    rig.write()
    await rejected("control_disabled", rig.service.call(rig.principal(), "launch", {
        "profile_id": "player1", "game_id": "nes-fixture", "request_id": "protected-family-profile",
    }))
    assert not rig.runtime.calls


@pytest.mark.anyio
async def test_concurrent_retry_never_submits_the_same_action_twice(rig):
    actor, _, observation, lease = await controls(rig)
    rig.runtime.perform_continue = asyncio.Event()
    arguments = action(observation, lease)
    first = asyncio.create_task(rig.service.call(actor, "act", arguments))
    try:
        await asyncio.wait_for(rig.runtime.perform_started.wait(), 1)
        await rejected("outcome_unknown", rig.service.call(actor, "act", arguments))
    finally:
        rig.runtime.perform_continue.set()
        await first
    assert (await rig.service.call(actor, "act", arguments))["replayed"]
    assert len([call for call in rig.runtime.calls if call[0] == "perform"]) == 1


@pytest.mark.anyio
async def test_nes_launch_defaults_to_stream_without_config_writes(rig):
    rig.runtime.sessions["ai1"] = None
    before = rig.config_path.read_bytes()
    actor = rig.principal()
    arguments = {"profile_id": "ai1", "game_id": "nes-fixture", "request_id": "launch-nes"}
    result = await rig.service.call(actor, "launch", arguments)
    assert result["mode"] == result["session"]["mode"] == "stream"
    assert [call for call in rig.runtime.calls if call[0] == "launch"] == [("launch", "ai1", "nes-fixture", "stream", None)]
    assert rig.config_path.read_bytes() == before
    assert (await rig.service.call(actor, "launch", arguments))["replayed"]
    await rejected("browser_not_observable", rig.service.call(actor, "launch", dict(arguments, request_id="browser-launch", mode="browser")))
    assert len([call for call in rig.runtime.calls if call[0] == "launch"]) == 1


@pytest.mark.anyio
async def test_replacement_requires_exact_current_session(rig):
    actor = rig.principal()
    arguments = {"profile_id": "ai1", "game_id": "another-game", "request_id": "not-authorized"}
    await rejected("session_busy", rig.service.call(actor, "launch", arguments))
    await rejected("session_busy", rig.service.call(actor, "launch", dict(arguments, request_id="wrong-id", replace_session_id="old-session")))
    result = await rig.service.call(actor, "launch", dict(arguments, request_id="authorized", replace_session_id="session-ai1"))
    assert result["session"]["game_id"] == "another-game"
    assert len([call for call in rig.runtime.calls if call[0] == "launch"]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["launch", "stop"])
async def test_completed_mutation_ledger_survives_restart_without_reexecution(rig, kind):
    arguments = {"profile_id": "ai1", "request_id": "persistent-request"}
    if kind == "launch":
        rig.runtime.sessions["ai1"] = None
        arguments["game_id"] = "nes-fixture"
    else:
        arguments["expected_session_id"] = "session-ai1"
    first = await rig.service.call(rig.principal(), kind, arguments)
    await rig.restart()
    retry = await rig.service.call(rig.principal(), kind, arguments)
    assert retry == dict(first, replayed=True)
    assert not [call for call in rig.runtime.calls if call[0] == kind]


@pytest.mark.anyio
async def test_interrupted_mutation_is_not_reexecuted_after_restart(rig):
    rig.runtime.sessions["ai1"] = None
    rig.runtime.launch_continue = asyncio.Event()
    arguments = {"profile_id": "ai1", "game_id": "nes-fixture", "request_id": "interrupted"}
    pending = asyncio.create_task(rig.service.call(rig.principal(), "launch", arguments))
    await asyncio.wait_for(rig.runtime.launch_started.wait(), 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await rig.restart()
    await rejected("outcome_unknown", rig.service.call(rig.principal(), "launch", arguments))
    assert not [call for call in rig.runtime.calls if call[0] == "launch"]


@pytest.mark.anyio
async def test_failed_mutation_outcome_is_preserved_across_restart(rig):
    arguments = {"profile_id": "ai1", "expected_session_id": "old-session", "request_id": "failed-stop"}
    await rejected("session_changed", rig.service.call(rig.principal(), "stop", arguments))
    await rig.restart()
    rig.runtime.sessions["ai1"]["id"] = "old-session"
    await rejected("session_changed", rig.service.call(rig.principal(), "stop", arguments))
    assert not [call for call in rig.runtime.calls if call[0] == "stop"]


@pytest.mark.anyio
async def test_action_retry_after_restart_cannot_reuse_an_ephemeral_lease(rig):
    actor, _, observed, lease = await controls(rig)
    arguments = action(observed, lease)
    await rig.service.call(actor, "act", arguments)
    await rig.restart()
    await rejected("invalid_lease", rig.service.call(rig.principal(), "act", arguments))
    assert not [call for call in rig.runtime.calls if call[0] == "perform"]


@pytest.mark.anyio
@pytest.mark.parametrize("following", ["release_control", "stop"])
async def test_release_and_stop_wait_for_active_input_to_finish(rig, following):
    actor, _, observed, lease = await controls(rig)
    rig.runtime.perform_continue = asyncio.Event()
    running = asyncio.create_task(rig.service.call(actor, "act", action(observed, lease)))
    next_task = None
    try:
        await asyncio.wait_for(rig.runtime.perform_started.wait(), 1)
        arguments = {"lease_id": lease["lease_id"]} if following == "release_control" else {
            "profile_id": "ai1", "expected_session_id": "session-ai1", "request_id": "stop-after-act",
        }
        next_task = asyncio.create_task(rig.service.call(actor, following, arguments))
        await asyncio.sleep(0.01)
        assert not next_task.done()
        assert lease["lease_id"] in rig.service.leases
        assert rig.runtime.events == ["act-start"]
    finally:
        rig.runtime.perform_continue.set()
        await running
        if next_task:
            await next_task
    assert lease["lease_id"] not in rig.service.leases
    assert rig.runtime.events == (["act-start", "act-end", "stop"] if following == "stop" else ["act-start", "act-end"])


@pytest.mark.anyio
async def test_family_grants_are_explicit_scoped_and_separate_from_mcp_handles(rig):
    family = await rig.service.family_sessions()
    assert [session["profile_id"] for session in family["sessions"]] == ["player1", "ai1"]
    assert "private-runtime.invalid" not in json.dumps(family)
    await rejected("forbidden", rig.service.mint_family_watch("ai2", "ai1", "session-ai1"))
    await rejected("forbidden", rig.service.mint_family_watch("player1", "ai2", "session-ai2"))
    await rejected("session_changed", rig.service.mint_family_watch("player1", "ai1", "old-session"))
    minted = await rig.service.mint_family_watch("player1", "ai1", "session-ai1")
    assert minted["watch_url"].startswith("/spectate/")
    assert "private-" not in json.dumps(minted)
    handle = minted["watch_url"].split("/")[2]
    binding = await rig.service.resolve_watch(handle)
    assert binding["session_id"] == "session-ai1"
    assert binding["selkies_token"] == "private-viewer-token"  # Internal proxy binding only.
    actor = rig.principal()
    await rejected("invalid_handle", rig.service.call(actor, "observe", {"handle": handle}))
    mcp_handle = (await rig.service.call(actor, "watch", {"profile_id": "ai1", "expected_session_id": "session-ai1"}))["handle"]
    await rejected("invalid_handle", rig.service.resolve_watch(mcp_handle))
    rig.runtime.sessions["ai1"]["id"] = "replacement"
    await rejected("session_changed", rig.service.resolve_watch(handle))
    rig.config["family_watch"]["enabled"] = False
    rig.write()
    await rejected("spectating_disabled", rig.service.family_sessions())
    await rejected("spectating_disabled", rig.service.resolve_watch(handle))


@pytest.mark.anyio
async def test_family_grants_expire_and_target_scope_removal_revokes_them(rig):
    minted = await rig.service.mint_family_watch("player1", "ai1", "session-ai1")
    handle = minted["watch_url"].split("/")[2]
    rig.config["family_watch"]["profiles"] = ["player1"]
    rig.write()
    await rejected("invalid_handle", rig.service.resolve_watch(handle))
    rig.config["family_watch"]["profiles"] = ["player1", "ai1"]
    rig.write()
    rig.service.handles[handle]["expires_at"] = time.time() - 1
    await rejected("invalid_handle", rig.service.resolve_watch(handle))


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['disconnect', 'cancel'])
async def test_uncertain_input_consumes_old_observation(rig, failure):
    actor, watch, observed, lease = await controls(rig)
    async def uncertain(*args, **kwargs):
        if failure == 'cancel':
            raise asyncio.CancelledError()
        raise RuntimeFailure('runtime_disconnected', 'Disconnected after input submission.')
    rig.runtime.perform = uncertain
    first = action(observed, lease)
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else ServiceFailure):
        await rig.service.call(actor, 'act', first)
    await rejected('stale_observation', rig.service.call(actor, 'act', dict(first, action_id='new-after-uncertain')))
    fresh = await rig.service.call(actor, 'observe', {'handle': watch['handle']})
    assert fresh['last_action_sequence'] == 1


@pytest.mark.anyio
async def test_controller_slot_scope_is_enforced_and_revoked(rig):
    actor, _, observed, lease = await controls(rig)
    await rejected('forbidden', rig.service.call(actor, 'claim_control', {
        'profile_id': 'ai1', 'expected_session_id': 'session-ai1', 'slot': 2}))
    next(c for c in rig.config['credentials'] if c['id'] == 'alice')['slots'] = []
    rig.write()
    await rejected('forbidden', rig.service.call(actor, 'act', action(observed, lease)))
