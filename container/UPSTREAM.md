# Runtime provenance

All emulation, audio/video encoding, and input transport use existing upstream projects.
No game ROM, BIOS, proprietary firmware, or key is included in the Docker image.

| Component | Upstream | Version / pin | License |
| --- | --- | --- | --- |
| Selkies base | https://github.com/linuxserver/docker-baseimage-selkies | Debian trixie amd64 manifest `a684b614e142b4d1e942b98a2c5232fdaff94c33f89f4a751d97303866afb645` | GPL-3.0 base packaging |
| Selkies | https://github.com/selkies-project/selkies | 2.0.0 bundled in base | MPL-2.0 |
| Pixelflux | https://github.com/selkies-project/pixelflux | 2.1.0 bundled in base | MPL-2.0 |
| RetroArch | https://github.com/libretro/RetroArch | Debian trixie packaged version, recorded by `dpkg-query` | GPL-3.0 |
| Libretro cores | https://buildbot.libretro.com/nightly/linux/x86_64/latest/ | Individual SHA256 in `runtime-lock.json`; preserve vendor cache because upstream nightly URLs rotate | Per-core upstream license, including noncommercial restrictions on some older arcade cores |
| EmulatorJS | https://github.com/EmulatorJS/EmulatorJS | 4.2.3 stable release + SHA256 lock | GPL-3.0 |
| PCSX2 | https://github.com/PCSX2/pcsx2 | 2.8.2 official AppImage + SHA256 lock | GPL-3.0 |
| SDLPAL | https://github.com/sdlpal/sdlpal | 3.0.2025.0616 official AppImage + SHA256 lock | GPL-3.0 |
| Azahar libretro | https://github.com/azahar-emu/azahar | 2126.2 official release + SHA256 lock | GPL-2.0+ |
| Wine | https://www.winehq.org/ | Debian trixie stable 10.0 with 32-bit OpenGL/audio | LGPL-2.1+ |
| DOSBox | https://www.dosbox.com/ | Debian trixie packaged version | GPL-2.0 |
| Dolphin | https://dolphin-emu.org/ | Debian trixie packaged version | GPL-2.0+ |
| MAME | https://www.mamedev.org/ | Debian trixie packaged version | GPL-2.0+, BSD-3-Clause parts |
| PPSSPP assets | https://github.com/hrydgard/ppsspp | Libretro asset bundle SHA256 lock | GPL-2.0+; bundled font licenses retained |

`scripts/fetch_runtime.py` validates cached/downloaded binaries. Initial core selection was
captured on 2026-10-19. A changed upstream nightly file fails verification; updating it
requires explicitly reviewing and changing the lock. Installed Debian packages are recorded
at `/opt/retroone/runtime-packages.tsv` inside the image. Keep the cache or built image for
reproducible restoration. Package versions should be exported with the acceptance report.

The service targets 60 streamed frames per second at 1280×720 or 1920×1080 using VAAPI on Intel,
while preserving each game's original simulation/audio timing. This is not a guarantee
that every title reaches 60 unique game frames or is compatible. Each profile runs one
server game at a time, with up to four virtual gamepads when supported by its emulator. Keyboard and
gamepad traffic follows the Selkies connection; no game disc is sent to the browser.

Per-profile output preferences use the installed upstream `selkies-resize` CLI and
verify the realized X11 geometry using `xrandr --current`. They apply immediately
before a new emulator process starts; saving a preference does not touch an active
session. The new stream iframe then captures that geometry. An already connected
stream must reconnect to adopt a changed encoding size. This does not implement
VPP super-resolution or alter per-emulator internal-resolution options.
`SELKIES_MANUAL_WIDTH/HEIGHT=0`, `SELKIES_MANUAL_RESOLUTION=false|locked` and
`SELKIES_ENABLE_RESIZE=false|locked` keep geometry server-owned: plain `false` is
only an unlocked default in Selkies 2.0. `SELKIES_FRAMERATE=60-60` locks the FPS
range. A stale browser manual-resolution preference cannot override these locks.
`RETROONE_GPU_BACKEND=intel` selects the verified adapter. The `nvidia` adapter is
reserved but explicitly unavailable; selecting it refuses new cloud launches.
It does not silently use the Intel path or claim NVENC support. Future NVIDIA
work must provide driver/device access and verify the actual encoder while
retaining the same per-profile resolution preference format.

Compose starts two instances of the same image. Player 1 retains `./data`; player 2
uses `./profiles/player2`. Their displays, audio servers, emulator processes and save
directories are independent. The player 1 gateway exposes both under `/p/player1/`
and `/p/player2/`; only the gateway publishes host ports. Neither instance receives
the Docker socket. Separate sessions do not by themselves enable game networking:
original game LAN/Ad-hoc support must be configured and tested for each emulator/title.

The verified initial Network title is the library's Ridge Racer 2 / ULJS00080.
Two independent PPSSPP consoles completed a shared race with synchronized positions.
Accepted invitations supply only a room UUID, host profile, role and nickname; the
runner resolves that profile's fixed Docker DNS name and writes isolated ad-hoc
options. Each profile keeps its MAC in `retroone/network/psp-identity.json` and keeps
its own game saves. Solo launches explicitly disable WLAN and the built-in server.
No active game is silently replaced to accept a network launch. This uses PPSSPP's
original PSP ad-hoc protocol, not RetroArch's shared-console netplay.

The queue-only `provisioner` helper creates subsequent members from server-generated
`playerN` IDs, with `/p/playerN/` routing and `./profiles/playerN` storage. It alone
receives the Docker socket; it has no HTTP listener and accepts no user command,
image name, environment, port or filesystem path. The helper runs without Linux
capabilities and with a read-only root filesystem, but Docker access remains a
privileged operation: only trusted deployment operators should edit its code or
environment. Its fixed template never removes or recreates an existing runtime.
`RETROONE_HOST_PROFILES_ROOT` must be the absolute host path corresponding to the
`RETROONE_PROFILES_PATH` bind mount. `RETROONE_DOCKER_GID` must match the host Docker
socket's group (65536 on this Synology). Pending jobs recover after helper restart.
Artwork alone is shared through `/artwork`; `RETROONE_ARTWORK_PATH` and the helper's
`RETROONE_HOST_ARTWORK_ROOT` must refer to the same absolute host cache directory.
No personal configuration or saved-game directory is shared between members.

`prepare-cores.py` applies the reviewed SHA-pinned `melonds_libretro.so` GNU_STACK
metadata fix using Debian patchelf 0.18.0. Only one byte changes from RWE to RW;
upstream assembly lacks `.note.GNU-stack`, and glibc 2.41 otherwise rejects dlopen.
The vendor cache remains pristine. Image builds then independently load all 42
cores and check their libretro ABI; `/opt/retroone/core-load-audit.json` records the
result. Successful loading still does not prove a particular game's compatibility.

Windows entries remain unavailable unless an explicitly supported engine is detected.
PAL98 resources are verified before SDLPAL is selected. Wine only exposes individually
verified executable/resource combinations, using isolated writable prefixes and game
copies; the original Windows launch scripts are not executed. Switch/PS3/Xbox360 and other
unconfigured engines are reported as missing; cataloging an entry never proves playability.

Back up the entire persistent `/config` bind mount, including `retroone/work/`: DOS
and Wine games may store their native saves beside their writable game files. The
runner never cleans these working directories automatically. PCSX2 memory cards,
3DS title-ID saves, emulator configurations and browser uploads also live in `/config`.
Back up all profile mounts, the registry and its profile-control queue. Additional
member containers are managed by the provisioner, not listed as static Compose
services; stopping Compose alone does not stop those additional game sessions.
