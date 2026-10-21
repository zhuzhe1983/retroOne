#!/usr/bin/env python3
"""Create two registry-visible, isolated AI workers on the Docker host.

Default is a read-only plan. --apply is resumable using private intent records.
--rollback restores only this deployment's original, idle AI containers; registry
members and saves remain because the registry intentionally has no delete API.
Run on the NAS, not inside a family runtime. No secret is printed or put in argv.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import quote
import uuid


OWNER_LABEL = "io.retroone.mcp.owner"
PROFILE_LABEL = "io.retroone.profile"
IMAGE_DEFAULT = "retroone:local"
SELECTED_ENV = {
    "RETROONE_RUNTIME_IMAGE", "RETROONE_HOST_PROFILES_ROOT", "RETROONE_DOCKER_NETWORK",
    "RETROONE_HOST_ROM_ROOT", "RETROONE_HOST_ARTWORK_ROOT", "RETROONE_HOST_LIBRARY_DATA_ROOT",
}


class DeployError(Exception):
    pass


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=45):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Docker:
    def __init__(self, binary="/usr/local/bin/docker", socket_path="/var/run/docker.sock"):
        self.binary = binary
        self.socket = socket_path

    def call(self, method, path, body=None, missing_ok=False):
        connection = UnixConnection(self.socket, timeout=45)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            content = response.read(4 * 1024 * 1024)
            if response.status == 404 and missing_ok:
                return None
            if not 200 <= response.status < 300:
                raise DeployError("Docker operation failed (HTTP %d); no upstream body is logged." % response.status)
            return json.loads(content) if content else None
        finally:
            connection.close()

    def inspect(self, name, missing_ok=False):
        return self.call("GET", "/containers/" + quote(name, safe="") + "/json", missing_ok=missing_ok)

    def request(self, container, path, *, method="GET", body=None, token=None, port=8080):
        # This constant command receives request data through stdin, keeping
        # credentials out of process listings and SSH command arguments.
        code = """import json,sys,urllib.request,urllib.error
d=json.load(sys.stdin)
h={'Content-Type':'application/json'}
if d.get('token'): h['Authorization']='Bearer '+d['token']
r=urllib.request.Request('http://127.0.0.1:'+str(d['port'])+d['path'],
 data=json.dumps(d['body']).encode() if d['body'] is not None else None,
 headers=h,method=d['method'])
try:
 with urllib.request.urlopen(r,timeout=12) as f: status=f.status; raw=f.read(2*1024*1024)
except urllib.error.HTTPError as e: status=e.code; raw=b'{}'
try: value=json.loads(raw)
except ValueError: value={}
print(json.dumps({'status':status,'body':value}))
"""
        try:
            result = subprocess.run([self.binary, "exec", "-i", container, "/usr/bin/python3", "-c", code],
                                    input=json.dumps({"path": path, "method": method, "body": body,
                                                      "token": token, "port": port}).encode(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=True)
            value = json.loads(result.stdout)
            return value["status"], value["body"]
        except (subprocess.SubprocessError, ValueError, KeyError):
            raise DeployError("The selected runtime API is not ready.") from None


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise DeployError("Refusing a symlink for a private deployment file.")
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
        raise DeployError("Unsafe private deployment file.")
    return json.loads(path.read_text())


def env_dict(inspect):
    return dict(value.split("=", 1) for value in inspect["Config"].get("Env", []) if "=" in value)


def original_spec(inspect, network, name):
    """Snapshot a reproducible container, preserving its data bind mounts."""
    spec = copy.deepcopy(inspect["Config"])
    spec["Image"] = inspect["Image"]
    spec.pop("Hostname", None)
    spec["HostConfig"] = copy.deepcopy(inspect["HostConfig"])
    spec["NetworkingConfig"] = {"EndpointsConfig": {network: {"Aliases": [name]}}}
    return spec


def validate_new_worker(inspect, entry, state, environment):
    profile = entry["profile_id"]
    if profile in state["baseline_profiles"] or int(profile[6:]) < 5:
        raise DeployError("Refusing to modify a pre-existing family profile.")
    labels = inspect["Config"].get("Labels") or {}
    if labels.get(PROFILE_LABEL) != profile or labels.get("io.retroone.managed") != "profile":
        raise DeployError("The new worker does not match provisioner ownership.")
    existing_owner = labels.get(OWNER_LABEL)
    if existing_owner and existing_owner != state["owner"]:
        raise DeployError("The worker belongs to another MCP deployment.")
    mounts = {mount["Destination"]: mount for mount in inspect.get("Mounts", [])}
    expected = environment["RETROONE_HOST_PROFILES_ROOT"].rstrip("/") + "/" + profile
    if mounts.get("/config", {}).get("Source") != expected:
        raise DeployError("The new worker has an unexpected profile directory.")
    if (mounts.get("/roms", {}).get("Source") != environment["RETROONE_HOST_ROM_ROOT"]
            or mounts.get("/roms", {}).get("RW") is not False):
        raise DeployError("The new worker does not have the expected read-only ROM library.")
    if environment["RETROONE_DOCKER_NETWORK"] not in inspect["NetworkSettings"]["Networks"]:
        raise DeployError("The new worker is on an unexpected network.")


def token_table(entry):
    table = {token: {"role": "viewer", "slot": int(slot), "mk_control": False}
             for slot, token in entry["slot_tokens"].items()}
    table[entry["viewer_token"]] = {"role": "viewer", "slot": None, "mk_control": False}
    return table


# The table is memory-only upstream. This custom s6 service re-provisions it
# after Selkies restarts. A successful lightweight probe does not touch inputs,
# connect a controller, or initialize capture. It never prints credentials.
RECONCILER = '''#!/usr/bin/python3
import json,time,urllib.request,urllib.error
from pathlib import Path
d=json.loads(Path('/run/retroone-mcp-credentials.json').read_text())
base='http://127.0.0.1:3000/p/'+d['profile_id']+'/stream/api/'
def request(path,token,body=None):
 r=urllib.request.Request(base+path,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},
   data=json.dumps(body).encode() if body is not None else None)
 try:
  with urllib.request.urlopen(r,timeout=5) as f: f.read(1048576); return f.status
 except urllib.error.HTTPError as e: return e.code
while True:
 try:
  if request('sessions',d['viewer_token'])==401:
   request('tokens',d['master_token'],d['table'])
 except Exception: pass
 time.sleep(2)
'''
SERVICE_RUN = "#!/usr/bin/with-contenv bash\nexec /usr/bin/python3 /run/retroone-mcp-token-reconciler.py\n"


def install_runtime_files(root, entry):
    folder = root / "private"
    credential_path = folder / (entry["profile_id"] + "-selkies.json")
    private_json(credential_path, {"profile_id": entry["profile_id"], "master_token": entry["master_token"],
                                   "viewer_token": entry["viewer_token"], "table": token_table(entry)})
    paths = [(folder / "token-reconciler.py", RECONCILER), (folder / "token-service", SERVICE_RUN)]
    for path, content in paths:
        if path.is_symlink():
            raise DeployError("Refusing a symlink for a generated service.")
        path.write_text(content)
        path.chmod(0o755)
    return [str(credential_path) + ":/run/retroone-mcp-credentials.json:ro",
            str(paths[0][0]) + ":/run/retroone-mcp-token-reconciler.py:ro",
            str(paths[1][0]) + ":/custom-services.d/retroone-mcp-tokens:ro"]


def secure_spec(original, entry, state, extra_binds):
    spec = copy.deepcopy(original)
    spec["Image"] = state["image_id"]
    environment = dict(item.split("=", 1) for item in spec["Env"])
    environment.update(SELKIES_MASTER_TOKEN=entry["master_token"], SELKIES_ENABLE_COLLAB="false",
                       SELKIES_COMMAND_ENABLED="false", SELKIES_FILE_TRANSFERS="none")
    spec["Env"] = [key + "=" + value for key, value in environment.items()]
    spec.setdefault("Labels", {})[OWNER_LABEL] = state["owner"]
    spec["HostConfig"]["Binds"].extend(extra_binds)
    return spec


def require_idle(docker, container):
    status, value = docker.request(container, "/api/session")
    if status != 200 or value.get("session"):
        raise DeployError("The task-owned AI worker has a live or unknown game session; it was preserved.")


def create_container(docker, name, spec):
    docker.call("POST", "/containers/create?name=" + quote(name, safe=""), spec)
    docker.call("POST", "/containers/" + name + "/start")


def replace_container(docker, name, old_inspect, spec):
    # Container identity is rechecked immediately before mutation. Deleting this
    # ephemeral container never deletes its bind-mounted saves or profile data.
    latest = docker.inspect(name)
    if latest["Id"] != old_inspect["Id"]:
        raise DeployError("The worker changed during preparation; no replacement was attempted.")
    if latest.get("State", {}).get("Running"):
        require_idle(docker, name)
        docker.call("POST", "/containers/" + name + "/stop?t=20")
    docker.call("DELETE", "/containers/" + name)
    create_container(docker, name, spec)


def registry(docker):
    status, value = docker.request("retroone", "/api/profiles")
    if status != 200 or not isinstance(value.get("profiles"), list):
        raise DeployError("The profile registry is unavailable.")
    return value


def wait_ready(docker, profile, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        values = registry(docker)["profiles"]
        record = next((item for item in values if item["id"] == profile), None)
        if record and record["status"] == "ready":
            return
        if record and record["status"] == "error":
            raise DeployError("New AI profile provisioning failed; retry this script after resolving the provisioner error.")
        time.sleep(2)
    raise DeployError("Timed out waiting for the new AI profile; existing players were not changed.")


def write_config(root, state, records):
    (root / 'state').mkdir(parents=True, exist_ok=True, mode=0o700)
    if 'observer_token' not in state:
        state['observer_token'] = secrets.token_urlsafe(48)
        private_json(root / 'private' / 'deployment.json', state)
    profiles = {}
    entries = {entry["profile_id"]: entry for entry in state["entries"]}
    for record in records:
        identifier = record["id"]
        container = "retroone" if identifier == "player1" else "retroone-" + identifier
        base = "http://" + container + ":3000/p/" + identifier
        entry = entries.get(identifier)
        profiles[identifier] = {"name": record["name"], "api_url": base, "selkies_url": base + "/stream",
                                "control_enabled": bool(entry)}
        if entry:
            profiles[identifier].update(selkies_tokens={"1": entry["slot_tokens"]["1"]}, selkies_viewer_token=entry["viewer_token"])
    credentials = []
    for entry in state["entries"]:
        credentials.append({"id": entry["agent_id"], "token_sha256": hashlib.sha256(entry["client_token"].encode()).hexdigest(),
                            "observe_profiles": list(profiles), "control_profiles": [entry["profile_id"]],
                            "launch_profiles": [entry["profile_id"]], "stop_profiles": [entry["profile_id"]],
                            "slots": [1], "enabled": True})
    credentials.append({'id': 'family-observer',
                        'token_sha256': hashlib.sha256(state['observer_token'].encode()).hexdigest(),
                        'observe_profiles': list(profiles), 'control_profiles': [],
                        'launch_profiles': [], 'stop_profiles': [], 'enabled': True})
    path = root / "config" / "mcp.json"
    # A marker prevents silently replacing unrelated operator credentials.
    if path.exists() and read_json(path).get("deployment_owner") != state["owner"]:
        raise DeployError("An unrelated MCP config already exists; it was not overwritten.")
    existing = read_json(path) if path.exists() else {}
    # Reconciliation must not silently undo operator revocation or replace
    # credentials they added after the first deployment.
    previous = {item['id']: item for item in existing.get('credentials', [])}
    for credential in credentials:
        previous.setdefault(credential['id'], credential)
    credentials = list(previous.values())
    private_json(path, {"deployment_owner": state["owner"], "profiles": profiles, "credentials": credentials,
                        "family_watch": existing.get('family_watch', {"enabled": False, "profiles": list(profiles)})})
    tokens = {entry["agent_id"]: {"token": entry["client_token"], "profile_id": entry["profile_id"]} for entry in state["entries"]}
    tokens['family-observer'] = {'token': state['observer_token'], 'profile_id': None}
    private_json(root / "private" / "client-tokens.json", tokens)
    # No tokens here. Keep ownership aligned with the operator-created 0600
    # config instead of broadening its permissions for an image-default UID.
    env_path = root / 'compose.env'
    if not env_path.exists():
        env_path.write_text('RETROONE_MCP_UID=' + str(os.getuid()) + '\nRETROONE_MCP_GID=' + str(os.getgid()) + '\n'
                            + 'RETROONE_MCP_CONFIG_PATH=' + str(root / 'config') + '\n'
                            + 'RETROONE_MCP_STATE_PATH=' + str(root / 'state') + '\n')
        env_path.chmod(0o600)


def execute(args):
    docker = Docker(args.docker, args.socket)
    provisioner = docker.inspect("retroone-provisioner")
    environment = {key: value for key, value in env_dict(provisioner).items() if key in SELECTED_ENV}
    if not SELECTED_ENV <= environment.keys():
        raise DeployError("Provisioner settings are incomplete.")
    snapshot = registry(docker)
    root = Path(args.root).absolute()
    state_path = root / "private" / "deployment.json"
    state = read_json(state_path) if state_path.exists() else None
    image = docker.call("GET", "/images/" + quote(args.image, safe="") + "/json")
    if not args.apply and not args.rollback:
        print(json.dumps({"action": "plan", "image": image["Id"], "network": environment["RETROONE_DOCKER_NETWORK"],
                          "existing_profiles": [item["id"] for item in snapshot["profiles"]],
                          "new_names": ["AI A", "AI B"], "private_root": str(root), "resuming": bool(state)}, indent=2))
        return
    if not state:
        if args.rollback:
            raise DeployError("No task-owned deployment exists to roll back.")
        if (root / "config" / "mcp.json").exists():
            raise DeployError("An MCP config already exists without this helper's ownership record.")
        state = {"version": 1, "owner": uuid.uuid4().hex, "image_id": image["Id"],
                 "baseline_profiles": [item["id"] for item in snapshot["profiles"]], "entries": []}
        for letter in ("A", "B"):
            state["entries"].append({"name": "AI " + letter, "agent_id": "agent-" + letter.lower(),
                                     "request_id": str(uuid.uuid4()), "avatar": "rocket",
                                     "master_token": secrets.token_urlsafe(48), "viewer_token": secrets.token_urlsafe(48),
                                     "slot_tokens": {str(n): secrets.token_urlsafe(48) for n in range(1, 5)},
                                     "client_token": secrets.token_urlsafe(48)})
        private_json(state_path, state)  # Durable intent before the first POST.
    elif state["image_id"] != image["Id"]:
        raise DeployError("The selected image changed; pass the original pinned image to resume.")

    if args.rollback:
        for entry in state["entries"]:
            if not entry.get("original_spec"):
                continue
            name = "retroone-" + entry["profile_id"]
            current = docker.inspect(name)
            if current["Config"].get("Labels", {}).get(OWNER_LABEL) != state["owner"]:
                continue  # Never replace containers no longer carrying our label.
            validate_new_worker(current, entry, state, environment)
            replace_container(docker, name, current, entry["original_spec"])
            entry["phase"] = "rolled_back"
            private_json(state_path, state)
            print("Restored original AI worker " + entry["profile_id"] + "; member and saves retained.")
        config_path = root / "config" / "mcp.json"
        if config_path.exists():
            config = read_json(config_path)
            if config.get("deployment_owner") == state["owner"]:
                for entry in state["entries"]:
                    profile = config["profiles"].get(entry.get("profile_id"), {})
                    profile["control_enabled"] = False
                    profile.pop("selkies_tokens", None)
                    profile.pop("selkies_viewer_token", None)
                for credential in config["credentials"]:
                    credential["enabled"] = False
                private_json(config_path, config)
        return

    for entry in state["entries"]:
        if not entry.get("profile_id"):
            status, created = docker.request("retroone", "/api/profiles", method="POST", body={
                "name": entry["name"], "avatar": entry["avatar"], "request_id": entry["request_id"]})
            if status != 202 or not str(created.get("id", "")).startswith("player"):
                raise DeployError("The registry could not allocate the AI profile.")
            entry["profile_id"] = created["id"]
            private_json(state_path, state)
        profile = entry["profile_id"]
        wait_ready(docker, profile)
        name = "retroone-" + profile
        current = docker.inspect(name, missing_ok=True)
        if current is None:
            # Resume after a crash between removing our original and creating
            # the secure replacement; there is no existing container to touch.
            if not entry.get("secure_spec"):
                raise DeployError("The newly allocated worker is missing before ownership was recorded.")
            create_container(docker, name, entry["secure_spec"])
            current = docker.inspect(name)
        validate_new_worker(current, entry, state, environment)
        if current["Config"].get("Labels", {}).get(OWNER_LABEL) != state["owner"]:
            if entry.get("phase") == "secure":
                raise DeployError("The task-owned worker was replaced externally; it was preserved.")
            if current.get("State", {}).get("Running"):
                require_idle(docker, name)
            entry["original_spec"] = original_spec(current, environment["RETROONE_DOCKER_NETWORK"], name)
            binds = install_runtime_files(root, entry)
            entry["secure_spec"] = secure_spec(entry["original_spec"], entry, state, binds)
            entry["phase"] = "replacing"
            private_json(state_path, state)
            try:
                replace_container(docker, name, current, entry["secure_spec"])
            except Exception:
                # If creation failed after removal, recover the new idle worker
                # from its saved original spec, never another family worker.
                if docker.inspect(name, missing_ok=True) is None:
                    create_container(docker, name, entry["original_spec"])
                raise
        elif not current.get("State", {}).get("Running"):
            docker.call("POST", "/containers/" + name + "/start")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                status, _ = docker.request(name, "/p/" + profile + "/stream/api/sessions",
                                           token=entry["viewer_token"], port=3000)
                if status == 200:
                    break
            except DeployError:
                pass
            time.sleep(2)
        else:
            raise DeployError("The secure AI runtime is not ready; rerun to resume or use --rollback.")
        entry["phase"] = "secure"
        private_json(state_path, state)
        print("Secure AI worker ready: " + profile)
    write_config(root, state, registry(docker)["profiles"])
    print("Private MCP configuration: " + str(root / "config" / "mcp.json"))
    print("Private client credentials: " + str(root / "private" / "client-tokens.json"))
    print("Family workers were not restarted, stopped, or connected to a controller.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/volume1/docker/retroOne/mcp")
    parser.add_argument("--image", default=IMAGE_DEFAULT)
    parser.add_argument("--docker", default="/usr/local/bin/docker")
    parser.add_argument("--socket", default="/var/run/docker.sock")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    try:
        execute(args)
    except (DeployError, OSError, ValueError, KeyError) as error:
        # Never print arbitrary Docker responses or credential-bearing configs.
        message = str(error) if isinstance(error, DeployError) else "Deployment failed; private intent was retained for recovery."
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
