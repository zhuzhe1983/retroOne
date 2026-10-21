"""Read-only family viewing gateway for the optional MCP sidecar.

Family profiles are labels on a trusted LAN, not authenticated identities. A
watch grant only selects one existing session; it is never an MCP credential.
The upstream URL/role are server-owned and all client-to-Selkies traffic is
restricted independently of the upstream's own viewer permissions.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import html
import inspect
import json
import re
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect

_GRANT = re.compile(r"[A-Za-z0-9_-]{24,160}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ASSET = re.compile(r"(?:assets/[A-Za-z0-9_-]+\.(?:js|css|wasm)|(?:icon(?:-512)?\.png|favicon\.ico|selkies\.svg))\Z")
_HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp", "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), gamepad=(), clipboard-read=(), clipboard-write=()",
}
# STOP_AUDIO is deliberately absent: Selkies 2.0 stops the shared audio capture
# globally. SETTINGS, input, clipboard, microphone, files, and all binary client
# frames are also forbidden. Pause/resume video affects this viewer only.
_VIEWER_MESSAGES = frozenset({"START_VIDEO", "STOP_VIDEO", "START_AUDIO", "REQUEST_KEYFRAME"})


def viewer_message(value: Any) -> str | None:
    """Return only complete, known, non-input wire messages (no prefix match)."""
    return value if isinstance(value, str) and value in _VIEWER_MESSAGES else None


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _error(code: str, status: int = 403) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": {
        "watch_ended": "This game session has ended or changed. Open family activity to watch again.",
        "watch_unavailable": "Live viewing is temporarily unavailable.",
        "invalid_request": "Invalid viewing request.",
        "invalid_origin": "Viewing requests must come from this RetroOne site.",
        "invalid_host": "Host is not allowed.",
        "readonly": "This viewing connection is read-only.",
    }.get(code, "Viewing is unavailable.")}}, status_code=status, headers=_HEADERS)


def _authority(value: str) -> str:
    parsed = urlsplit("//" + value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.path or any(c in value for c in "\\?# \t\r\n"):
        raise ValueError("Invalid host")
    parsed.port
    return parsed.hostname.lower()


def _upstream(binding: dict[str, Any], path: str, *, websocket: bool = False) -> str:
    base = urlsplit(binding["stream_base_url"])
    if base.scheme not in {"http", "https"} or not base.hostname or base.username or base.password or base.query or base.fragment:
        raise ValueError("Invalid internal stream URL")
    query = ""
    scheme = base.scheme
    if websocket:
        scheme = "wss" if scheme == "https" else "ws"
        # Caller query strings are never copied, including role, slot, mk, token.
        params = {"role": "viewer"}
        if binding.get("selkies_token"):
            params["token"] = binding["selkies_token"]
        query = urlencode(params)
    return urlunsplit((scheme, base.netloc, base.path.rstrip("/") + "/" + path, query, ""))


def _frontend(source: str) -> str:
    """Reuse installed Selkies decoder assets; initialize strictly as #shared."""
    # The controller-only RetroOne adapter polls the game session and installs
    # virtual gamepads. It has no purpose or access in a watch connection.
    source = re.sub(r'<script\b[^>]*src=["\'][^"\']*retroone-input\.js[^"\']*["\'][^>]*>\s*</script>', "", source, flags=re.I)
    source = re.sub(r'<link\b[^>]*rel=["\']manifest["\'][^>]*>', "", source, flags=re.I)
    bootstrap = """<script>
'use strict';
history.replaceState(null, '', location.pathname + '#shared');
window.__SELKIES_STREAMING_MODE__ = 'websockets';
// UX only: authority is enforced by the server, independently of this script.
try { Object.defineProperty(navigator, 'getGamepads', {value: () => [], writable: true, configurable: true}); } catch (_) {}
window.addEventListener('message', e => {
  if(e.source !== window || e.origin !== location.origin) return;
  if(e.data && e.data.type === 'clientRoleUpdate' && e.data.role !== 'viewer') location.replace('../');
});
</script>"""
    if "<head>" not in source:
        raise ValueError("Unsupported Selkies frontend")
    return source.replace("<head>", "<head>" + bootstrap, 1)


def spectate_routes(service: Any, *, allowed_hosts: tuple[str, ...], allowed_origins: tuple[str, ...] = ()) -> list[Any]:
    hosts = frozenset(_authority(host) for host in allowed_hosts)
    origins = frozenset(allowed_origins)

    def guard(request: Request | WebSocket, *, mutation: bool = False) -> Response | None:
        headers = request.headers
        try:
            valid_host = len(headers.getlist("host")) == 1 and _authority(headers["host"]) in hosts
        except (ValueError, KeyError):
            valid_host = False
        if not valid_host:
            return _error("invalid_host", 421)
        values = headers.getlist("origin")
        if values:
            try:
                origin = urlsplit(values[0])
                valid = (len(values) == 1 and origin.scheme in {"http", "https"}
                         and origin.netloc.lower() == headers["host"].lower()
                         and not origin.path and not origin.query and not origin.fragment
                         and not origin.username and not origin.password
                         and (not origins or values[0] in origins))
            except ValueError:
                valid = False
            if not valid:
                return _error("invalid_origin")
        elif mutation or isinstance(request, WebSocket):
            return _error("invalid_origin")
        if headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            return _error("invalid_origin")
        return None

    async def binding(grant: str) -> dict[str, Any]:
        if not _GRANT.fullmatch(grant):
            raise ValueError("Invalid watch grant")
        value = await _await(service.resolve_watch(grant))
        if not isinstance(value, dict) or not value.get("session_id"):
            raise ValueError("Expired watch grant")
        return value

    async def sessions(request: Request) -> Response:
        if rejected := guard(request):
            return rejected
        if request.url.query:
            return _error("invalid_request", 400)
        try:
            return JSONResponse(await _await(service.family_sessions()), headers=_HEADERS)
        except Exception:
            return _error("watch_unavailable", 503)

    async def mint(request: Request) -> Response:
        if rejected := guard(request, mutation=True):
            return rejected
        if request.url.query:
            return _error("invalid_request", 400)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error("invalid_request", 415)
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 2048:
                    return _error("invalid_request", 413)
            data = json.loads(body)
            if not isinstance(data, dict) or set(data) != {"viewer_profile_id", "target_profile_id", "expected_session_id"}:
                raise ValueError()
            if not all(isinstance(data[k], str) and _PROFILE.fullmatch(data[k]) for k in ("viewer_profile_id", "target_profile_id")):
                raise ValueError()
            if not isinstance(data["expected_session_id"], str) or not 1 <= len(data["expected_session_id"]) <= 256:
                raise ValueError()
        except (ValueError, TypeError):
            return _error("invalid_request", 400)
        try:
            return JSONResponse(await _await(service.mint_family_watch(**data)), headers=_HEADERS)
        except Exception:
            return _error("watch_ended", 409)

    async def watch_page(request: Request) -> Response:
        if rejected := guard(request):
            return rejected
        try:
            item = await binding(request.path_params["grant"])
        except Exception:
            return _error("watch_ended", 410)
        name = html.escape(str(item.get("profile_name") or item["profile_id"]))
        title = html.escape(str(item.get("title") or "云端游戏 / Cloud game"))
        page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{name} · {title}</title><style>html,body{{margin:0;width:100%;height:100%;background:#07111f;color:#dceafa;font:14px system-ui}}body{{display:flex;flex-direction:column}}header{{padding:10px 16px;display:flex;align-items:center;gap:16px}}header span{{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}small{{color:#9bb0c8}}button{{background:#173047;color:#dceafa;border:1px solid #3e5268;border-radius:7px;padding:8px 12px;cursor:pointer}}iframe{{border:0;width:100%;flex:1;min-height:0}}body.embedded header{{display:none}}</style></head><body><header><span>{name} · {title} <small>只读观看 / Watching only</small></span><button id="exit">退出观看 / Exit</button></header><iframe id="video" src="./stream/#shared" title="{name} · {title}" allow="autoplay; fullscreen" referrerpolicy="no-referrer"></iframe><script>if(parent!==window)document.body.classList.add('embedded');document.getElementById('exit').onclick=()=>{{document.getElementById('video').remove();if(parent!==window)parent.postMessage({{type:'retroone:watch-close'}},location.origin);else location.replace('/');}};</script></body></html>'''
        return HTMLResponse(page, headers={**_HEADERS, "Content-Security-Policy": "frame-ancestors 'self'; base-uri 'none'; object-src 'none'"})

    async def stream_http(request: Request) -> Response:
        if rejected := guard(request):
            return rejected
        path = request.path_params.get("asset", "")
        if path not in {"", "index.html", "manifest.json", "api/status", "api/websockets"} and not _ASSET.fullmatch(path):
            return _error("readonly", 403)
        try:
            item = await binding(request.path_params["grant"])
            if path == "api/status":
                return JSONResponse({"current_mode": "websockets", "enable_dual_mode": False}, headers=_HEADERS)
            if path == "manifest.json":
                return JSONResponse({"name": "RetroOne · 只读观看", "short_name": "RetroOne"}, headers=_HEADERS)
            if path == "api/websockets":
                return Response(status_code=426, headers=_HEADERS)
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                result = await client.get(_upstream(item, path or "index.html"))
            current = await binding(request.path_params["grant"])
            if current["session_id"] != item["session_id"]:
                return _error("watch_ended", 410)
            if result.status_code != 200 or len(result.content) > 8 * 1024 * 1024:
                return _error("watch_unavailable", 502)
            if path in {"", "index.html"}:
                return HTMLResponse(_frontend(result.text), headers={**_HEADERS, "Content-Security-Policy": "frame-ancestors 'self'; base-uri 'none'; object-src 'none'"})
            media = "text/javascript" if path.endswith(".js") else "text/css" if path.endswith(".css") else result.headers.get("content-type", "application/octet-stream").split(";")[0]
            return Response(result.content, media_type=media, headers=_HEADERS)
        except (ValueError, KeyError):
            return _error("watch_ended", 410)
        except Exception:
            return _error("watch_unavailable", 502)

    async def stream_ws(websocket: WebSocket) -> None:
        if guard(websocket):
            await websocket.close(code=1008)
            return
        grant = websocket.path_params["grant"]
        try:
            item = await binding(grant)
            async with connect(_upstream(item, "api/websockets", websocket=True),
                               compression=None, max_size=16 * 1024 * 1024,
                               open_timeout=10, close_timeout=2, proxy=None) as upstream:
                current = await binding(grant)
                if current["session_id"] != item["session_id"]:
                    raise ValueError("Session changed")
                await websocket.accept(headers=[(b"cache-control", b"no-store")])
                viewer_verified = asyncio.Event()
                if not item.get("selkies_token"):
                    viewer_verified.set()

                async def from_viewer() -> None:
                    while True:
                        event = await websocket.receive()
                        if event["type"] == "websocket.disconnect":
                            return
                        message = viewer_message(event.get("text"))
                        if message is not None:
                            await viewer_verified.wait()
                            await upstream.send(message)

                async def from_game() -> None:
                    async for message in upstream:
                        if isinstance(message, str):
                            if message.startswith(("AUTH_SUCCESS,", "ROLE_UPDATE,")):
                                role = json.loads(message.split(",", 1)[1])
                                if role.get("role") != "viewer" or role.get("slot") is not None:
                                    raise ValueError("Upstream did not grant strict viewer role")
                                viewer_verified.set()
                            if message == "MK_ACCESS,1":
                                raise ValueError("Upstream granted input authority")
                            # Clipboard/file notifications are unrelated to spectating.
                            if message.lower().startswith(("clipboard", "print", "apps", "system,")):
                                continue
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)

                async def watchdog() -> None:
                    while True:
                        await asyncio.sleep(1)
                        current = await binding(grant)
                        if current["session_id"] != item["session_id"]:
                            raise ValueError("Session changed")

                tasks = [asyncio.create_task(run()) for run in (from_viewer, from_game, watchdog)]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (Exception, WebSocketDisconnect):
            pass
        finally:
            with suppress(Exception):
                await websocket.close(code=1000, reason="Viewing ended. Reopen family activity to watch again.")

    return [
        Route("/spectate/api/sessions", sessions, methods=["GET"]),
        Route("/spectate/api/watch", mint, methods=["POST"]),
        Route("/spectate/{grant}/", watch_page, methods=["GET"]),
        WebSocketRoute("/spectate/{grant}/stream/api/websockets", stream_ws),
        Route("/spectate/{grant}/stream/{asset:path}", stream_http, methods=["GET", "HEAD"]),
    ]
