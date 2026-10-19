"""Eden adapter: read-only console assets, private portable data for each member.

Readiness checks file structure only, never imply decryption or game compatibility.
No console assets or emulator executable are downloaded by this module.
"""
from __future__ import annotations

import configparser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile


ENGINE = "Eden 0.2.1"
FORMATS = {".nsp", ".xci", ".nro"}
KEY_LIMIT = 2 * 1024**2
FIRMWARE_LIMIT = 4 * 1024**3
FIRMWARE_FILE_LIMIT = 4096
# Pinned Selkies SDL 2.32.4 GUID, with bytes 2..3 cleared as Eden's GetGUID does.
SELKIES_GUID = "030000005e0400008e02000014010000"


def controller_settings():
    prefix = f"engine:sdl,port:0,guid:{SELKIES_GUID},"
    # Nintendo button positions: B south, A east, Y west, X north.
    buttons = {"a": 1, "b": 0, "x": 3, "y": 2, "l": 4, "r": 5,
               "minus": 6, "plus": 7, "home": 8, "lstick": 9, "rstick": 10}
    values = {"player_0_connected": "true", "player_0_type": "0"}
    for key, button in buttons.items():
        values[f"player_0_button_{key}"] = json.dumps(prefix + f"button:{button}")
    for key, direction in {"dup": "up", "ddown": "down", "dleft": "left", "dright": "right"}.items():
        values[f"player_0_button_{key}"] = json.dumps(prefix + f"hat:0,direction:{direction}")
    for key, axis in {"zl": 2, "zr": 5}.items():
        values[f"player_0_button_{key}"] = json.dumps(prefix + f"axis:{axis},threshold:0.5,invert:+")
    for key, axes in {"lstick": (0, 1), "rstick": (3, 4)}.items():
        values[f"player_0_{key}"] = json.dumps(prefix + f"axis_x:{axes[0]},axis_y:{axes[1]},offset_x:0,offset_y:0,invert_x:+,invert_y:+,deadzone:0.15,range:1")
    for player in range(1, 8):
        values[f"player_{player}_connected"] = "false"
    return values


class SwitchRuntime:
    def __init__(self, config_root: Path, rom_root: Path, program):
        self.config_root = Path(config_root).resolve()
        self.rom_root = Path(rom_root).resolve()
        self.source = self.rom_root / "firmware" / "switch"
        self.cwd = self.config_root / "switch"
        self.user = self.cwd / "user"
        self.program = program

    def _source_file(self, path: Path, maximum: int):
        if not path.resolve().is_relative_to(self.source.resolve()) or not path.resolve().is_relative_to(self.rom_root):
            raise ValueError("Switch 固件或密钥链接超出了指定目录")
        if not path.is_file() or not 0 < path.stat().st_size <= maximum:
            raise ValueError("Switch 固件或密钥文件为空或大小异常")
        return path

    def _keys(self):
        path = self._source_file(self.source / "prod.keys", KEY_LIMIT)
        text = path.read_text(encoding="utf-8-sig")
        # A filename alone is not enough; do not expose or log any key material.
        header = re.search(r"(?mi)^\s*header_key\s*=\s*([0-9a-f]{64})\s*(?:#.*)?$", text)
        if not header:
            raise ValueError("prod.keys 格式不正确或缺少 header_key；请使用自己导出的完整密钥文件")
        return path

    @staticmethod
    def _nca_name(name):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
            raise ValueError("Switch 固件压缩包包含不安全的路径")
        if not re.fullmatch(r"[0-9a-fA-F]{32}\.nca", path.name):
            raise ValueError("Switch 固件应包含以内容 ID 命名的 .nca 文件")
        return path.name.lower()

    def _firmware(self):
        """Return bounded metadata, not decrypted firmware or any key values."""
        archive = self.source / "firmware.zip"
        loose = self.source / "registered"
        items = []
        if loose.is_dir():
            if not loose.resolve().is_relative_to(self.source.resolve()) or not loose.resolve().is_relative_to(self.rom_root):
                raise ValueError("Switch 固件目录链接超出了指定目录")
            for path in loose.rglob("*"):
                if path.is_file():
                    self._source_file(path, FIRMWARE_LIMIT)
                    name = self._nca_name(path.relative_to(loose).as_posix())
                    items.append((name, path, path.stat().st_size, path.stat().st_mtime_ns))
                    if len(items) > FIRMWARE_FILE_LIMIT:
                        raise ValueError("Switch 固件文件数量超过检查上限")
            source = loose
            kind = "directory"
        elif archive.is_file():
            self._source_file(archive, FIRMWARE_LIMIT)
            with zipfile.ZipFile(archive) as zipped:
                if len(zipped.infolist()) > FIRMWARE_FILE_LIMIT * 2:
                    raise ValueError("Switch 固件压缩包目录过大")
                for entry in zipped.infolist():
                    if entry.is_dir():
                        continue
                    name = self._nca_name(entry.filename)
                    if entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                        raise ValueError("Switch 固件压缩包包含加密条目或链接")
                    items.append((name, entry.filename, entry.file_size, entry.CRC))
            source = archive
            kind = "zip"
        else:
            raise ValueError("缺少 Switch 固件：请放入 one/firmware/switch/firmware.zip 或 registered/ 下的 NCA 文件")
        if not items or len(items) > FIRMWARE_FILE_LIMIT or any(not 0 < item[2] <= FIRMWARE_LIMIT for item in items):
            raise ValueError("Switch 固件为空或文件大小异常")
        if sum(item[2] for item in items) > FIRMWARE_LIMIT or len({item[0] for item in items}) != len(items):
            raise ValueError("Switch 固件过大或包含重名 NCA，需先检查")
        identity = [kind, str(source), source.stat().st_size, source.stat().st_mtime_ns,
                    [(name, str(member), size, stamp) for name, member, size, stamp in sorted(items)]]
        return {"kind": kind, "source": source, "items": items,
                "identity": hashlib.sha256(json.dumps(identity).encode()).hexdigest()}

    def readiness(self):
        installed = bool(self.program("retroone-eden"))
        keys_ok = firmware_ok = False
        reasons = []
        if not installed:
            reasons.append("尚未安装 Eden；请使用包含 Switch 运行组件的本地完整镜像")
        if not (self.source / "prod.keys").is_file():
            reasons.append("缺少 one/firmware/switch/prod.keys")
        else:
            try:
                self._keys()
                keys_ok = True
            except (OSError, UnicodeError, ValueError):
                reasons.append("Switch prod.keys 无法读取或格式不正确")
        count = 0
        try:
            firmware = self._firmware()
            count = len(firmware["items"])
            firmware_ok = True
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            reasons.append(str(error) if isinstance(error, ValueError) else "Switch 固件无法读取或压缩包损坏")
        ready = installed and keys_ok and firmware_ok
        return {"available": ready, "engine": ENGINE, "experimental": True,
                "reason": "；".join(reasons) if reasons else "实验支持：云端 Eden；固件与密钥只通过结构检查，兼容性和性能需逐款验证",
                "requires_bios": True, "verified_playable": False,
                "checks": {"emulator_installed": installed, "keys_structure": keys_ok,
                           "firmware_structure": firmware_ok, "firmware_files": count},
                "supported_formats": sorted(FORMATS)}

    def _directory(self, path):
        if not path.resolve().is_relative_to(self.config_root):
            raise ValueError("Switch 玩家目录链接超出当前玩家的存档目录")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write(self, path, data, mode=0o600):
        self._directory(path.parent)
        if path.is_symlink():
            raise ValueError("Switch 配置文件不能是符号链接")
        if path.is_file() and path.read_bytes() == data:
            path.chmod(mode)
            return
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".retroone-", delete=False) as output:
            temp = Path(output.name)
            try:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                temp.chmod(mode)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)

    def _prepare_firmware(self, firmware):
        # Keep each firmware generation immutable. Switching generations never
        # overwrites a member's NAND, saves, or a previous firmware installation.
        generations = self._directory(self.user / "firmware-generations")
        generation = generations / firmware["identity"]
        complete = generation / ".complete"
        if generation.is_symlink() or complete.is_symlink():
            raise ValueError("Switch 固件缓存不能是外部链接")
        if not complete.is_file():
            total = sum(item[2] for item in firmware["items"])
            if firmware["kind"] == "zip" and total > shutil.disk_usage(generations).free - 1024**3:
                raise RuntimeError("没有足够空间准备 Switch 固件（保留 1 GiB 余量）")
            if generation.exists():
                raise RuntimeError("Switch 固件存在未完成的安装目录，已保留，请检查后重试")
            with tempfile.TemporaryDirectory(dir=generations, prefix=".preparing-") as temporary:
                stage = Path(temporary)
                if firmware["kind"] == "zip":
                    with zipfile.ZipFile(firmware["source"]) as zipped:
                        for name, member, size, crc in firmware["items"]:
                            info = zipped.getinfo(member)
                            if info.file_size != size or info.CRC != crc:
                                raise RuntimeError("Switch 固件在准备过程中发生变化，请重试")
                            with zipped.open(member) as source, (stage / name).open("wb") as output:
                                remaining = size
                                while remaining:
                                    chunk = source.read(min(1024**2, remaining))
                                    if not chunk:
                                        raise RuntimeError("Switch 固件文件不完整")
                                    output.write(chunk)
                                    remaining -= len(chunk)
                                if source.read(1):
                                    raise RuntimeError("Switch 固件文件大小不匹配")
                else:
                    for name, path, _, _ in firmware["items"]:
                        (stage / name).symlink_to(path)
                (stage / ".complete").write_text(firmware["identity"])
                os.rename(stage, generation)
        elif complete.read_text() != firmware["identity"]:
            raise RuntimeError("Switch 固件缓存校验标记异常，已保留，请检查")
        registered = self._directory(self.user / "nand" / "system" / "Contents") / "registered"
        if registered.is_symlink():
            if not registered.resolve().is_relative_to(generations.resolve()):
                raise RuntimeError("Switch 已有外部固件目录，已保留；请先检查当前玩家的 registered 链接")
            temporary = registered.with_name(".retroone-registered")
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeError("Switch 固件切换存在未完成的临时链接")
            temporary.symlink_to(generation)
            os.replace(temporary, registered)
        elif registered.exists():
            raise RuntimeError("Switch 已有独立安装的固件，已保留；请先检查当前玩家的 registered 目录")
        else:
            registered.symlink_to(generation)

    def _configure(self):
        path = self.user / "config" / "qt-config.ini"
        self._directory(path.parent)
        if path.is_symlink():
            raise ValueError("Switch 配置文件不能是符号链接")
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        if path.is_file():
            try:
                parser.read_string(path.read_text())
            except (configparser.Error, UnicodeError) as error:
                raise RuntimeError("Switch 现有配置无法读取，已保留，请先检查") from error
        defaults = {
            "Renderer": {"backend": "1", "resolution_setup": "3", "fullscreen_mode": "0"},
            "System": {"use_docked_mode": "0", "current_user": "0"},
            "Core": {"use_speed_limit": "true", "speed_limit": "100"},
        }
        for section, values in defaults.items():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in values.items():
                if key not in parser[section]:
                    parser[section][key] = value
                    parser[section][key + "\\default"] = "false"
        managed = {"Controls": controller_settings(), "UI": {
            "confirmStop": "2", "firstStart": "false", "check_for_updates": "false",
            "select_user_on_boot": "false", "fullscreen": "true",
        }}
        for section, values in managed.items():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in values.items():
                parser[section][key] = value
                parser[section][key + "\\default"] = "false"
        output = io.StringIO()
        parser.write(output, space_around_delimiters=False)
        self._write(path, output.getvalue().encode())

    def prepare(self, content: Path):
        if content.suffix.lower() not in FORMATS:
            raise ValueError("Switch 当前支持 NSP / XCI / NRO；NSZ / XCZ 需先转换，更新与 DLC 不能作为本体启动")
        ready = self.readiness()
        if not ready["available"]:
            raise RuntimeError(ready["reason"])
        self._directory(self.user)
        self._write(self.user / "keys" / "prod.keys", self._keys().read_bytes())
        title = self.source / "title.keys"
        if title.exists():
            self._write(self.user / "keys" / "title.keys", self._source_file(title, KEY_LIMIT).read_bytes())
        self._prepare_firmware(self._firmware())
        self._configure()
        return [self.program("retroone-eden"), "-f", "-u", "0", "-g", str(content)]
