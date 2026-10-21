import copy
import importlib.util
import json
from pathlib import Path

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
