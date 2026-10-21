"""Bounded HTTP and gamepad adapter for the installed Selkies 2.0 protocol.

This intentionally never sends SETTINGS, keyboard, mouse, shell, or video-start
messages. Selkies has no gamepad acknowledgement: ``submitted`` means frames
were written, not that an emulator accepted them. Session polling is a guard,
not an atomic binding between RetroOne sessions and the Selkies desktop.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import time
from contextlib import suppress
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from PIL import Image, UnidentifiedImageError


BUTTONS = {name: index for index, name in enumerate(
    ("a", "b", "x", "y", "lb", "rb", "lt", "rt", "back", "start",
     "l3", "r3", "up", "down", "left", "right"))}
AXES = {"lx": 0, "ly": 1, "rx": 2, "ry": 3}
MAX_ACTION_MS = 5000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_SCREENSHOT_PIXELS = 33_177_600  # 8K; reject unexpected/decompression-bomb images.


class RuntimeFailure(Exception):
    """A safe, public error that contains no upstream body, URL, or secret."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class RuntimeClient:
    def __init__(self, profiles: dict[str, dict], timeout: float = 10.0):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.profiles = profiles
        self.timeout = float(timeout)
        # Emulator preparation can outlast ordinary reads, especially just after
        # a worker starts. This does not extend input or screenshot deadlines.
        self.launch_timeout = 120.0
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_connect: dict[str, float] = {}
        # Installed Selkies rejects connections less than 500 ms apart by IP.
        self._reconnect_interval = 0.55
        self._poll_interval = 0.1

    async def close(self) -> None:
        """Requests own their sessions; no background connection survives a call."""

    def _profile(self, profile: str) -> dict:
        config = self.profiles.get(profile)
        if not isinstance(config, dict):
            raise RuntimeFailure("unknown_profile", "The selected profile is not configured.")
        return config

    @staticmethod
    def _base(config: dict, key: str) -> str:
        value = config.get(key)
        try:
            parsed = urlsplit(value) if isinstance(value, str) else None
            valid = (parsed and parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeFailure("runtime_configuration", "The runtime endpoint is not configured correctly.")
        return value.rstrip("/")

    @staticmethod
    def _token(config: dict, slot: int | None = None) -> str | None:
        tokens = config.get("selkies_tokens", {})
        if not isinstance(tokens, dict):
            raise RuntimeFailure("runtime_configuration", "Controller credentials are not configured correctly.")
        value = config.get("selkies_token")
        if slot is not None:
            value = tokens.get(str(slot), tokens.get(slot, value))
        elif not value and tokens:
            value = next(iter(tokens.values()))
        if value is not None and (not isinstance(value, str) or not value):
            raise RuntimeFailure("runtime_configuration", "Controller credentials are not configured correctly.")
        return value

    @staticmethod
    def _http_error(status: int) -> RuntimeFailure:
        if status in (401, 403):
            return RuntimeFailure("runtime_auth_failed", "The runtime refused the configured credentials.")
        if status == 409:
            return RuntimeFailure("session_conflict", "The runtime state conflicts with this request.")
        if status == 404:
            return RuntimeFailure("runtime_not_found", "The requested runtime resource was not found.")
        if status == 429:
            return RuntimeFailure("runtime_busy", "The runtime is temporarily rate limited.")
        return RuntimeFailure("runtime_unavailable", "The runtime could not complete the request.")

    async def _request(self, method: str, url: str, *, params: dict | None = None,
                       payload: dict | None = None, token: str | None = None,
                       raw: bool = False, timeout: float | None = None) -> Any:
        headers = {"Authorization": "Bearer " + token} if token else {}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout if timeout is None else timeout),
                                             trust_env=False) as session:
                async with session.request(method, url, params=params, json=payload,
                                           headers=headers, allow_redirects=False) as response:
                    if not 200 <= response.status < 300:
                        raise self._http_error(response.status)
                    chunks = []
                    size = 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise RuntimeFailure("runtime_response_invalid", "The runtime response exceeded its size limit.")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if raw:
                        return data
                    value = json.loads(data)
                    if not isinstance(value, dict):
                        raise ValueError("object required")
                    return value
        except RuntimeFailure:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise RuntimeFailure("runtime_timeout", "The runtime request timed out.") from None
        except (aiohttp.ClientError, OSError):
            raise RuntimeFailure("runtime_unavailable", "The runtime connection failed.") from None
        except (ValueError, UnicodeError):
            raise RuntimeFailure("runtime_response_invalid", "The runtime returned an invalid response.") from None

    async def status(self, profile: str) -> dict:
        return await self._request("GET", self._base(self._profile(profile), "api_url") + "/api/session")

    async def create_profile(self, gateway: str, name: str, avatar: str, request_id: str) -> dict:
        return await self._request("POST", gateway + "/api/profiles",
                                   payload={"name": name, "avatar": avatar, "request_id": request_id})

    async def list_profiles(self, gateway: str) -> dict:
        return await self._request("GET", gateway + "/api/profiles")

    async def games(self, profile: str, q: str = "", platform: str = "",
                    limit: int = 60, offset: int = 0) -> dict:
        return await self._request("GET", self._base(self._profile(profile), "api_url") + "/api/games",
                                   params={"q": q, "platform": platform, "limit": limit, "offset": offset})

    async def launch(self, profile: str, game_id: str, mode: str,
                     replace_session_id: str | None = None) -> dict:
        if mode != "stream":
            raise RuntimeFailure("unsupported_mode", "Agent control requires a server streaming session.")
        body = {"mode": mode}
        if replace_session_id is not None:
            body["replace_session_id"] = replace_session_id
        try:
            return await self._request("POST", self._base(self._profile(profile), "api_url")
                                       + "/api/games/" + quote(game_id, safe="") + "/launch",
                                       payload=body, timeout=self.launch_timeout)
        except RuntimeFailure as error:
            if error.code == "runtime_timeout":
                raise RuntimeFailure("runtime_timeout", "Launch timed out. The game may still be starting; inspect sessions before submitting another launch.") from None
            raise

    async def stop(self, profile: str, expected_session_id: str) -> dict:
        self._require_session_id(expected_session_id)
        return await self._request("POST", self._base(self._profile(profile), "api_url") + "/api/session/stop",
                                   payload={"expected_session_id": expected_session_id})

    async def screenshot(self, profile: str, max_width: int = 1280) -> tuple[bytes, str, int, int]:
        if type(max_width) is not int or not 1 <= max_width <= 7680:
            raise RuntimeFailure("invalid_argument", "Screenshot width must be between 1 and 7680.")
        config = self._profile(profile)
        data = await self._request("GET", self._base(config, "selkies_url") + "/api/screenshot",
                                   params={"display": "primary"}, token=self._token(config), raw=True)
        return await asyncio.to_thread(self._resize, data, max_width)

    @staticmethod
    def _resize(data: bytes, max_width: int) -> tuple[bytes, str, int, int]:
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.width * image.height > MAX_SCREENSHOT_PIXELS:
                    raise ValueError("invalid image")
                image.load()
                if image.width > max_width:
                    height = max(1, round(image.height * max_width / image.width))
                    image = image.resize((max_width, height), Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    data = output.getvalue()
                return data, "image/png", image.width, image.height
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            raise RuntimeFailure("runtime_response_invalid", "The runtime did not return a valid screenshot.") from None

    @staticmethod
    def _require_session_id(value: str) -> None:
        if not isinstance(value, str) or not value or len(value) > 128:
            raise RuntimeFailure("invalid_argument", "An expected session ID is required.")

    async def _assert_session(self, profile: str, expected: str) -> None:
        current = (await self.status(profile)).get("session")
        if not isinstance(current, dict) or (current.get("id") or current.get("session_id")) != expected:
            raise RuntimeFailure("session_conflict", "The active game session changed; input was stopped.")

    @staticmethod
    def _permissions(message: str, slot: int) -> None:
        try:
            permissions = json.loads(message.split(",", 1)[1])
            if (type(permissions.get("slot")) is not int or permissions["slot"] != slot
                    or permissions.get("role") not in ("viewer", "controller")):
                raise ValueError("slot mismatch")
        except (ValueError, AttributeError, KeyError):
            raise RuntimeFailure("controller_slot_denied", "The controller credential does not grant this slot.") from None

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> str | None:
        message = await ws.receive()
        if message.type == aiohttp.WSMsgType.TEXT:
            if message.data.startswith("KILL "):
                raise RuntimeFailure("runtime_input_unavailable", "The runtime refused this controller connection.")
            return message.data
        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            raise RuntimeFailure("runtime_disconnected", "The controller connection closed; input was stopped.")
        return None

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse, slot: int, secure: bool) -> None:
        authenticated = not secure
        mode = False
        settings = False
        async with asyncio.timeout(self.timeout):
            while not (authenticated and mode and settings):
                text = await self._receive(ws)
                if not text:
                    continue
                if text.startswith("AUTH_SUCCESS,"):
                    self._permissions(text, slot)
                    authenticated = True
                elif text.startswith("MODE "):
                    if text != "MODE websockets":
                        raise RuntimeFailure("runtime_input_unavailable", "The configured input transport is not active.")
                    mode = True
                elif text.startswith("{"):
                    try:
                        value = json.loads(text)
                        if value.get("type") == "server_settings":
                            enabled = value.get("settings", {}).get("gamepad_enabled", {})
                            if not isinstance(enabled, dict) or enabled.get("value") is not True:
                                raise RuntimeFailure("runtime_input_unavailable", "Gamepad input is not enabled on the runtime.")
                            settings = True
                    except (ValueError, AttributeError):
                        raise RuntimeFailure("runtime_response_invalid", "The runtime input handshake was invalid.") from None

    async def _watch_connection(self, ws: aiohttp.ClientWebSocketResponse, slot: int) -> None:
        while True:
            text = await self._receive(ws)
            if text and text.startswith(("ROLE_UPDATE,", "AUTH_SUCCESS,")):
                self._permissions(text, slot)

    async def _watch_session(self, profile: str, expected: str) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            await self._assert_session(profile, expected)

    async def _cleanup(self, ws: aiohttp.ClientWebSocketResponse, index: int, associated: bool) -> bool:
        released = False
        try:
            async with asyncio.timeout(min(self.timeout, 2.0)):
                if associated and not ws.closed:
                    # Explicit zeros plus disassociation. The latter also resets any
                    # state recorded by Selkies before an interrupted send.
                    for button in BUTTONS.values():
                        await ws.send_str(f"js,b,{index},{button},0")
                    for axis in AXES.values():
                        await ws.send_str(f"js,a,{index},{axis},0")
                    await ws.send_str(f"js,d,{index}")
                    released = True
        except (aiohttp.ClientError, OSError, RuntimeError, TimeoutError):
            pass
        finally:
            with suppress(aiohttp.ClientError, OSError, RuntimeError, TimeoutError):
                async with asyncio.timeout(min(self.timeout, 2.0)):
                    await ws.close()
        return released

    async def perform(self, profile: str, slot: int, buttons: list[str], axes: dict,
                      duration_ms: int, expected_session_id: str) -> dict:
        self._require_session_id(expected_session_id)
        if (type(slot) is not int or not 1 <= slot <= 4
                or type(duration_ms) is not int or not 0 <= duration_ms <= MAX_ACTION_MS
                or not isinstance(buttons, list) or any(not isinstance(b, str) or b not in BUTTONS for b in buttons)
                or len(buttons) != len(set(buttons)) or not isinstance(axes, dict)
                or any(k not in AXES or type(v) not in (int, float) or not -1 <= v <= 1 or not math.isfinite(v)
                       for k, v in axes.items())):
            raise RuntimeFailure("invalid_argument", "The controller action contains an invalid slot, control, value, or duration.")
        config = self._profile(profile)
        endpoint = self._base(config, "selkies_url")
        token = self._token(config, slot)
        if not token and config.get("allow_legacy_input") is not True:
            raise RuntimeFailure("runtime_configuration", "A slot-scoped controller credential is required.")
        # The legacy exception is for explicitly configured isolated workers only;
        # legacy player 1 has no server-side slot restriction.
        query = {"token": token} if token else ({"role": "viewer", "slot": str(slot)} if slot > 1 else {})
        async with self._locks.setdefault(endpoint, asyncio.Lock()):
            delay = self._reconnect_interval - (time.monotonic() - self._last_connect.get(endpoint, -math.inf))
            if delay > 0:
                await asyncio.sleep(delay)
            await self._assert_session(profile, expected_session_id)
            return await self._perform_connected(profile, slot, buttons, axes, duration_ms,
                                                 expected_session_id, endpoint, query, bool(token))

    async def _perform_connected(self, profile: str, slot: int, buttons: list[str], axes: dict,
                                 duration_ms: int, expected: str, endpoint: str, query: dict,
                                 secure: bool) -> dict:
        index = slot - 1
        submitted = 0
        associated = False
        ws = None
        tasks: list[asyncio.Task] = []
        started = time.monotonic()
        released = False
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout), trust_env=False) as client:
                try:
                    self._last_connect[endpoint] = time.monotonic()
                    ws = await client.ws_connect(endpoint + "/api/websockets", params=query,
                                                 heartbeat=10, max_msg_size=4 * 1024 * 1024)
                    await self._handshake(ws, slot, secure)
                    await self._assert_session(profile, expected)
                    name = base64.b64encode(b"RetroOne Agent Standard Gamepad").decode("ascii")
                    # Mark before sending: cancellation may happen after the frame
                    # has reached the socket but before send_str returns.
                    associated = True
                    await asyncio.wait_for(ws.send_str(f"js,c,{index},{name},4,17"), self.timeout)
                    started = time.monotonic()

                    async def submit_and_hold() -> None:
                        nonlocal submitted
                        # Start the server's 2 s stale-state watchdog before any
                        # press; it also recovers silent TCP loss without a close.
                        await ws.send_str(f"js,h,{index}")
                        for button in buttons:
                            await ws.send_str(f"js,b,{index},{BUTTONS[button]},1")
                            submitted += 1
                        for axis, value in axes.items():
                            await ws.send_str(f"js,a,{index},{AXES[axis]},{float(value):.8g}")
                            submitted += 1
                        while True:
                            await ws.send_str(f"js,h,{index}")
                            await asyncio.sleep(0.25)

                    timer = asyncio.create_task(asyncio.sleep(duration_ms / 1000))
                    tasks = [asyncio.create_task(submit_and_hold()),
                             asyncio.create_task(self._watch_session(profile, expected)),
                             asyncio.create_task(self._watch_connection(ws, slot)), timer]
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    # Prefer an observed failure over a simultaneous timer expiry.
                    for task in done:
                        if task is not timer:
                            task.result()
                finally:
                    for task in tasks:
                        task.cancel()

                    async def finish() -> bool:
                        if tasks:
                            await asyncio.gather(*tasks, return_exceptions=True)
                        return await self._cleanup(ws, index, associated) if ws is not None else False

                    cleanup = asyncio.create_task(finish())
                    cancelled_during_cleanup = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            # Finish bounded release even if cancellation arrives
                            # while already unwinding another cancellation.
                            cancelled_during_cleanup = True
                    released = cleanup.result()
                    if cancelled_during_cleanup:
                        raise asyncio.CancelledError
            elapsed = round((time.monotonic() - started) * 1000)
            return {"delivery": "submitted", "submitted_messages": submitted,
                    "duration_ms": duration_ms, "elapsed_ms": elapsed, "slot": slot,
                    "session_id": expected, "neutralization_submitted": released,
                    "game_effect_confirmed": False}
        except RuntimeFailure:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise RuntimeFailure("runtime_timeout", "The controller operation timed out; input was stopped.") from None
        except (aiohttp.ClientError, OSError, RuntimeError):
            raise RuntimeFailure("runtime_unavailable", "The controller connection failed; input was stopped.") from None
