#!/usr/bin/env python3
"""Resumable, read-only arcade audit using a pinned DAT and the real libretro core.

Run ONLY in a disposable Linux worker with read-only ROM/core/system mounts and
CPU/memory limits. No production API, display, audio server or save is contacted.
This is a small libretro frontend, not an emulator. Observations never certify
playability: a core can return true and render an error screen.

Manifest example (operator-owned paths; do not commit game data):
  {"cores":{"fbneo":{"path":"/cores/fbneo_libretro.so","sha256":"...",
    "version":"v...","dat":"/inputs/arcade.dat","dat_sha256":"...",
    "dat_provenance":{"kind":"source_commit","commit":"...","url":"..."}}},
   "games":[{"id":"...","native_core":"fbneo","rom_path":"/roms/...zip"}]}

  python3 validate_games.py --manifest inputs.json --rom-root /roms \
    --system-root /system --output /results --level observe --limit 3

metadata: ZIP directory claims only (not integrity); integrity: decompress and
hash all members; observe: integrity plus real retro_load_game/run/input calls.
DAT BIOS lists can include alternative firmware. Missing DAT entries describe
set completeness, not necessarily a runtime blocker. No files are repaired.
"""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes as C
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib

VERSION = 1
CHUNK = 1024 * 1024
MAX_MEMBERS = 20000
MAX_UNPACKED = 2 * 1024**3
SAFE_SET = re.compile(r"[A-Za-z0-9_.+-]{1,180}\Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(CHUNK), b""):
            value.update(block)
    return value.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(canonical(value) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def contained(path, roots):
    target = Path(path).resolve(strict=True)
    if not any(target == root or root in target.parents for root in roots):
        raise ValueError("path outside allowed read-only roots")
    if not target.is_file():
        raise ValueError("not a regular file")
    return target


def identity(path):
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "missing": True}
    s = p.stat()
    return {"path": str(p.resolve()), "size": s.st_size, "mtime_ns": s.st_mtime_ns,
            "ctime_ns": s.st_ctime_ns, "inode": s.st_ino, "device": s.st_dev}


class Dat:
    def __init__(self, path):
        if Path(path).stat().st_size > 128 * 1024**2:
            raise ValueError("DAT exceeds bounded parser size")
        # stdlib ElementTree does not fetch the external Logiqx DTD. Explicit
        # entities are rejected to avoid expansion in untrusted operator input.
        data = Path(path).read_bytes()
        if b"<!ENTITY" in data.upper():
            raise ValueError("DAT entities are not allowed")
        tree = ET.fromstring(data)
        self.header = {node.tag: node.text for node in tree.findall("header/*")}
        self.games = {}
        for node in list(tree.findall("game")) + list(tree.findall("machine")):
            name = node.get("name", "")
            if not SAFE_SET.fullmatch(name) or name in self.games:
                raise ValueError("invalid or duplicate DAT set name")
            self.games[name] = {**node.attrib,
                                "roms": [dict(rom.attrib) for rom in node.findall("rom")]}

    def dependencies(self, name):
        result, visiting = [], set()

        def walk(current):
            if current in visiting:
                raise ValueError("cyclic DAT dependency")
            if current in result:
                return
            if not SAFE_SET.fullmatch(current):
                raise ValueError("unsafe DAT dependency")
            result.append(current)
            visiting.add(current)
            node = self.games.get(current, {})
            for field in ("cloneof", "romof"):
                if node.get(field):
                    walk(node[field])
            visiting.remove(current)
        walk(name)
        return result


def candidate_archives(rom, dat, system_root):
    """Match FBNeo's default current-folder -> system/fbneo -> system search.

    Do not find a same-named parent in an unrelated collection: the actual core
    would not see it. Merged archive launch aliases require explicit review.
    """
    roots = [rom.parent]
    if system_root:
        roots += [system_root / "fbneo", system_root]
    names = dat.dependencies(rom.stem) if rom.stem in dat.games else [rom.stem]
    candidates = [rom]
    for name in names:
        for root in roots:
            for extension in (".zip", ".7z"):
                candidate = root / (name + extension)
                if candidate not in candidates:
                    candidates.append(candidate)
    return candidates


def zip_inventory(path, verify):
    """No extraction. CRC/SHA1 are measured only after complete decompression."""
    start = identity(path)
    members = []
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS or sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise ValueError("archive exceeds member/unpacked budget")
        names = set()
        for info in infos:
            if info.is_dir():
                continue
            name = info.filename
            pure = PurePosixPath(name.replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts or name in names:
                raise ValueError("unsafe or duplicate ZIP member")
            names.add(name)
            row = {"name": name, "size": info.file_size, "crc": f"{info.CRC:08x}",
                   "verified": False}
            if verify:
                crc, count, digest = 0, 0, hashlib.sha1()
                with archive.open(info) as source:
                    for block in iter(lambda: source.read(CHUNK), b""):
                        count += len(block)
                        if count > info.file_size or count > MAX_UNPACKED:
                            raise ValueError("archive member exceeded declared size")
                        crc = zlib.crc32(block, crc)
                        digest.update(block)
                if count != info.file_size or crc != info.CRC:
                    raise ValueError("archive payload CRC/size mismatch")
                row.update(crc=f"{crc:08x}", sha1=digest.hexdigest(), verified=True)
            members.append(row)
    fingerprint = sha256(path) if verify else None
    if start != identity(path):
        raise ValueError("source changed during verification")
    return {"path": str(path), "identity": start, "sha256": fingerprint,
            "members": members, "status": "verified" if verify else "directory_only"}


def compare_dat(dat, set_name, archives):
    if set_name not in dat.games:
        return {"status": "unknown_set", "set_name": set_name, "playable": None}
    available = [(a["path"], member) for a in archives for member in a.get("members", [])]
    matched, missing, optional, uncertain = [], [], [], []
    for expected in dat.games[set_name]["roms"]:
        if expected.get("status") == "nodump" or expected.get("optional") == "yes":
            optional.append(expected)
            continue
        if not expected.get("crc") and not expected.get("sha1"):
            uncertain.append(expected)
            continue
        if expected.get("sha1") and not any(member.get("sha1") for _, member in available):
            uncertain.append({**expected, "reason": "SHA1 requires payload verification"})
            continue
        found = None
        for source, member in available:
            if expected.get("size") and int(expected["size"]) != member["size"]:
                continue
            if expected.get("crc") and expected["crc"].lower() != member["crc"]:
                continue
            if expected.get("sha1") and member.get("sha1") != expected["sha1"].lower():
                continue
            found = {"expected": expected["name"], "archive": source,
                     "member": member["name"], "verified": member["verified"]}
            break
        (matched if found else missing).append(found or expected)
    # A flat FBNeo DAT lists BIOS alternatives and optional chips without
    # encoding all runtime choices. Do not declare these missing rows fatal.
    return {"status": "dat_differences" if missing else "dat_incomplete_evidence" if uncertain else "dat_members_match",
            "set_name": set_name, "dependencies": dat.dependencies(set_name)[1:],
            "matched": matched, "missing_dat_members": missing,
            "optional_or_nodump": optional, "unverifiable_entries": uncertain,
            "all_matched_bytes_verified": bool(matched) and all(x["verified"] for x in matched),
            "runtime_requirement_of_missing": "unknown_requires_core_check",
            "playable": None}


class SystemInfo(C.Structure):
    _fields_ = [("name", C.c_char_p), ("version", C.c_char_p), ("extensions", C.c_char_p),
                ("need_fullpath", C.c_bool), ("block_extract", C.c_bool)]


class GameInfo(C.Structure):
    _fields_ = [("path", C.c_char_p), ("data", C.c_void_p), ("size", C.c_size_t), ("meta", C.c_char_p)]


class Geometry(C.Structure):
    _fields_ = [("width", C.c_uint), ("height", C.c_uint), ("max_width", C.c_uint),
                ("max_height", C.c_uint), ("aspect", C.c_float)]


class Timing(C.Structure):
    _fields_ = [("fps", C.c_double), ("sample_rate", C.c_double)]


class AVInfo(C.Structure):
    _fields_ = [("geometry", Geometry), ("timing", Timing)]


class Variable(C.Structure):
    _fields_ = [("key", C.c_char_p), ("value", C.c_char_p)]


def core_identity(path):
    library = C.CDLL(str(path))
    info = SystemInfo()
    library.retro_get_system_info.argtypes = [C.POINTER(SystemInfo)]
    library.retro_get_system_info(C.byref(info))
    return library, {"name": info.name.decode(), "version": info.version.decode(),
                     "extensions": info.extensions.decode(), "sha256": sha256(path)}


def observe(core, rom, system, work, frames):
    """Software-video libretro ABI. Each invocation MUST be a child process."""
    library, info = core_identity(core)
    output = {"core": info, "load_returned_true": False, "playable": None,
              "classification": "unverified", "messages": [], "log_formats": [],
              "core_errors": [],
              "frames": 0, "duplicate_frames": 0, "sample_hashes": {},
              "audio_frames": 0, "callback_errors": []}
    work.mkdir(parents=True, exist_ok=False)
    os.chdir(work)
    (work / "home").mkdir()
    os.environ["HOME"] = str(work / "home")
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        os.environ[name] = str(work / "home" / name.lower())
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "PULSE_SERVER"):
        os.environ.pop(name, None)
    # FBNeo copies MAX_PATH bytes from these pointers; allocate stable padded
    # buffers, not temporary Python strings. All writable paths are task-owned.
    buffers = {9: C.create_string_buffer(os.fsencode(system), 4096),
               31: C.create_string_buffer(os.fsencode(work / "saves"), 4096),
               30: C.create_string_buffer(os.fsencode(work / "assets"), 4096)}
    (work / "saves").mkdir()
    (work / "assets").mkdir()
    variables, references = {}, []
    position, pixel_format = [0], [0]
    event_coin, event_start = min(600, frames // 3), min(660, frames // 3 + 60)
    phases = {name: set() for name in ("before_coin", "after_coin", "after_start")}
    errors = output["callback_errors"]

    def append_bounded(key, text):
        if len(output[key]) < 200:
            output[key].append(text[:2048] if isinstance(text, str) else text)

    # HandleMessage supplies an already formatted "%s". Other variadic log
    # calls retain only their format string: never guess a vararg ABI.
    @C.CFUNCTYPE(None, C.c_int, C.c_char_p, C.c_void_p)
    def logger(level, fmt, arg):
        try:
            if fmt == b"%s" and arg:
                text = C.string_at(arg).decode("utf-8", "replace")
                append_bounded("messages", {"level": level, "text": text[:2048]})
            else:
                text = (fmt or b"").decode("utf-8", "replace")
                append_bounded("log_formats", text)
            if level == 3:
                append_bounded("core_errors", text)
        except Exception as exc:
            errors.append(str(exc))

    @C.CFUNCTYPE(C.c_bool, C.c_uint, C.c_void_p)
    def environment(command, pointer):
        try:
            if command in buffers:
                C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.addressof(buffers[command])
                return True
            if command == 27:  # GET_LOG_INTERFACE
                C.cast(pointer, C.POINTER(C.c_void_p))[0] = C.cast(logger, C.c_void_p).value
                return True
            if command in (6, 60):  # SET_MESSAGE / SET_MESSAGE_EXT first field
                message = C.cast(pointer, C.POINTER(C.c_char_p))[0]
                append_bounded("messages", {"level": None, "text": (message or b"").decode("utf-8", "replace")[:2048]})
                return True
            if command == 10:  # SET_PIXEL_FORMAT
                pixel_format[0] = C.cast(pointer, C.POINTER(C.c_int))[0]
                return pixel_format[0] in (0, 1, 2)
            if command in (3, 17):  # GET_CAN_DUPE / GET_VARIABLE_UPDATE
                C.cast(pointer, C.POINTER(C.c_bool))[0] = command == 3
                return True
            if command == 16:  # SET_VARIABLES, stable legacy options fallback
                values = C.cast(pointer, C.POINTER(Variable))
                for index in range(4096):
                    if not values[index].key:
                        break
                    text = values[index].value or b""
                    default = text.split(b";", 1)[-1].strip().split(b"|", 1)[0]
                    variables.setdefault(values[index].key, default)
                return True
            if command == 15:  # GET_VARIABLE
                value = C.cast(pointer, C.POINTER(Variable))
                key = value[0].key
                value[0].value = variables.get(key)
                return key in variables
            if command in (39, 52, 59):  # language, options version, message version
                C.cast(pointer, C.POINTER(C.c_uint))[0] = 0
                return True
            if command == (47 | 0x10000):  # GET_AUDIO_VIDEO_ENABLE (experimental)
                C.cast(pointer, C.POINTER(C.c_int))[0] = 3
                return True
            if command in (1, 11, 18, 32, 34, 35, 36, 37, 44, 55):
                return True
            if command == 7:  # SHUTDOWN is evidence, not successful gameplay
                output["core_requested_shutdown"] = True
                return True
            return False
        except Exception as exc:
            if len(errors) < 20:
                errors.append(str(exc))
            return False

    @C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_size_t)
    def video(data, width, height, pitch):
        output["frames"] += 1
        if not data:
            output["duplicate_frames"] += 1
            return
        if data == C.c_void_p(-1).value:
            output["hardware_frame_unsupported"] = True
            return
        if width > 4096 or height > 4096 or pitch * height > 32 * 1024**2:
            output["invalid_video_geometry"] = True
            return
        output["last_dimensions"] = [width, height]
        if position[0] % 30 == 0:
            # Hash only active pixel rows; uninitialised pitch padding would
            # create fake changes in an otherwise identical diagnostic screen.
            bpp = 4 if pixel_format[0] == 1 else 2
            if width * bpp > pitch:
                output["invalid_video_geometry"] = True
                return
            digest = hashlib.sha256()
            for row in range(height):
                digest.update(C.string_at(data + row * pitch, width * bpp))
            phase = "before_coin" if position[0] < event_coin else "after_coin" if position[0] < event_start else "after_start"
            phases[phase].add(digest.hexdigest())

    @C.CFUNCTYPE(C.c_size_t, C.c_void_p, C.c_size_t)
    def audio_batch(data, count):
        output["audio_frames"] += count
        return count

    @C.CFUNCTYPE(None, C.c_int16, C.c_int16)
    def audio(left, right):
        output["audio_frames"] += 1

    @C.CFUNCTYPE(None)
    def poll():
        pass

    @C.CFUNCTYPE(C.c_int16, C.c_uint, C.c_uint, C.c_uint, C.c_uint)
    def inputs(port, device, index, button):
        if port != 0 or device != 1:
            return 0
        return int((button == 2 and event_coin <= position[0] < event_coin + 12)
                   or (button == 3 and event_start <= position[0] < event_start + 12))

    callbacks = {"environment": environment, "video_refresh": video, "audio_sample": audio,
                 "audio_sample_batch": audio_batch, "input_poll": poll, "input_state": inputs}
    references.extend(callbacks.values())
    for name, callback in callbacks.items():
        method = getattr(library, "retro_set_" + name)
        method.argtypes = [type(callback)]
        method(callback)
    library.retro_load_game.argtypes = [C.POINTER(GameInfo)]
    library.retro_load_game.restype = C.c_bool
    library.retro_get_system_av_info.argtypes = [C.POINTER(AVInfo)]
    library.retro_set_controller_port_device.argtypes = [C.c_uint, C.c_uint]
    library.retro_init()
    started = time.monotonic()
    try:
        game = GameInfo(os.fsencode(rom), None, 0, None)
        output["load_returned_true"] = bool(library.retro_load_game(C.byref(game)))
        if output["load_returned_true"]:
            library.retro_set_controller_port_device(0, 1)
            av = AVInfo()
            library.retro_get_system_av_info(C.byref(av))
            output["reported_fps"] = av.timing.fps
            output["reported_sample_rate"] = av.timing.sample_rate
            for frame in range(frames):
                position[0] = frame
                library.retro_run()
                if output.get("core_requested_shutdown"):
                    break
            library.retro_unload_game()
    finally:
        library.retro_deinit()
    output["elapsed_seconds"] = round(time.monotonic() - started, 4)
    output["sample_hashes"] = {key: sorted(value) for key, value in phases.items()}
    output["input_schedule"] = {"coin_frame": event_coin, "start_frame": event_start,
                                "hold_frames": 12, "port": 0, "device": "joypad"}
    output["options"] = {key.decode(): value.decode() for key, value in variables.items()}
    output["classification"] = "load_and_frames_observed" if output["load_returned_true"] and output["frames"] else "load_failed_or_no_frames"
    if output["core_errors"]:
        output["classification"] = "core_reported_error"
    output["limitations"] = ["No visual gameplay or audible-audio verification", "Frame changes do not prove coin/start acceptance", "Default core options; no production shader/stream/network test", "No real-time performance claim from unthrottled headless run"]
    return output


def validate_one(request, parsed_dat=None, archive_cache=None):
    roots = [Path(request["rom_root"]).resolve()]
    system = Path(request["system_root"]).resolve() if request.get("system_root") else None
    if system:
        roots.append(system)
    rom = contained(request["game"]["rom_path"], roots[:1])
    spec, level = request["core"], request["level"]
    for field, expected in (("path", "sha256"), ("dat", "dat_sha256")):
        pinned_identity = request.get("pinned_identities", {}).get(field)
        if pinned_identity:
            if identity(spec[field]) != pinned_identity:
                raise ValueError("core/DAT changed after checksum verification")
        elif sha256(spec[field]) != spec[expected]:
            raise ValueError("core/DAT changed or pinned SHA256 mismatch")
    dat = parsed_dat or Dat(spec["dat"])
    archives, failures = [], []
    for path in candidate_archives(rom, dat, system):
        if not path.exists():
            continue
        try:
            path = contained(path, roots)
            if path.suffix.lower() != ".zip":
                failures.append({"path": str(path), "reason": "7z integrity not implemented"})
                continue
            cache_key = canonical(identity(path))
            if archive_cache is not None and cache_key in archive_cache:
                inventory = archive_cache[cache_key]
            else:
                inventory = zip_inventory(path, level != "metadata")
                if archive_cache is not None:
                    if len(archive_cache) >= 512:
                        archive_cache.pop(next(iter(archive_cache)))
                    archive_cache[cache_key] = inventory
            archives.append(inventory)
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            failures.append({"path": str(path), "reason": str(exc)})
    result = {"game_id": request["game"]["id"], "core_key": request["game"]["native_core"],
              "route_tested": "native_libretro" if level == "observe" else None,
              "original_mode": request["game"].get("mode"), "level": level,
              "core_sha256": spec["sha256"], "dat_sha256": spec["dat_sha256"],
              "dat_provenance": spec.get("dat_provenance", {}), "archives": archives,
              "archive_errors": failures, "integrity": compare_dat(dat, rom.stem, archives),
              "playable": None, "manual_review_required": True}
    if level == "observe":
        _, actual = core_identity(spec["path"])
        if actual["version"] != spec["version"]:
            raise ValueError("core ABI version differs from manifest")
        empty_system = Path(request["work"]).parent / "empty-system"
        empty_system.mkdir(exist_ok=True)
        result["observation"] = observe(spec["path"], rom, system or empty_system,
                                         Path(request["work"]), request["frames"])
    return result


def run_child(request, directory, timeout):
    """Separate process group, bounded retained diagnostics, hard wall deadline."""
    directory.mkdir(parents=True, exist_ok=False)
    source, output = directory / "request.json", directory / "result.json"
    atomic_json(source, request)
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(source), str(output)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    captured = bytearray()
    total = [0]

    def consume():
        with process.stdout:
            for block in iter(lambda: process.stdout.read(8192), b""):
                total[0] += len(block)
                if len(captured) < CHUNK:
                    captured.extend(block[:CHUNK - len(captured)])
    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    state = "completed"
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        state = "timeout"
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    finally:
        reader.join(timeout=5)
    (directory / "diagnostic.log").write_bytes(captured)
    result = json.loads(output.read_text()) if output.is_file() and output.stat().st_size < 32 * CHUNK else {}
    result.update(worker_state=state if process.returncode == 0 or state == "timeout" else "error",
                  worker_exit=process.returncode, diagnostic_total_bytes=total[0],
                  diagnostic_retained_bytes=len(captured), playable=None)
    return result


def run_metadata(request, directory, timeout, dat, cache):
    """Directory-only reads reuse one DAT; no core is dlopened or executed."""
    directory.mkdir(parents=True, exist_ok=False)
    atomic_json(directory / "request.json", request)

    def expired(signum, frame):
        raise TimeoutError("metadata inspection exceeded wall deadline")
    prior = signal.signal(signal.SIGALRM, expired)
    signal.alarm(timeout)
    try:
        result = validate_one(request, parsed_dat=dat, archive_cache=cache)
        result.update(worker_state="completed", worker_exit=0)
    except Exception as exc:
        result = {"worker_state": "timeout" if isinstance(exc, TimeoutError) else "error",
                  "error": str(exc), "playable": None}
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prior)
    atomic_json(directory / "result.json", result)
    return result


def run_batch(args):
    manifest = json.loads(args.manifest.read_text())
    root = args.rom_root.resolve(strict=True)
    system = args.system_root.resolve(strict=True) if args.system_root else None
    out = args.output.resolve()
    if out == root or root in out.parents or (system and (out == system or system in out.parents)):
        raise ValueError("output must be outside read-only source trees")
    out.mkdir(parents=True, exist_ok=True)
    lock = (out / "controller.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    database = sqlite3.connect(out / "progress.sqlite3")
    database.execute("CREATE TABLE IF NOT EXISTS results (key TEXT PRIMARY KEY, game_id TEXT, status TEXT, result TEXT, updated REAL)")
    database.execute("UPDATE results SET status='interrupted' WHERE status='running'")
    database.commit()
    dats, checked, pins, archive_cache = {}, {}, {}, {}
    for key, spec in manifest["cores"].items():
        if sha256(spec["path"]) != spec["sha256"] or sha256(spec["dat"]) != spec["dat_sha256"]:
            raise ValueError("manifest core/DAT checksum mismatch: " + key)
        if not spec.get("dat_provenance", {}).get("kind"):
            raise ValueError("explicit DAT provenance required")
        dats[key] = Dat(spec["dat"])
        checked[key] = spec
        pins[key] = {field: identity(spec[field]) for field in ("path", "dat")}
    counts = Counter()
    script_hash = sha256(__file__)
    selected = [game for game in manifest["games"]
                if (not args.game_id or game["id"] in args.game_id)
                and (not getattr(args, "platform", None) or game.get("platform") in args.platform)]
    atomic_json(out / "status.json", {"updated": time.time(), "counts": counts,
                                       "total_selected": len(selected), "finished": False,
                                       "level": args.level, "playable": None})
    for game in selected:
        if args.game_id and game["id"] not in args.game_id:
            continue
        if counts["executed"] >= args.limit:
            break
        core_key = game.get("native_core")
        if core_key not in {"fbneo", "fbalpha2012"} or core_key not in checked:
            counts["unsupported_core"] += 1
            continue
        spec, dat = checked[core_key], dats[core_key]
        rom = contained(game["rom_path"], [root])
        candidates = candidate_archives(rom, dat, system)
        signature = {"version": VERSION, "script": script_hash, "game": game,
                     "spec": spec, "files": [identity(p) for p in candidates],
                     "level": args.level, "frames": args.frames, "timeout": args.timeout,
                     "rom_root": str(root), "system_root": str(system) if system else None}
        key = hashlib.sha256(canonical(signature).encode()).hexdigest()
        previous = database.execute("SELECT status FROM results WHERE key=?", (key,)).fetchone()
        if previous and previous[0] == "completed" and not args.retry:
            counts["resumed_skipped"] += 1
            continue
        directory = out / "attempts" / (key[:16] + "-" + uuid.uuid4().hex[:8])
        request = {"game": game, "core": spec, "rom_root": str(root),
                   "system_root": str(system) if system else None, "level": args.level,
                   "frames": args.frames, "work": str(directory / "work"),
                   "pinned_identities": pins[core_key]}
        database.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?)",
                         (key, game["id"], "running", canonical({"attempt": str(directory)}), time.time()))
        database.commit()
        result = (run_metadata(request, directory, args.timeout, dat, archive_cache)
                  if args.level == "metadata" else run_child(request, directory, args.timeout))
        result.update(game_id=game["id"], attempt=str(directory), provenance_key=key)
        database.execute("UPDATE results SET status=?,result=?,updated=? WHERE key=?",
                         (result["worker_state"], canonical(result), time.time(), key))
        database.commit()
        counts["executed"] += 1
        counts[result["worker_state"]] += 1
        atomic_json(out / "status.json", {"updated": time.time(), "counts": counts,
                                           "total_selected": len(selected), "finished": False,
                                           "level": args.level, "last_game_id": game["id"], "playable": None})
        print(canonical({"game_id": game["id"], "state": result["worker_state"], "counts": counts}), flush=True)
    atomic_json(out / "status.json", {"updated": time.time(), "counts": counts,
                                       "total_selected": len(selected), "finished": True,
                                       "level": args.level, "playable": None})
    database.close()
    lock.close()
    return counts


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        try:
            result = validate_one(json.loads(Path(sys.argv[2]).read_text()))
            atomic_json(sys.argv[3], result)
        except Exception as exc:
            atomic_json(sys.argv[3], {"error": str(exc), "playable": None})
            raise
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--rom-root", type=Path, required=True)
    parser.add_argument("--system-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--level", choices=("metadata", "integrity", "observe"), default="metadata")
    parser.add_argument("--limit", type=int, default=3, help="Explicit batch ceiling; default is a small sample")
    parser.add_argument("--frames", type=int, default=1800)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--game-id", action="append")
    parser.add_argument("--platform", action="append", help="Filter manifest entries, not a claim of core support")
    parser.add_argument("--retry", action="store_true", help="Retest completed entries; saves remain attempt-isolated")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100000 or not 120 <= args.frames <= 36000 or not 1 <= args.timeout <= 600:
        parser.error("invalid bounded limit/frames/timeout")
    def terminate(signum, frame):
        # Raising through run_child's finally path kills only this task's
        # process group. Default SIGTERM would orphan a hung native core.
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    run_batch(args)


if __name__ == "__main__":
    main()
