import copy
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


spec = importlib.util.spec_from_file_location("create_mcp_runtime", Path(__file__).parents[1] / "scripts/create_mcp_runtime.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def fixture():
    environment = {"RETROONE_HOST_PROFILES_ROOT": "/profiles", "RETROONE_HOST_ROM_ROOT": "/roms",
                   "RETROONE_DOCKER_NETWORK": "retroone_default"}
    entry = {"profile_id": "player5", "name": "AI A", "agent_id": "agent-a", "master_token": "master-secret",
             "viewer_token": "viewer-secret", "slot_tokens": {str(n): "slot-secret-%d" % n for n in range(1, 5)},
             "client_token": "client-secret"}
    state = {"owner": "our-owner", "image_id": "sha256:reviewed-image", "baseline_profiles": ["player1", "player2", "player3", "player4"],
             "entries": [entry]}
    inspect = {"Id": "new-container", "Image": "sha256:original", "State": {"Running": True},
               "Config": {"Env": ["PUID=1026"], "Labels": {"io.retroone.managed": "profile", "io.retroone.profile": "player5"}},
               "HostConfig": {"Binds": ["/profiles/player5:/config:rw", "/roms:/roms:ro"]},
               "Mounts": [{"Destination": "/config", "Source": "/profiles/player5", "RW": True},
                          {"Destination": "/roms", "Source": "/roms", "RW": False}],
               "NetworkSettings": {"Networks": {"retroone_default": {}}}}
    return environment, entry, state, inspect


@pytest.mark.parametrize("tamper", ["family", "owner", "profile", "mount", "writable_roms", "network"])
def test_ownership_validation_refuses_existing_family_or_changed_container(tamper):
    environment, entry, state, inspect = fixture()
    if tamper == "family":
        state["baseline_profiles"].append("player5")
    elif tamper == "owner":
        inspect["Config"]["Labels"][deploy.OWNER_LABEL] = "another-owner"
    elif tamper == "profile":
        inspect["Config"]["Labels"][deploy.PROFILE_LABEL] = "player1"
    elif tamper == "mount":
        inspect["Mounts"][0]["Source"] = "/profiles/player1"
    elif tamper == "writable_roms":
        inspect["Mounts"][1]["RW"] = True
    else:
        inspect["NetworkSettings"]["Networks"] = {"other": {}}
    with pytest.raises(deploy.DeployError):
        deploy.validate_new_worker(inspect, entry, state, environment)


def test_secure_spec_preserves_data_and_only_adds_owned_service(tmp_path):
    environment, entry, state, inspect = fixture()
    deploy.validate_new_worker(inspect, entry, state, environment)
    original = deploy.original_spec(inspect, environment["RETROONE_DOCKER_NETWORK"], "retroone-player5")
    saved = copy.deepcopy(original)
    binds = deploy.install_runtime_files(tmp_path, entry)
    secure = deploy.secure_spec(original, entry, state, binds)
    assert original == saved
    assert secure["Image"] == state["image_id"]
    assert secure["Labels"][deploy.OWNER_LABEL] == "our-owner"
    assert secure["HostConfig"]["Binds"][:2] == original["HostConfig"]["Binds"]
    assert len(secure["HostConfig"]["Binds"]) == 5
    private = tmp_path / "private/player5-selkies.json"
    assert private.stat().st_mode & 0o777 == 0o600
    tokens = json.loads(private.read_text())["table"]
    assert tokens["viewer-secret"] == {"role": "viewer", "slot": None, "mk_control": False}
    assert tokens["slot-secret-1"] == {"role": "viewer", "slot": 1, "mk_control": False}
    assert all(value["role"] == "viewer" and not value["mk_control"] for value in tokens.values())
    compile(deploy.RECONCILER, "reconciler", "exec")


class RecordingDocker:
    def __init__(self, inspect, active=None):
        self.current = inspect
        self.active = active
        self.mutations = []

    def inspect(self, name):
        assert name == "retroone-player5"
        return self.current

    def request(self, container, path):
        return 200, {"session": self.active}

    def call(self, method, path, body=None):
        self.mutations.append((method, path, body))


def test_live_session_or_identity_change_prevents_all_container_mutations():
    _, _, _, inspect = fixture()
    docker = RecordingDocker(inspect, active={"session_id": "live"})
    with pytest.raises(deploy.DeployError):
        deploy.replace_container(docker, "retroone-player5", inspect, {})
    assert docker.mutations == []
    docker.active = None
    original = copy.deepcopy(inspect)
    docker.current["Id"] = "externally-replaced"
    with pytest.raises(deploy.DeployError):
        deploy.replace_container(docker, "retroone-player5", original, {})
    assert docker.mutations == []


def test_config_scopes_mutations_to_own_ai_and_retains_family_read_access(tmp_path):
    _, entry, state, _ = fixture()
    records = [{"id": "player1", "name": "Family"}, {"id": "player5", "name": "AI A"}]
    deploy.write_config(tmp_path, state, records)
    config = json.loads((tmp_path / "config/mcp.json").read_text())
    family = config["profiles"]["player1"]
    assert not family["control_enabled"] and "selkies_tokens" not in family
    assert config["profiles"]["player5"]["selkies_tokens"] == {"1": "slot-secret-1"}
    credential = config["credentials"][0]
    assert credential["observe_profiles"] == ["player1", "player5"]
    for scope in ("control_profiles", "launch_profiles", "stop_profiles"):
        assert credential[scope] == ["player5"]
    assert "client-secret" not in (tmp_path / "config/mcp.json").read_text()
    deploy.write_config(tmp_path, state, records)  # Own config is idempotent.
    config["deployment_owner"] = "someone-else"
    (tmp_path / "config/mcp.json").write_text(json.dumps(config))
    with pytest.raises(deploy.DeployError):
        deploy.write_config(tmp_path, state, records)


def test_private_json_does_not_follow_a_symlink(tmp_path):
    target = tmp_path / "outside"
    target.write_text("preserve")
    link = tmp_path / "private.json"
    link.symlink_to(target)
    with pytest.raises(deploy.DeployError):
        deploy.private_json(link, {"secret": "value"})
    assert target.read_text() == "preserve"


def test_family_sharing_requires_operator_enablement_and_rerun_preserves_revocation(tmp_path):
    _, _, state, _ = fixture()
    records = [{"id": "player1", "name": "Family"}, {"id": "player5", "name": "AI A"}]
    deploy.write_config(tmp_path, state, records)
    path = tmp_path / "config/mcp.json"
    config = json.loads(path.read_text())
    assert config["family_watch"]["enabled"] is False
    config["family_watch"] = {"enabled": True, "profiles": ["player1"]}
    config["credentials"][0]["enabled"] = False
    config["credentials"][0]["observe_profiles"] = ["player5"]
    path.write_text(json.dumps(config))
    deploy.write_config(tmp_path, state, records)
    updated = json.loads(path.read_text())
    assert updated["family_watch"] == {"enabled": True, "profiles": ["player1"]}
    assert updated["credentials"][0]["enabled"] is False
    assert updated["credentials"][0]["observe_profiles"] == ["player5"]


def ownership_ledger(root, profiles):
    path = root / "state/requests.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE created_profiles (profile_id TEXT)")
        connection.executemany("INSERT INTO created_profiles VALUES (?)", [(profile,) for profile in profiles])
    return path


def test_rerun_preserves_creation_policy_and_leaves_owned_profiles_dynamic(tmp_path):
    _, _, state, _ = fixture()
    records = [{"id": "player1", "name": "Family"}, {"id": "player5", "name": "AI A"}]
    deploy.write_config(tmp_path, state, records)
    path = tmp_path / "config/mcp.json"
    existing = json.loads(path.read_text())
    existing["profile_creation"] = {"enabled": True, "api_url": "http://trusted-gateway:3000",
                                    "allow_legacy_input": True, "share_with_family": False}
    existing["credentials"][0].update(create_profiles=True, max_created_profiles=2,
                                       created_profile_permissions=["observe", "control", "launch", "stop"])
    path.write_text(json.dumps(existing))
    ledger = ownership_ledger(tmp_path, ["player6", None])
    before = ledger.read_bytes()

    deploy.write_config(tmp_path, state, records + [{"id": "player6", "name": "Own Agent"}])
    updated = json.loads(path.read_text())
    assert updated == existing
    assert "player6" not in updated["profiles"]
    assert ledger.read_bytes() == before


def test_explicit_owned_profile_and_operator_control_policy_survive_rerun(tmp_path):
    _, _, state, _ = fixture()
    records = [{"id": "player1", "name": "Family"}, {"id": "player5", "name": "AI A"}]
    deploy.write_config(tmp_path, state, records)
    path = tmp_path / "config/mcp.json"
    existing = json.loads(path.read_text())
    existing["profiles"]["player5"]["control_enabled"] = False
    existing["profiles"]["player6"] = {
        "name": "Operator Agent", "api_url": "http://explicit-worker:8080",
        "selkies_url": "http://explicit-worker:3000/stream", "control_enabled": True,
        "selkies_tokens": {"1": "explicit-slot-secret"}, "selkies_viewer_token": "explicit-viewer-secret",
    }
    existing["profile_creation"] = {"enabled": False}
    path.write_text(json.dumps(existing))
    ownership_ledger(tmp_path, ["player6"])

    deploy.write_config(tmp_path, state, records + [{"id": "player6", "name": "Registry Agent"}])
    assert json.loads(path.read_text()) == existing


@pytest.mark.parametrize("legacy_ledger", [False, True])
def test_config_generation_accepts_missing_or_legacy_ownership_table(tmp_path, legacy_ledger):
    _, _, state, _ = fixture()
    ledger = tmp_path / "state/requests.sqlite"
    if legacy_ledger:
        ledger.parent.mkdir()
        with sqlite3.connect(ledger) as connection:
            connection.execute("CREATE TABLE requests (id TEXT)")
        before = ledger.read_bytes()
    deploy.write_config(tmp_path, state, [{"id": "player1", "name": "Family"},
                                        {"id": "player5", "name": "AI A"}])
    config = json.loads((tmp_path / "config/mcp.json").read_text())
    assert list(config["profiles"]) == ["player1", "player5"]
    assert "profile_creation" not in config
    if legacy_ledger:
        assert ledger.read_bytes() == before
    else:
        assert not ledger.exists()


def test_unreadable_ownership_ledger_does_not_replace_existing_config(tmp_path):
    _, _, state, _ = fixture()
    records = [{"id": "player1", "name": "Family"}, {"id": "player5", "name": "AI A"}]
    deploy.write_config(tmp_path, state, records)
    path = tmp_path / "config/mcp.json"
    before = path.read_bytes()
    (tmp_path / "state/requests.sqlite").write_bytes(b"corrupted ledger")
    with pytest.raises(deploy.DeployError, match="ownership ledger"):
        deploy.write_config(tmp_path, state, records + [{"id": "player6", "name": "Own Agent"}])
    assert path.read_bytes() == before
