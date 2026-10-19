# RetroOne integration contract

This project is a LAN-only launcher and library migration tool, reusing EmulatorJS,
RetroArch and its upstream cores, native emulators, and Selkies streaming. Do not
implement emulators or streaming codecs. The verified deployment uses x86-64
Linux with Intel Iris Xe exposed as `/dev/dri/renderD128`; macOS can host UI and
API development. NVIDIA has an adapter interface but is not yet implemented.

ROM collections, firmware and saved games are supplied and retained by the
operator. Migration must preserve sources, variants, multi-disc dependencies and
existing collection metadata. The repository and public releases contain no game
ROMs, firmware, keys, commercial cover art or commercial gameplay screenshots.

## Directory contract

Container ROM_ROOT=/roms is a read-only bind mount of `one`.
`one/library/<platform>/<source>/<collection>/` retains each collection's internal
structure, metadata.pegasus.txt and media/<game>/boxFront.jpg/logo.png/video.mp4.
`one/archive/` preserves auxiliary programs and otherwise unclassified data.
`one/firmware/` holds runtime firmware copies. `/config/retroone/` holds the SQLite
catalog, saved games, runtime working copies, logs and settings, outside ROM_ROOT.
`retroone/platforms.py` contains shared platform definitions and aliases.

## Backend/frontend HTTP contract

- Profile-scoped routes below use /p/playerN; returned URLs include that prefix.
- Global GET /api/profiles, POST /api/profiles and PATCH /api/profiles/{id} manage names and avatars. Creating a member queues an isolated worker; report provisioning/error/ready honestly. POST /api/profiles/{id}/retry retries the same member after a provisioning error.
- Global POST /api/presence and /api/presence/leave track each browser tab. Online status expires after 75 seconds without a heartbeat; a running emulator alone does not imply an online person.
- GET /api/health: service/runtime readiness.
- GET /api/platforms: {platforms:[{id,name,count,browser_count,stream_count}]}.
- GET /api/games?q=&platform=&mode=&favorite=&offset=0&limit=60:
  {games:[Game],total,offset,limit}; Game fields: id,title,platform,platform_name,
  collection,size,unpacked_size,mode (browser|stream|unavailable),browser_core,
  description,cover_url,video_url,reason,favorite. Optional recent/featured metadata.
- GET /api/games/{id}: Game.
- POST /api/games/{id}/favorite: {favorite:bool} -> updated favorite.
- POST /api/games/{id}/launch: {mode?:"browser"|"stream",replace_session_id?:string,network_room_id?:UUID} ->
  {mode,play_url,session_id?}. Browser uses /play/{id}; streaming uses /stream/.
  Enforce size threshold + supported core on server; never send large ROMs to clients.
  A conflicting cloud game returns 409 with the current session. After explicit confirmation, replace_session_id must still match under the runtime lock before stopping it. A stale confirmation never stops a newer session. Network launches cannot replace an existing session.
- GET /api/session: active session (or null), game and runtime readiness.
- GET/PATCH /api/settings/display: per-worker persistent `{stream_resolution:"720p"|"1080p"}` preference. Responses distinguish saved `settings`, supported `capabilities`, verified `active` session output dimensions, `active_unknown`, and `pending`; `applies_on` is `next_launch`. Saving does not resize or stop a live game. Browser-local emulation is outside this setting's scope.
- POST /api/session/stop: {expected_session_id?:string}; cleanly terminate only the managed emulator session. The UI confirms the named game and sends its session ID; a changed session returns 409 and requires a fresh confirmation. No active session is an idempotent success.
- Global GET /api/network/capabilities lists only individually validated Network games. GET /api/network/invites?profile_id=playerN and POST /api/network/invites manage invitations. POST /api/network/invites/{id}/accept, /decline and /cancel take {profile_id}; POST /api/network/rooms/{id}/close closes room metadata without stopping games. Accepting an invitation creates a room, not a running game: each member launches independently with network_room_id and follows the game's original multiplayer menu.
- POST /api/scan: asynchronous scan; status through health, avoid blocking HTTP.
- GET /api/migration: read-only migration progress summary, if available.
- Browser page /play/{id} embeds locally hosted EmulatorJS, path /emulatorjs/data/.
- Saves persist (browser local + export at minimum; prefer server backup integration).

Frontend files: web/index.html, web/app.js, web/styles.css. Vanilla JS is suitable;
no required framework. Chinese PSP/XMB-inspired platform horizontal navigation,
game cards + detail panel, search, favorites, keyboard/gamepad, responsive layout.
No fabricated game data or covers. Graceful empty/loading/error states.

## Native runner contract

`retroone/runner.py` implements the native process lifecycle. Export
`Runner(config_root: Path, rom_root: Path)`, `.capabilities()` JSONable dict,
`.start(game: dict)` JSONable session, `.status()`, `.stop()`. Game internal fields
include absolute `rom_path`, `platform`, `id`, `title`, and optional launch metadata.
Ensure paths remain within ROM_ROOT; no shell commands derived from filenames.
Run one server session per family profile, reject a second launch in that profile without stopping the first. Each profile has its own container, display, audio and entire /config tree; the first profile retains ./data for existing saves. Other profiles use ./profiles/playerN. A gateway exposes /p/playerN/ on the same origin. Never pass the Docker socket to the web or game containers.
Allow two/four gamepads within a game if supported. 60 fps target, natural game speed.
Do not expose arbitrary shell execution or unrelated host files in the web API.

For a new cloud session, the API reads the profile's validated display preference into server-owned `game['_display']`; it never accepts emulator commands or dimensions through LaunchBody. The runner applies the preset using the installed Selkies resize tool and verifies the real desktop dimensions before reporting `session.display={settings,width,height}`. Resuming an existing session does not reapply saved preferences. Client-side desktop resizing is locked so previous browser preferences cannot override a player's chosen output. A newly opened stream uses the resized desktop; existing stream connections must reconnect to adopt changed encoder dimensions. This is stream output sizing, not an implementation of AI super-resolution or per-emulator internal resolution settings.

The verified deployment path is Selkies resize → X11 desktop geometry → Pixelflux DRI3 capture → Intel VAAPI H.264 encoding. `retroone/display_runtime.py` owns the vendor-neutral adapter interface (`capabilities`, `apply`); `RETROONE_GPU_BACKEND=intel` is the default. Selecting `nvidia` currently selects an explicit unavailable adapter and refuses a new cloud launch without executing the Intel resize path. Unknown values also fail closed. The saved `720p`/`1080p` preference is GPU-vendor-neutral: a future NVIDIA deployment should retain profile settings and this API schema. NVIDIA driver/device passthrough, container runtime access, capture/encoder selection and actual NVENC output must be configured and tested on that host separately. NVIDIA compatibility and performance are not established by the current Intel acceptance; no NVIDIA host is provisioned by this change.

## Components

- `retroone/api.py`, `catalog.py`: HTTP interface and collection index.
- `retroone/profiles.py`, `presence.py`, `network.py`: family profiles, presence and invitations.
- `web/`: browser interface and emulator integration.
- `retroone/runner.py`, `container/`: emulator lifecycle and streaming integration.
- `scripts/`: local collection migration, artwork preparation and runtime verification.

Every completion claim must distinguish tested playability, missing dependencies,
and catalog-only entries. Never claim all games work based only on their presence.

## Multiplayer capability contract

Keep independent play, Same Console and Network separate. Same Console means multiple controllers for one emulated console, including remotely forwarded input or synchronized copies of that console. Network means independent emulated consoles communicating through the original game's LAN/Ad-hoc/wireless/link protocol. Multiple processes, a signaling connection, or two browser streams do not prove Network support.

The normal <=20 MiB browser rule does not prove network capability. Check the installed core build and transport, then verify two independent players join the same in-game session and control their own roles. Current browser builds have no validated Network support. PPSSPP Ad-hoc has passed a two-instance race in PSP Ridge Racer 2 (ULJS00080); this is the only initial invitation whitelist entry. Do not expose a generic Network label for every game on a platform. The coordinator chooses room membership and host role; workers derive their private address and unique MAC internally. Never accept an arbitrary address or emulator option from the browser.
