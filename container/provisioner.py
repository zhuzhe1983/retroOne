"""Consume a narrow profile-creation queue; the web app has no Docker access.

Only operator-configured image, mounts, network and GPU may be used. Requests
cannot select commands, environment variables, paths, ports or image names.
The worker never removes or recreates a container and never writes the registry.
"""
from __future__ import annotations

import fcntl
import http.client
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import threading
import time
from urllib.parse import quote
from urllib.request import urlopen
import uuid

LOG = logging.getLogger("retroone.provisioner")
PROFILE = re.compile(r"player[1-9][0-9]{0,5}\Z")
ALLOWED_REQUEST_KEYS = {"version", "operation", "profile_id", "job_id", "attempt", "created_at"}


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path):
        super().__init__("localhost", timeout=30)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Docker:
    def __init__(self, socket_path="/var/run/docker.sock"):
        self.socket_path = socket_path

    def call(self, method, path, body=None, missing_ok=False):
        connection = UnixConnection(self.socket_path)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, payload, {"Content-Type": "application/json"})
            response = connection.getresponse()
            content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise RuntimeError("Docker response exceeded the worker limit")
            if response.status == 404 and missing_ok:
                return None
            if not 200 <= response.status < 300:
                raise RuntimeError(f"Docker {method} {path}: HTTP {response.status}: {content[:1000]!r}")
            return json.loads(content) if content else None
        finally:
            connection.close()


def read_json(path: Path, limit=256 * 1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Unsafe or oversized control file")
    return json.loads(path.read_text("utf-8"))


def atomic_json(path: Path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def validate_request(value, filename):
    if not isinstance(value, dict) or set(value) != ALLOWED_REQUEST_KEYS:
        raise ValueError("Unsupported request fields")
    if type(value.get("version")) is not int or value["version"] != 1 or value.get("operation") != "provision":
        raise ValueError("Unsupported operation")
    profile = value.get("profile_id", "")
    if not isinstance(profile, str) or not PROFILE.fullmatch(profile) or int(profile[6:]) < 3:
        raise ValueError("Only newly allocated player IDs may be provisioned")
    job = value.get("job_id", "")
    if not isinstance(job, str) or str(uuid.UUID(job)) != job or filename != job + ".json":
        raise ValueError("Invalid job identity")
    if type(value.get("attempt")) is not int or not 1 <= value["attempt"] <= 1000:
        raise ValueError("Invalid retry count")
    return profile, job


class Provisioner:
    def __init__(self, docker=None):
        self.control = Path(os.environ.get("RETROONE_CONTROL_ROOT", "/control"))
        self.registry = Path(os.environ.get("RETROONE_REGISTRY_PATH", "/registry/profiles.json"))
        self.profiles = Path(os.environ.get("RETROONE_PROFILES_ROOT", "/profiles"))
        self.host_profiles = os.environ.get("RETROONE_HOST_PROFILES_ROOT", "/volume1/docker/retroOne/profiles")
        self.rom = os.environ.get("RETROONE_HOST_ROM_ROOT", "/volume2/rom/one")
        self.artwork = os.environ.get("RETROONE_HOST_ARTWORK_ROOT", "/volume1/docker/retroOne/data/retroone/artwork")
        self.library_data = os.environ.get("RETROONE_HOST_LIBRARY_DATA_ROOT", "/volume1/docker/retroOne/library-data")
        self.network = os.environ.get("RETROONE_DOCKER_NETWORK", "retroone_default")
        self.image = os.environ.get("RETROONE_RUNTIME_IMAGE", "retroone:local")
        self.gateway = os.environ.get("RETROONE_GATEWAY_URL", "http://retroone:3000").rstrip("/")
        self.docker = docker or Docker()
        self.stopping = threading.Event()
        self.image_id = None
        for path in (self.host_profiles, self.rom, self.artwork, self.library_data):
            if not Path(path).is_absolute() or any(c in path for c in (":", "\n", "\0")):
                raise ValueError("Operator mount paths must be absolute bind-mount paths")
        for name in ("requests", "running", "results", "rejected"):
            (self.control / name).mkdir(parents=True, exist_ok=True)

    def authorized(self, profile, job):
        registry = read_json(self.registry)
        # Registry schema is versioned by the API. It is read atomically and
        # never edited by this worker, so user names and avatars stay intact.
        entries = registry.get("profiles", {})
        entry = entries.get(profile) if isinstance(entries, dict) else None
        if not entry or entry.get("status") != "provisioning" or entry.get("job_id") != job:
            raise ValueError("Registry does not authorize this provisioning job")

    def container_spec(self, profile):
        name = "retroone-" + profile
        environment = {
            "PUID": os.environ.get("PUID", "1026"), "PGID": os.environ.get("PGID", "100"),
            "RETROONE_SUPPLEMENTARY_GIDS": os.environ.get("RETROONE_SUPPLEMENTARY_GIDS", "101"),
            "TZ": os.environ.get("TZ", "Asia/Shanghai"),
            "DRI_NODE": "/dev/dri/renderD128", "DRINODE": "/dev/dri/renderD128",
            "RETROONE_GPU_BACKEND": os.environ.get("RETROONE_GPU_BACKEND", "intel"),
            "SELKIES_USE_CPU": "false", "SELKIES_VIDEO_STREAMING_MODE": "true",
            "SELKIES_FRAMERATE": "60-60", "SELKIES_MANUAL_WIDTH": "0", "SELKIES_MANUAL_HEIGHT": "0",
            "SELKIES_MANUAL_RESOLUTION": "false|locked", "SELKIES_ENABLE_RESIZE": "false|locked",
            "RETROONE_ARTWORK_ROOT": "/artwork",
            "RETROONE_LIBRARY_DATA_ROOT": "/library-data",
            "RETROONE_PROFILE_ID": profile, "RETROONE_BASE_PATH": "/p/" + profile,
            "SUBFOLDER": "/p/" + profile + "/stream/", "RETROONE_GATEWAY": "false",
        }
        return {
            "Image": self.image_id,
            "Env": [key + "=" + value for key, value in environment.items()],
            "Labels": {"io.retroone.managed": "profile", "io.retroone.profile": profile},
            "HostConfig": {
                "Binds": [self.rom + ":/roms:ro", self.host_profiles + "/" + profile + ":/config:rw", self.artwork + ":/artwork:rw", self.library_data + ":/library-data:rw"],
                "Devices": [{"PathOnHost": "/dev/dri/renderD128", "PathInContainer": "/dev/dri/renderD128", "CgroupPermissions": "rw"}],
                "ShmSize": 2 * 1024 ** 3, "NetworkMode": self.network,
                "RestartPolicy": {"Name": "unless-stopped"},
                "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3"}},
            },
            "NetworkingConfig": {"EndpointsConfig": {self.network: {"Aliases": [name]}}},
        }

    def ensure_runtime(self, profile):
        name = "retroone-" + profile
        destination = self.profiles / profile
        if destination.is_symlink():
            raise ValueError("Profile directory must not be a symlink")
        destination.mkdir(exist_ok=True)
        if destination.resolve().parent != self.profiles.resolve():
            raise ValueError("Profile directory is outside configured storage")
        existing = self.docker.call("GET", "/containers/" + name + "/json", missing_ok=True)
        if existing is None:
            spec = self.container_spec(profile)
            self.docker.call("POST", "/containers/create?name=" + name, spec)
            LOG.info("Created isolated runtime %s", name)
            existing = self.docker.call("GET", "/containers/" + name + "/json")
        labels = existing.get("Config", {}).get("Labels") or {}
        if labels.get("io.retroone.managed") != "profile" or labels.get("io.retroone.profile") != profile:
            raise ValueError("Existing container is not owned by this profile worker")
        if existing.get("Image") != self.image_id:
            raise ValueError("Existing runtime uses another image; operator review is required")
        mounts = {item["Destination"]: item for item in existing.get("Mounts", [])}
        if (mounts.get("/config", {}).get("Source") != self.host_profiles + "/" + profile
                or mounts.get("/roms", {}).get("Source") != self.rom
                or mounts.get("/roms", {}).get("RW") is not False
                or mounts.get("/artwork", {}).get("Source") != self.artwork
                or mounts.get("/artwork", {}).get("RW") is not True
                or mounts.get("/library-data", {}).get("Source") != self.library_data
                or mounts.get("/library-data", {}).get("RW") is not True):
            raise ValueError("Existing runtime has unexpected storage mounts")
        if self.network not in existing.get("NetworkSettings", {}).get("Networks", {}):
            raise ValueError("Existing runtime is on an unexpected network")
        if not existing.get("State", {}).get("Running"):
            self.docker.call("POST", "/containers/" + name + "/start")
        return name

    def wait_ready(self, profile):
        deadline = time.monotonic() + 240
        url = self.gateway + "/p/" + profile + "/api/health"
        while not self.stopping.is_set() and time.monotonic() < deadline:
            try:
                with urlopen(url, timeout=15) as response:
                    payload = response.read(256 * 1024)
                    health = json.loads(payload)
                if health.get("status") == "ok" and health.get("profile_id") == profile:
                    return
            except (OSError, ValueError):
                pass
            self.stopping.wait(2)
        if self.stopping.is_set():
            raise InterruptedError("Worker stopping; retain job for startup recovery")
        raise RuntimeError("新成员服务尚未通过入口健康检查，请稍后重试")

    def process(self, path):
        profile = job = None
        try:
            request = read_json(path, 8192)
            profile, job = validate_request(request, path.name)
            self.authorized(profile, job)
        except (OSError, ValueError, TypeError, KeyError) as error:
            LOG.error("Rejected control request %s: %s", path.name, error)
            path.replace(self.control / "rejected" / (path.name + "." + uuid.uuid4().hex))
            return
        try:
            name = self.ensure_runtime(profile)
            self.wait_ready(profile)
            result = {"version": 1, "profile_id": profile, "job_id": job, "status": "ready", "completed_at": time.time()}
            LOG.info("Profile %s ready through shared gateway (%s)", profile, name)
        except InterruptedError:
            return
        except Exception:
            LOG.exception("Could not provision %s", profile)
            result = {"version": 1, "profile_id": profile, "job_id": job, "status": "error", "error": "成员服务启动失败，请稍后重试；已有存档会保留", "completed_at": time.time()}
        atomic_json(self.control / "results" / (job + ".json"), result)
        path.unlink(missing_ok=True)

    def run(self):
        # One worker owns the queue, including jobs claimed before a restart.
        with (self.control / "worker.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.image_id = self.docker.call("GET", "/images/" + quote(self.image, safe="") + "/json")["Id"]
            self.docker.call("GET", "/networks/" + quote(self.network, safe=""))
            LOG.info("Worker ready: fixed image %s, network %s", self.image_id, self.network)
            while not self.stopping.is_set():
                paths = sorted((self.control / "running").glob("*.json"))
                for queued in sorted((self.control / "requests").glob("*.json")):
                    claimed = self.control / "running" / queued.name
                    if claimed.exists():
                        continue
                    queued.replace(claimed)
                    paths.append(claimed)
                for path in paths:
                    if self.stopping.is_set():
                        break
                    self.process(path)
                self.stopping.wait(2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = Provisioner()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stopping.set())
    worker.run()
