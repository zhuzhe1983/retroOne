"""One managed native game, rendered by established emulators into Selkies.

Never accepts an executable or command line from metadata. ROMs are read-only;
emulator settings, archive working copies and saves live in config_root.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
import uuid
import zipfile

from .platforms import PLATFORMS
from .settings import DisplaySettings
from .display_runtime import display_backend
from .switch_runtime import SwitchRuntime

ARCADE_CORES = {"fbneo", "fbalpha2012", "mame2003_plus", "mame2003", "mame2010"}
NETWORK_GAMES = {"f9eb2a58978f3b367e18a092"}  # Ridge Racer 2 / ULJS00080, two-console race verified
BIOS_NAMES = {
    "fds": {"disksys.rom"}, "psx": {"scph5500.bin", "scph5501.bin", "scph5502.bin", "scph1001.bin", "psxonpsp660.bin"},
    "segacd": {"bios_cd_u.bin", "bios_cd_e.bin", "bios_cd_j.bin"},
    "saturn": {"sega_101.bin", "mpr-17933.bin"}, "dreamcast": {"dc_boot.bin"},
    "naomi": {"naomi.zip"}, "3do": {"panafz1.bin", "panafz10.bin", "goldstar.bin"},
    "atari5200": {"5200.rom"}, "pcecd": {"syscard3.pce"}, "pcfx": {"pcfx.rom"},
    "colecovision": {"colecovision.rom", "coleco.rom"}, "odyssey2": {"o2rom.bin"},
    "channelf": {"sl31253.bin"}, "msx": {"msx.rom"}, "msx2": {"msx2.rom"},
    "neogeocd": {"neocd.bin", "neocd_z.rom", "top-sp1.bin", "front-sp1.bin"},
}


class Runner:
    def __init__(self, config_root: Path, rom_root: Path):
        self.config_root = Path(config_root).resolve()
        self.rom_root = Path(rom_root).resolve()
        self.core_root = Path(os.getenv("RETROONE_CORE_ROOT", "/opt/retroone/libretro"))
        self._process = None
        self._session = None
        self._lock = threading.RLock()
        self.last_exit = None
        self._wine_prefix = None
        self._display_backend = display_backend(lambda name: self._program(name))
        self._switch = SwitchRuntime(self.config_root, self.rom_root, lambda name: self._program(name))

    @staticmethod
    def _firmware_files(root, excluded=()):
        # Prune console-specific trees before walking; Switch firmware may contain
        # hundreds of large NCAs and private keys, never RetroArch BIOS inputs.
        for directory, names, files in os.walk(root, followlinks=False):
            if Path(directory) == root:
                names[:] = [name for name in names if name.lower() not in excluded]
            for name in files:
                path = Path(directory) / name
                if path.is_file():
                    yield path

    @staticmethod
    def _program(name):
        return shutil.which(name) or next((str(p) for p in (Path("/usr/games") / name, Path("/usr/local/bin") / name) if p.is_file() and os.access(p, os.X_OK)), None)

    def _core(self, platform, hint=None):
        name = hint if platform in ("mame", "lightgun", "fbneo") and hint in ARCADE_CORES else PLATFORMS.get(platform, {}).get("native_core")
        return self.core_root / f"{name}_libretro.so" if name else None

    def _bios_status(self, platform, files):
        names = {p.name.lower() for p in files}
        if platform == "ps2":
            return any(p.stat().st_size in (4 * 1024**2, 8 * 1024**2) and p.suffix.lower() in (".bin", ".rom", ".bios") for p in files)
        required = BIOS_NAMES.get(platform)
        return bool(names & required) if required else True

    def display_capabilities(self):
        return self._display_backend.capabilities()

    def _apply_display(self, snapshot):
        return self._display_backend.apply(snapshot)

    def capabilities(self):
        display = bool(os.getenv("DISPLAY")) and Path("/tmp/.X11-unix/X" + os.getenv("DISPLAY", ":1").split(":")[-1].split(".")[0]).exists()
        firmware = self.rom_root / "firmware"
        firmware_files = list(self._firmware_files(firmware, ("switch",)))
        platforms = {}
        for key, definition in PLATFORMS.items():
            if key == "switch":
                capability = self._switch.readiness()
                if self._display_backend.unsupported_reason or not display:
                    capability["available"] = False
                    capability["reason"] += "；" + (self._display_backend.unsupported_reason or "云游戏图形桌面尚未就绪")
                platforms[key] = capability
                continue
            core = self._core(key)
            if key == "dos":
                engine = "DOSBox"
                ready = bool(self._program("dosbox"))
            elif key in ("gamecube", "wii"):
                engine = "Dolphin"
                ready = bool(self._program("dolphin-emu"))
            elif key == "ps2":
                engine = "PCSX2"
                ready = bool(self._program("pcsx2"))
            elif key in ("mame", "lightgun"):
                engine = "MAME"
                ready = bool(self._program("mame"))
            else:
                engine = f"RetroArch / {definition.get('native_core')}" if core else "未配置"
                ready = bool(core and core.is_file() and self._program("retroarch"))
            reason = None
            if not ready:
                reason = "尚未安装或验证这个系统的模拟器"
                if definition.get("kind") == "windows":
                    reason = "Windows 游戏需要逐个配置 Wine；目前仅收录，尚未验证运行"
            elif self._display_backend.unsupported_reason:
                reason = self._display_backend.unsupported_reason
                ready = False
            elif not display:
                reason = "云游戏图形桌面尚未就绪"
            elif definition.get("requires_bios") and not self._bios_status(key, firmware_files):
                reason = "缺少已识别的 BIOS / 固件，请将现有文件整理到 one/firmware"
                ready = False
            platforms[key] = {"available": ready and display, "engine": engine, "reason": reason, "requires_bios": bool(definition.get("requires_bios")), "verified_playable": False}
            if key == "3ds" and ready and display:
                platforms[key].update(experimental=True, reason="实验支持：部分游戏首次启动已通过，重复启动仍可能遇到 ARM 错误")
        sdlpal = bool(not self._display_backend.unsupported_reason and display and self._program("sdlpal"))
        wine = bool(not self._display_backend.unsupported_reason and display and self._program("wine") and self._program("wineserver") and self._program("retroone-wine"))
        return {"available": display and any(p["available"] for p in platforms.values()), "platforms": platforms, "engines": {"sdlpal": {"available": sdlpal, "reason": None if sdlpal else "SDLPAL 运行环境尚未就绪"}, "wine": {"available": wine, "reason": None if wine else "Wine 运行环境尚未就绪"}}, "gpu_backend": self._display_backend.name, "stream_url": "/stream/", "transport": "Selkies encoded video + Opus audio", "target_fps": 60, "max_sessions": 1, "max_gamepads": 4, "last_exit": self.last_exit}

    def status(self):
        with self._lock:
            if self._process is not None and self._process.poll() is not None:
                self.last_exit = {"game_id": self._session["game_id"], "exit_code": self._process.returncode, "log": self._session["log"]}
                self._process = None
                self._session = None
            if self._session:
                return dict(self._session, elapsed_seconds=round(time.monotonic() - self._started, 1))
            return None

    def _rom(self, value):
        path = Path(value).resolve()
        if not path.is_relative_to(self.rom_root) or not path.is_file():
            raise ValueError("游戏文件不存在或位于 ROM 目录之外")
        return path

    @staticmethod
    def _member(name):
        path = PurePosixPath(name.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or (path.parts and ":" in path.parts[0]):
            raise ValueError("压缩包包含不安全的路径")
        return path

    def _extract(self, archive: Path, dest: Path):
        marker = dest / ".retroone-extracted.json"
        identity = {"path": str(archive), "size": archive.stat().st_size, "mtime_ns": archive.stat().st_mtime_ns}
        if marker.exists() and json.loads(marker.read_text()) == identity:
            return dest
        if dest.exists() and any(dest.iterdir()):
            raise RuntimeError("游戏工作目录需要检查：发现未完成的解压，已保留现有文件")
        dest.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as zipped:
                total = sum(i.file_size for i in zipped.infolist())
                if total > shutil.disk_usage(dest).free - 1024**3:
                    raise RuntimeError("没有足够空间解压游戏（保留 1 GiB 余量）")
                for item in zipped.infolist():
                    name = item.filename
                    if not item.flag_bits & 0x800:
                        try:
                            name = name.encode("cp437").decode("gb18030")
                        except (UnicodeEncodeError, UnicodeDecodeError):
                            pass
                    relative = self._member(name)
                    if stat.S_ISLNK(item.external_attr >> 16):
                        raise ValueError("压缩包包含符号链接")
                    target = dest.joinpath(*relative.parts)
                    if item.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zipped.open(item) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
        else:
            binary = self._program("7z") or self._program("7zz")
            if not binary:
                raise RuntimeError("没有可用的 7-Zip 解压程序")
            index = dest / ".retroone-archive-index.txt"
            started = time.monotonic()
            with index.open("wb") as output:
                process = subprocess.Popen([binary, "l", "-slt", "--", str(archive)], stdout=output, stderr=subprocess.STDOUT)
                while process.poll() is None:
                    if time.monotonic() - started > 30 or index.stat().st_size > 16 * 1024**2:
                        process.kill()
                        process.wait()
                        raise RuntimeError("压缩包目录过大或检查超时，需先单独整理这个压缩包")
                    time.sleep(0.05)
            if process.returncode:
                raise RuntimeError("无法读取压缩包目录")
            if index.stat().st_size > 16 * 1024**2:
                raise RuntimeError("压缩包目录超过检查上限")
            listing = index.read_text(errors="replace")
            records = listing.split("----------", 1)[-1]
            unpacked_size = 0
            for line in records.splitlines():
                if line.startswith("Path = "):
                    self._member(line[7:])
                if line.startswith(("Symbolic Link = ", "Hard Link = ")):
                    raise ValueError("压缩包包含链接")
                if line.startswith("Size = ") and line[7:].isdigit():
                    unpacked_size += int(line[7:])
            if unpacked_size > shutil.disk_usage(dest).free - 1024**3:
                raise RuntimeError("没有足够空间解压游戏（保留 1 GiB 余量）")
            with (dest / ".retroone-extract.log").open("wb") as output:
                subprocess.run([binary, "x", "-y", "-bso0", "-bsp0", f"-o{dest}", "--", str(archive)], stdout=output, stderr=subprocess.STDOUT, check=True, timeout=1800)
            if any(p.is_symlink() for p in dest.rglob("*")):
                raise ValueError("压缩包包含符号链接，拒绝启动")
        marker.write_text(json.dumps(identity))
        return dest

    def _prepare_content(self, path, platform, workspace):
        if path.suffix.lower() not in (".zip", ".7z", ".rar", ".dosz"):
            return path
        if platform in ("fbneo", "mame", "lightgun", "naomi"):
            return path
        directory = self._extract(path, workspace / "content")
        extensions = set(PLATFORMS.get(platform, {}).get("extensions", [])) - {".zip", ".7z", ".rar", ".dosz"}
        choices = [p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in extensions]
        if platform == "dos":
            # Typical distribution launchers; ambiguous collections remain explicit.
            # PAL's bundled PLAY.BAT starts an old JS3 joystick TSR first; the
            # browser already supplies input, so launch the actual PAL executable.
            priorities = ["pal.exe", "play.bat", "start.bat", "run.bat", "game.exe"]
            for name in priorities:
                selected = [p for p in choices if p.name.lower() == name]
                if len(selected) == 1:
                    return selected[0]
            choices = [p for p in choices if p.suffix.lower() in (".exe", ".com", ".bat") and p.stem.lower() not in ("install", "setup", "dos4gw", "config", "setsound", "uninst")]
        else:
            for extension in (".m3u", ".cue", ".gdi"):
                preferred = [p for p in choices if p.suffix.lower() == extension]
                if len(preferred) == 1:
                    return preferred[0]
        if len(choices) != 1:
            raise RuntimeError(f"压缩包中有 {len(choices)} 个可能的启动文件，需要指定游戏入口")
        return choices[0]

    @staticmethod
    def _q(value):
        # RetroArch config does not support arbitrary newline-containing paths.
        value = str(value)
        if any(c in value for c in ('"', "\n", "\r")):
            raise ValueError("游戏路径包含模拟器配置不支持的字符")
        return '"' + value + '"'

    def _firmware(self):
        system = self.config_root / "system"
        system.mkdir(parents=True, exist_ok=True)
        supplied = self.rom_root / "firmware"
        if supplied.exists():
            for path in self._firmware_files(supplied, ("azahar", "pcsx2", "switch")):
                if not path.is_file() or not path.resolve().is_relative_to(self.rom_root):
                    continue
                relative = path.relative_to(supplied)
                target = system / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(path, target)
                flat = system / path.name.lower()
                if not flat.exists():
                    flat.symlink_to(target)
                if path.name.lower() in ("dc_boot.bin", "dc_flash.bin", "naomi.zip"):
                    (system / "dc").mkdir(exist_ok=True)
                    dc = system / "dc" / path.name.lower()
                    if not dc.exists():
                        dc.symlink_to(target)
        return system

    def _validated_network(self, game):
        network = game.get("network")
        if network is None:
            return None
        if game.get("platform") != "psp" or game.get("id") not in NETWORK_GAMES:
            raise ValueError("这款游戏尚未验证原生联网")
        if not isinstance(network, dict) or set(network) != {"room_id", "host_profile_id", "role", "nickname"}:
            raise ValueError("无效的联机启动配置")
        room = network["room_id"]
        if not isinstance(room, str) or str(uuid.UUID(room)) != room:
            raise ValueError("无效的联机房间")
        profile = os.environ.get("RETROONE_PROFILE_ID", "player1")
        host = network["host_profile_id"]
        if not isinstance(host, str) or not re.fullmatch(r"player[1-9][0-9]{0,5}", host):
            raise ValueError("无效的联机主机成员")
        role = network["role"]
        if role not in ("host", "guest") or (role == "host") != (host == profile):
            raise ValueError("联机角色与当前成员不一致")
        nickname = network["nickname"]
        if not isinstance(nickname, str) or not 1 <= len(nickname) <= 20 or any(ord(c) < 32 for c in nickname):
            raise ValueError("无效的联机昵称")
        # Only a validated member ID becomes a fixed Docker DNS name. The API
        # cannot supply an IP, hostname, port or emulator option through this field.
        hostname = "retroone" if host == "player1" else "retroone-" + host
        address = ipaddress.IPv4Address(socket.gethostbyname(hostname))
        if not address.is_private or address.is_loopback or address.is_unspecified or address.is_link_local:
            raise RuntimeError("联机主机没有可用的内部网络地址")
        return dict(network, host_ip=str(address), nickname=nickname.replace('"', '＂').replace('\\', '＼'))

    def _psp_identity(self):
        directory = self.config_root / "network"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "psp-identity.json"
        profile = os.environ.get("RETROONE_PROFILE_ID", "player1")
        if path.exists():
            if path.stat().st_size > 4096:
                raise RuntimeError("PSP 联机身份文件异常，请保留文件并检查配置")
            value = json.loads(path.read_text())
            if value.get("profile_id") != profile or not re.fullmatch(r"02(?::[0-9a-f]{2}){5}", value.get("mac", "")):
                raise RuntimeError("PSP 联机身份与成员不一致，请检查配置")
            return value["mac"]
        raw = "02" + uuid.uuid4().hex[:10]
        mac = ":".join(raw[i:i + 2] for i in range(0, 12, 2))
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps({"version": 1, "profile_id": profile, "mac": mac}) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
        return mac

    def _ppsspp_options(self, game, workspace):
        values = {}
        previous = self.config_root.parent / ".config/retroarch/config/PPSSPP/PPSSPP.opt"
        if previous.is_file() and previous.stat().st_size < 256 * 1024:
            for line in previous.read_text().splitlines():
                match = re.fullmatch(r'\s*(ppsspp_[a-zA-Z0-9_]+)\s*=\s*"([^"\r\n]*)"\s*', line)
                if match:
                    values[match[1]] = match[2]
        network = game.get("_network")
        values.update({
            "ppsspp_enable_wlan": "enabled" if network else "disabled",
            "ppsspp_enable_builtin_pro_ad_hoc_server": "enabled" if network and network["role"] == "host" else "disabled",
            "ppsspp_enable_upnp": "disabled", "ppsspp_upnp_use_original_port": "disabled",
        })
        mac = self._psp_identity().replace(":", "")
        values.update({f"ppsspp_change_mac_address{i:02}": digit for i, digit in enumerate(mac, 1)})
        if network:
            digits = "".join(f"{int(part):03}" for part in network["host_ip"].split("."))
            values.update({"ppsspp_change_pro_ad_hoc_server_address": "IP address", "ppsspp_wlan_channel": "1",
                           "ppsspp_port_offset": "10000", "ppsspp_minimum_timeout": "0", "ppsspp_forced_first_connect": "disabled"})
            values.update({f"ppsspp_pro_ad_hoc_server_address{i:02}": digit for i, digit in enumerate(digits, 1)})
        suffix = network["room_id"] if network else "solo"
        options = workspace / ("ppsspp-" + suffix + ".cfg")
        options.write_text("\n".join(f"{key} = {self._q(value)}" for key, value in sorted(values.items())) + "\n")
        return options

    def _retroarch(self, game, content, workspace):
        save = self.config_root / "saves" / ("3ds" if game["platform"] == "3ds" else str(game["id"]))
        save.mkdir(parents=True, exist_ok=True)
        if game["platform"] == "3ds":
            # Azahar stores saves by title ID under a shared user directory.
            # Seed only missing system/user files; never overwrite existing saves.
            # RetroArch sorts saves by core name; Azahar then adds its own folder.
            user = save / "Azahar" / "Azahar"
            source = self.rom_root / "firmware" / "azahar"
            if not source.exists():
                source = self.rom_root / "archive" / "pegasus" / "Emulators" / "citra-canary-mingw" / "user"
            for name in ("nand", "sysdata", "sdmc"):
                for path in (source / name).rglob("*"):
                    if path.is_file() and path.resolve().is_relative_to(self.rom_root):
                        destination = user / path.relative_to(source)
                        if not destination.exists():
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(path, destination)
        settings = {
            "video_driver": "gl", "video_fullscreen": "true", "video_windowed_fullscreen": "true",
            "video_vsync": "true", "video_threaded": "false", "video_refresh_rate": "60.000000",
            "audio_driver": "pulse", "audio_latency": "64", "audio_sync": "true",
            "input_joypad_driver": "sdl2", "input_max_users": "4",
            "input_autodetect_enable": "true", "pause_nonactive": "false",
            "notification_show_autoconfig": "false",
            "video_font_path": "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "config_save_on_exit": "false", "autosave_interval": "10",
            "savestate_auto_save": "true", "savestate_auto_load": "false",
            "savefile_directory": str(save), "savestate_directory": str(save),
            "sort_savefiles_enable": "true", "sort_savestates_enable": "true",
            "system_directory": str(self._firmware()), "screenshot_directory": str(save),
            "input_player1_l_x_minus": "a", "input_player1_l_x_plus": "d",
            "input_player1_l_y_minus": "w", "input_player1_l_y_plus": "s",
            "input_player1_up": "w", "input_player1_down": "s", "input_player1_left": "a", "input_player1_right": "d",
            "input_player1_b": "k", "input_player1_a": "j", "input_player1_y": "u", "input_player1_x": "i",
            "input_player1_l": "q", "input_player1_r": "e", "input_player1_select": "lshift", "input_player1_start": "enter",
            "input_menu_toggle": "f1", "input_save_state": "f2", "input_load_state": "f4", "input_exit_emulator": "f12",
        }
        # RetroArch defaults reserve K for frame advance, U for cheats and E
        # for slow motion. Explicitly clear gameplay-letter hotkeys.
        for hotkey in ("toggle_fullscreen", "state_slot_increase", "state_slot_decrease", "toggle_fast_forward", "hold_fast_forward", "shader_next", "shader_prev", "shader_toggle", "rewind", "movie_record_toggle", "pause_toggle", "frame_advance", "reset", "cheat_index_plus", "cheat_index_minus", "cheat_toggle", "netplay_flip_players", "slowmotion", "hold_slowmotion", "toggle_slowmotion", "toggle_vrr_runloop", "overlay_next", "disk_eject_toggle", "disk_next", "toggle_statistics"):
            settings["input_" + hotkey] = "nul"
        for player in range(1, 5):
            settings[f"input_player{player}_joypad_index"] = str(player - 1)
            for axis, value in {"l_x_minus":"-0", "l_x_plus":"+0", "l_y_minus":"-1", "l_y_plus":"+1", "r_x_minus":"-3", "r_x_plus":"+3", "r_y_minus":"-4", "r_y_plus":"+4", "l2":"+2", "r2":"+5"}.items():
                settings[f"input_player{player}_{axis}_axis"] = value
            for button, index in {"b": "0", "a": "1", "y": "2", "x": "3", "l": "4", "r": "5", "select": "6", "start": "7", "l3": "9", "r3": "10", "up": "h0up", "down": "h0down", "left": "h0left", "right": "h0right"}.items():
                settings[f"input_player{player}_{button}_btn"] = index
        if game["platform"] == "3ds":
            settings["video_driver"] = "glcore"
            options = Path("/opt/retroone/azahar-core-options.cfg")
            if options.exists():
                settings["core_options_path"] = str(options)
        elif game["platform"] == "psp":
            # Per-core .opt files otherwise override core_options_path. A room
            # must use its reviewed ad-hoc settings; solo must disable WLAN.
            settings["global_core_options"] = "true"
            settings["game_specific_options"] = "false"
            settings["core_options_path"] = str(self._ppsspp_options(game, workspace))
            if game.get("_network"):
                settings["netplay_nickname"] = game["_network"]["nickname"]  # libretro GET_USERNAME, not RetroArch netplay
        config = workspace / "retroarch.cfg"
        config.write_text("\n".join(f"{key} = {self._q(value)}" for key, value in settings.items()) + "\n")
        return [self._program("retroarch"), "--verbose", "--config", str(config), "-L", str(self._core(game["platform"], game.get("native_core"))), str(content)]

    def _command(self, game, content, workspace):
        platform = game["platform"]
        if platform == "switch":
            return self._switch.prepare(content)
        if game.get("engine_hint") == "wine":
            if platform != "windows":
                raise RuntimeError("这款 Windows 游戏尚未配置并验证启动方式")
            source = content.parent
            siblings = {p.name.lower(): p for p in source.iterdir() if p.is_file()}
            supported = {"rich4.exe": {"data.mkf", "effect.mkf", "panel.mkf"}, "hsl01.exe": {"hsl.pak", "movie.pak"}}
            choices = [siblings[name] for name, required in supported.items() if name in siblings and required <= siblings.keys()]
            if len(choices) != 1:
                raise RuntimeError("这款 Windows 游戏的启动文件或必要资源尚未通过验证")
            content = choices[0]
            resources = workspace / "windows"
            resources.mkdir(exist_ok=True)
            missing = []
            for path in source.rglob("*"):
                if not path.resolve().is_relative_to(self.rom_root):
                    raise ValueError("游戏资源链接超出 ROM 目录")
                if path.is_file() and not (resources / path.relative_to(source)).exists():
                    missing.append(path)
            if sum(path.stat().st_size for path in missing) > shutil.disk_usage(resources).free - 1024**3:
                raise RuntimeError("没有足够空间准备 Windows 游戏副本")
            for path in missing:
                target = resources / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            prefix = workspace / "wine-prefix"
            prefix.mkdir(exist_ok=True)
            # Synology inherited ACLs make wineserver's socket paths inconsistent
            # unless its newly created, task-owned prefix is private first.
            prefix.chmod(0o700)
            self._wine_prefix = prefix
            return [self._program("retroone-wine"), "explorer", "/desktop=RetroOne,1280x720", "Z:" + str(resources / content.name).replace("/", "\\")]
        if game.get("engine_hint") == "sdlpal":
            source = content.parent
            names = {p.name.lower() for p in source.iterdir() if p.is_file()}
            if not {"abc.mkf", "data.mkf", "m.msg", "word.dat"} <= names:
                raise RuntimeError("SDLPAL 需要完整的仙剑奇侠传游戏资源")
            resources = workspace / "pal"
            resources.mkdir(exist_ok=True)
            for path in source.rglob("*"):
                if not path.resolve().is_relative_to(self.rom_root):
                    raise ValueError("游戏资源链接超出 ROM 目录")
                relative = path.relative_to(source)
                target = resources.joinpath(*(p.lower() for p in relative.parts))
                if path.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
            saves = self.config_root / "saves" / str(game["id"])
            saves.mkdir(parents=True, exist_ok=True)
            config_text = f"GamePath={resources}\nSavePath={saves}\nFullScreen=1\nLaunchSetting=0\nEnableAviPlay=1\nKeepAspectRatio=1\nWindowWidth=1280\nWindowHeight=720\nLogLevel=2\n"
            (workspace / "sdlpal.cfg").write_text(config_text)
            (workspace / ".config").mkdir(exist_ok=True)
            (workspace / ".config" / "sdlpal.cfg").write_text(config_text)
            return [self._program("sdlpal")]
        if platform == "dos":
            # Native DOSBox has mature keyboard/audio behavior, keeps the whole
            # extracted game writable for saves, and never changes game speed to fit 60 fps.
            if not content.is_relative_to(workspace):
                destination = workspace / "dos"
                if not destination.exists():
                    if any(not path.resolve().is_relative_to(self.rom_root) for path in content.parent.rglob("*")):
                        raise ValueError("DOS 游戏资源链接超出 ROM 目录")
                    shutil.copytree(content.parent, destination, symlinks=False)
                content = destination / content.name
            config = workspace / "dosbox.conf"
            mount = str(content.parent)
            if '"' in mount or '\n' in mount or '\n' in content.name:
                raise ValueError("DOS 游戏路径包含不支持的字符")
            config.write_text("[sdl]\nfullscreen=true\nfullresolution=desktop\noutput=opengl\nautolock=false\n[dosbox]\nmemsize=32\n[cpu]\ncore=auto\ncycles=auto\n[mixer]\nrate=44100\n[autoexec]\n@echo off\nmount c \"" + mount + "\"\nc:\n" + content.name + "\nexit\n")
            return [self._program("dosbox"), "-conf", str(config), "-noconsole"]
        if platform in ("gamecube", "wii"):
            data = self.config_root / "dolphin"
            config = data / "Config"
            config.mkdir(parents=True, exist_ok=True)
            pads = config / "GCPadNew.ini"
            if not pads.exists():
                sections = []
                keyboard = {"Buttons/A":"J", "Buttons/B":"K", "Buttons/X":"U", "Buttons/Y":"I", "Buttons/Z":"Shift_L", "Buttons/Start":"Return", "Main Stick/Up":"W", "Main Stick/Down":"S", "Main Stick/Left":"A", "Main Stick/Right":"D", "D-Pad/Up":"Up", "D-Pad/Down":"Down", "D-Pad/Left":"Left", "D-Pad/Right":"Right", "Triggers/L":"Q", "Triggers/R":"E"}
                buttons = {"Buttons/A":"Button S", "Buttons/B":"Button E", "Buttons/X":"Button W", "Buttons/Y":"Button N", "Buttons/Z":"Shoulder R", "Buttons/Start":"Start", "Main Stick/Up":"Left Y+", "Main Stick/Down":"Left Y-", "Main Stick/Left":"Left X-", "Main Stick/Right":"Left X+", "C-Stick/Up":"Right Y+", "C-Stick/Down":"Right Y-", "C-Stick/Left":"Right X-", "C-Stick/Right":"Right X+", "D-Pad/Up":"Pad N", "D-Pad/Down":"Pad S", "D-Pad/Left":"Pad W", "D-Pad/Right":"Pad E", "Triggers/L":"Shoulder L", "Triggers/R":"Shoulder R", "Triggers/L-Analog":"Trigger L", "Triggers/R-Analog":"Trigger R"}
                for player in range(4):
                    lines = [f"[GCPad{player + 1}]", "Device = XInput2/0/Virtual core pointer", "Options/Always Connected = True"]
                    for key, value in buttons.items():
                        expression = f"`SDL/{player}/X360 Controller:{value}`"
                        if player == 0 and key in keyboard:
                            expression += f" | `{keyboard[key]}`"
                        lines.append(f"{key} = {expression}")
                    sections.append("\n".join(lines))
                pads.write_text("\n\n".join(sections) + "\n")
            settings = config / "Dolphin.ini"
            if not settings.exists():
                settings.write_text("[Display]\nFullscreen = True\n[Core]\nGFXBackend = OGL\nSIDevice0 = 6\nSIDevice1 = 6\nSIDevice2 = 6\nSIDevice3 = 6\n[Interface]\nConfirmStop = False\n")
            return [self._program("dolphin-emu"), "--batch", "--exec", str(content), "--user", str(data)]
        if platform == "ps2":
            # Upstream appends /PCSX2 to -datapath; pre-create its parent.
            parent = self.config_root / "pcsx2"
            data = parent / "PCSX2"
            bios = data / "bios"
            bios.mkdir(parents=True, exist_ok=True)
            candidates = []
            for path in (self.rom_root / "firmware" / "pcsx2").rglob("*"):
                if path.is_file() and path.resolve().is_relative_to(self.rom_root):
                    target = bios / path.name
                    if not target.exists():
                        target.symlink_to(path)
                    if path.stat().st_size in (4 * 1024**2, 8 * 1024**2):
                        candidates.append(path.name)
            if not candidates:
                raise RuntimeError("没有找到 one/firmware/pcsx2 下的 PS2 BIOS")
            config = data / "inis" / "PCSX2.ini"
            if not config.exists():
                # Configuration failures must never overlay another streamed game.
                xvfb = self._program("xvfb-run")
                if not xvfb:
                    raise RuntimeError("PCSX2 初始化需要独立的 Xvfb 图形环境")
                subprocess.run([xvfb, "-a", self._program("pcsx2"), "-datapath", str(parent), "-testconfig"], capture_output=True, timeout=30, check=True)
            marker = data / ".retroone-input-v1"
            if not marker.exists():
                text = config.read_text()
                text = re.sub(r"(?m)^SetupWizardIncomplete\s*=.*$", "SetupWizardIncomplete = false", text)
                text = re.sub(r"(?m)^BIOS\s*=.*$", "BIOS = " + sorted(candidates)[0], text)
                for setting in ("OsdShowSpeed", "OsdShowFPS", "OsdShowVPS"):
                    text = re.sub(r"(?m)^" + setting + r"\s*=.*$", setting + " = true", text)
                # PCSX2 represents alternative bindings as repeated INI keys.
                keyboard = {"Up":"W", "Down":"S", "Left":"A", "Right":"D", "Cross":"J", "Circle":"K", "Square":"U", "Triangle":"I", "L1":"Q", "R1":"E", "L2":"1", "R2":"3", "Start":"Return", "Select":"Shift", "LUp":"W", "LDown":"S", "LLeft":"A", "LRight":"D"}
                buttons = {"Up":"DPadUp", "Down":"DPadDown", "Left":"DPadLeft", "Right":"DPadRight", "Cross":"FaceSouth", "Circle":"FaceEast", "Square":"FaceWest", "Triangle":"FaceNorth", "L1":"LeftShoulder", "R1":"RightShoulder", "L2":"+LeftTrigger", "R2":"+RightTrigger", "Start":"Start", "Select":"Back", "LUp":"-LeftY", "LDown":"+LeftY", "LLeft":"-LeftX", "LRight":"+LeftX", "RUp":"-RightY", "RDown":"+RightY", "RLeft":"-RightX", "RRight":"+RightX", "L3":"LeftStick", "R3":"RightStick"}
                for player in (1, 2):
                    lines = ["Type = DualShock2", "Deadzone = 0.12", "AxisScale = 1.0"]
                    if player == 1:
                        lines += [f"{key} = Keyboard/{value}" for key, value in keyboard.items()]
                        lines += [f"{key} = Keyboard/{key}" for key in ("Up", "Down", "Left", "Right")]
                    lines += [f"{key} = SDL-{player - 1}/{value}" for key, value in buttons.items()]
                    section = f"[Pad{player}]\n" + "\n".join(lines) + "\n\n"
                    text = re.sub(r"(?ms)^\[Pad" + str(player) + r"\]\n.*?(?=^\[|\Z)", lambda _: section, text)
                config.write_text(text)
                marker.touch()
            return [self._program("pcsx2"), "-datapath", str(parent), "-batch", "-fullscreen", "--", str(content)]
        if platform in ("mame", "lightgun") and game.get("native_core") not in ARCADE_CORES:
            save = self.config_root / "mame"
            save.mkdir(parents=True, exist_ok=True)
            return [self._program("mame"), content.stem, "-rompath", str(content.parent) + ";" + str(self._firmware()), "-cfg_directory", str(save / "cfg"), "-nvram_directory", str(save / "nvram"), "-state_directory", str(save / "state"), "-snapshot_directory", str(save / "snap"), "-skip_gameinfo", "-joystick", "-video", "opengl", "-sound", "sdl", "-nowindow"]
        return self._retroarch(game, content, workspace)

    @staticmethod
    def _capture_output(pipe, log):
        # A broken core can print thousands of exceptions per second. Rotate
        # only its log (10 MiB x 3), never impose process-wide file-size limits.
        limit = 10 * 1024**2
        output = None
        size = 0
        retry_at = 0.0
        try:
            while chunk := pipe.read1(65536):
                try:
                    if output is None and time.monotonic() >= retry_at:
                        output = log.open("ab", buffering=0)
                        size = log.stat().st_size
                    if output is None:
                        continue
                    if size + len(chunk) > limit:
                        output.close()
                        output = None
                        previous = log.with_suffix(log.suffix + ".1")
                        if previous.exists():
                            os.replace(previous, log.with_suffix(log.suffix + ".2"))
                        os.replace(log, previous)
                        output = log.open("wb", buffering=0)
                        size = 0
                    output.write(chunk)
                    size += len(chunk)
                except OSError:
                    # Continue draining even when storage is temporarily full.
                    if output:
                        output.close()
                    output = None
                    retry_at = time.monotonic() + 10
        finally:
            if output:
                output.close()
            pipe.close()

    def start(self, game: dict):
        with self._lock:
            if self.status():
                raise RuntimeError("已有游戏正在运行，请先结束当前游戏")
            if self._display_backend.unsupported_reason:
                raise RuntimeError(self._display_backend.unsupported_reason)
            # A failed/finished Wine launcher may leave its prefix server alive.
            if self._wine_prefix:
                environment = dict(os.environ, WINEPREFIX=str(self._wine_prefix), WINEARCH="win32", WINEDEBUG="-all")
                subprocess.run([self._program("wineserver"), "-k"], env=environment, capture_output=True, timeout=15, check=False)
                self._wine_prefix = None
            game = dict(game)
            display_settings = DisplaySettings.validate(game.get("_display", {"stream_resolution": "720p"}))
            game["_network"] = self._validated_network(game)
            identifier = str(game.get("id", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier):
                raise ValueError("无效的游戏 ID")
            capabilities = self.capabilities()
            capability = capabilities["platforms"].get(game.get("platform"), {})
            if game.get("platform") == "windows" and game.get("engine_hint") in ("sdlpal", "wine"):
                hint = game["engine_hint"]
                capability = dict(capabilities["engines"][hint], engine="SDLPAL 3.0" if hint == "sdlpal" else "Wine 10")
            if not capability.get("available"):
                raise RuntimeError(capability.get("reason", "这个系统暂不支持云游戏"))
            rom = self._rom(game["rom_path"])
            workspace = self.config_root / "work" / identifier
            workspace.mkdir(parents=True, exist_ok=True)
            content = self._prepare_content(rom, game["platform"], workspace)
            launch_files = game.get("launch_files") or []
            if len(launch_files) > 1 and game["platform"] in ("psx", "segacd", "pcecd", "saturn", "dreamcast"):
                discs = []
                for number, value in enumerate(launch_files):
                    disc = self._rom(value)
                    if any(c in str(disc) for c in ("\r", "\n")):
                        raise ValueError("光盘路径包含不支持的换行字符")
                    discs.append(self._prepare_content(disc, game["platform"], workspace / f"disc-{number}"))
                content = workspace / "discs.m3u"
                content.write_text("\n".join(str(p) for p in discs) + "\n")
            command = self._command(game, content, workspace)
            log_dir = self.config_root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log = log_dir / (identifier + ".log")
            environment = dict(os.environ, DISPLAY=os.getenv("DISPLAY", ":1"), SDL_AUDIODRIVER="pulse", SDL_JOYSTICK_HIDAPI="0")
            cwd = workspace
            if game["platform"] == "switch":
                cwd = self._switch.cwd
            elif game.get("engine_hint") == "sdlpal":
                environment["HOME"] = str(workspace)
            elif game.get("engine_hint") == "wine":
                environment.update(WINEPREFIX=str(self._wine_prefix), WINEARCH="win32", WINEDLLOVERRIDES="mscoree,mshtml=", WINEDEBUG="-all")
                environment.pop("LD_PRELOAD", None)  # bundled gamepad shim is 64-bit; Wine game is 32-bit
                cwd = workspace / "windows"
            with log.open("ab", buffering=0) as output:
                if game.get("engine_hint") == "wine":
                    initialized = self._wine_prefix / ".retroone-ready"
                    if not initialized.exists():
                        subprocess.run([self._program("wineboot"), "-u"], cwd=cwd, env=environment, stdout=output, stderr=subprocess.STDOUT, timeout=180, check=True)
                        initialized.touch()
                applied_display = self._apply_display(display_settings)
                self._process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                threading.Thread(target=self._capture_output, args=(self._process.stdout, log), daemon=True).start()
            self._started = time.monotonic()
            self._session = {"session_id": uuid.uuid4().hex, "game_id": identifier, "title": game.get("title", rom.stem), "platform": game["platform"], "mode": "stream", "play_url": "/stream/", "pid": self._process.pid, "started_at": datetime.now(timezone.utc).isoformat(), "log": log.name, "engine": capability["engine"], "target_fps": 60}
            self._session["display"] = applied_display
            if game.get("_network"):
                self._session["network"] = {key: game["_network"][key] for key in ("room_id", "host_profile_id", "role")}
            # Catch missing ELF dependencies, invalid CLI flags and immediate boot failures.
            try:
                exit_code = self._process.wait(timeout=0.7)
            except subprocess.TimeoutExpired:
                return dict(self._session)
            self.status()
            raise RuntimeError(f"模拟器启动失败（退出码 {exit_code}），请查看 {log.name}")

    def _close_switch_window(self):
        """WM_DELETE only this emulator's process group, so Qt can flush saves."""
        binary = self._program("wmctrl")
        if not binary or not self._process:
            return False
        deadline = time.monotonic() + 10
        try:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    return self._process.returncode == 0
                windows = subprocess.run([binary, "-lp"], capture_output=True, text=True,
                                         timeout=max(0.01, min(2, deadline - time.monotonic())), check=True)
                # Newer modal windows normally follow their parent. A closing
                # dialog can reveal another one, so re-enumerate within one budget.
                for line in reversed(windows.stdout.splitlines()):
                    fields = line.split(None, 4)
                    if len(fields) < 4 or not re.fullmatch(r"0x[0-9a-fA-F]+", fields[0]):
                        continue
                    try:
                        own = os.getpgid(int(fields[2])) == self._process.pid
                    except (ValueError, ProcessLookupError):
                        continue
                    remaining = deadline - time.monotonic()
                    if own and remaining > 0:
                        subprocess.run([binary, "-ic", fields[0]], capture_output=True, timeout=min(2, remaining))
                try:
                    return self._process.wait(timeout=max(0.01, min(0.2, deadline - time.monotonic()))) == 0
                except subprocess.TimeoutExpired:
                    continue
        except (OSError, subprocess.SubprocessError):
            pass
        return False

    def stop(self):
        with self._lock:
            current = self.status()
            if self._wine_prefix:
                environment = dict(os.environ, WINEPREFIX=str(self._wine_prefix), WINEARCH="win32", WINEDEBUG="-all")
                subprocess.run([self._program("wineserver"), "-k"], env=environment, capture_output=True, timeout=15, check=False)
                self._wine_prefix = None
            switch = bool(current and current.get("platform") == "switch")
            graceful = self._close_switch_window() if switch else None
            forced = False
            if self._process and self._process.poll() is None:
                try:
                    os.killpg(self._process.pid, signal.SIGTERM)
                    forced = True
                    self._process.wait(timeout=10)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    os.killpg(self._process.pid, signal.SIGKILL)
                    self._process.wait(timeout=5)
            self.status()
            result = {"stopped": bool(current), "session_id": current["session_id"] if current else None}
            if switch:
                result["graceful"] = graceful
                result["forced"] = forced
                if self.last_exit:
                    self.last_exit["graceful"] = graceful
                    self.last_exit["forced"] = forced
            return result
