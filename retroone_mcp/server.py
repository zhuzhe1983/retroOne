"""Authenticated, single-process MCP Streamable HTTP transport for RetroOne.

Run with ``python -m retroone_mcp.server``. The service deliberately has its own
optional dependency set; the emulator runtime does not import the MCP SDK.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager
import inspect
import json
import logging
import os
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route


LOGGER = logging.getLogger(__name__)
PREFIX = "retroone_"
DEFAULT_HOSTS = ("localhost", "127.0.0.1", "[::1]")
MAX_REQUEST_BYTES = 64 * 1024
Identifier = Annotated[str, Field(min_length=1, max_length=256)]
ProfileId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Button = Literal["a", "b", "x", "y", "lb", "rb", "lt", "rt", "back", "start", "l3", "r3", "up", "down", "left", "right"]
Axis = Literal["lx", "ly", "rx", "ry"]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListGamesArguments(Arguments):
    profile_id: ProfileId
    q: Annotated[str, Field(max_length=256)] = ""
    platform: Annotated[str, Field(max_length=64)] = ""
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0, le=100000)] = 0


class LaunchArguments(Arguments):
    profile_id: ProfileId
    game_id: Identifier
    request_id: Identifier
    mode: Literal["auto", "browser", "stream"] = "stream"
    replace_session_id: Identifier | None = None


class SessionArguments(Arguments):
    profile_id: ProfileId
    expected_session_id: Identifier


class ObserveArguments(Arguments):
    handle: Identifier
    after_action_id: Identifier | None = None
    max_width: Annotated[int, Field(ge=160, le=1920)] = 960


class ClaimArguments(SessionArguments):
    slot: Annotated[int, Field(ge=1, le=4)] = 1


class ActArguments(Arguments):
    lease_id: Identifier
    expected_session_id: Identifier
    action_id: Identifier
    observation_id: Identifier
    buttons: Annotated[list[Button], Field(max_length=16)] = Field(default_factory=list)
    axes: Annotated[dict[Axis, Annotated[float, Field(ge=-1.0, le=1.0, allow_inf_nan=False)]], Field(max_length=4)] = Field(default_factory=dict)
    duration_ms: Annotated[int, Field(ge=1, le=1000)] = 100

    @field_validator("buttons")
    @classmethod
    def unique_buttons(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Buttons must be unique")
        return value


class ReleaseArguments(Arguments):
    lease_id: Identifier


class StopArguments(SessionArguments):
    request_id: Identifier


# Tool descriptions explain the session/observation preconditions to clients.
TOOL_SPECS: dict[str, tuple[type[Arguments], str, bool]] = {
    "capabilities": (Arguments, "Describe authorized profiles, runtime support, and safe control limits.", True),
    "list_games": (ListGamesArguments, "Search games available to one authorized profile; returns stable game identifiers.", True),
    "sessions": (Arguments, "List the current sessions of authorized profiles.", True),
    "launch": (LaunchArguments, "Launch a game with an idempotent request_id. Replacing an active session requires its exact replace_session_id.", False),
    "watch": (SessionArguments, "Obtain an observation handle for the exact current session without taking control.", True),
    "observe": (ObserveArguments, "Read a fresh screenshot and observation_id. Supply after_action_id to observe after a completed action.", True),
    "claim_control": (ClaimArguments, "Claim one controller slot for the exact session; returns a short-lived control lease.", False),
    "act": (ActArguments, "Send a bounded controller input using a valid lease and fresh observation_id; action_id makes retries safe.", False),
    "release_control": (ReleaseArguments, "Release the caller's controller lease and neutralize its input.", False),
    "stop": (StopArguments, "Stop only the exact expected session with an idempotent request_id.", False),
}


def _host(value: str) -> str:
    """Normalize a Host authority without trusting forwarded headers."""
    if not value or any(char in value for char in "*/\\?#@ \t\r\n"):
        raise ValueError("Allowed hosts must be explicit hostnames or IP addresses")
    parsed = urlsplit("//" + value)
    if not parsed.hostname or parsed.path or parsed.username or parsed.password:
        raise ValueError("Invalid host")
    # Accessing port checks invalid/out-of-range port syntax as well.
    parsed.port
    return parsed.hostname.lower()


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if "*" in value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Allowed origins must be explicit http(s) origins without wildcards")
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Allowed origins must contain only scheme and authority")
    _host(parsed.netloc)
    return value


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class AuthorizationMiddleware:
    """Validate every MCP HTTP request before it reaches SDK parsing/dispatch."""

    def __init__(self, app: Any, *, service: Any, allowed_origins: tuple[str, ...], allowed_hosts: tuple[str, ...]):
        self.app = app
        self.service = service
        self.allowed_origins = frozenset(_origin(value) for value in allowed_origins)
        self.allowed_hosts = frozenset(_host(value) for value in allowed_hosts)
        if not self.allowed_hosts:
            raise ValueError("At least one explicit allowed host is required")

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path == "/healthz" or path == "/spectate" or path.startswith("/spectate/"):
            # Spectator routes own their distinct trusted-LAN and watch-ticket
            # checks. Their bypass must never extend to /mcp or lookalike paths.
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)

        async def reject(status: int, code: str) -> None:
            response = JSONResponse(
                {"error": code}, status_code=status,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"} if status == 401 else {"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)

        try:
            valid_host = len(headers.getlist("host")) == 1 and _host(headers["host"]) in self.allowed_hosts
        except (ValueError, KeyError):
            valid_host = False
        if not valid_host:
            await reject(421, "invalid_host")
            return
        origins = headers.getlist("origin")
        if origins and (len(origins) != 1 or origins[0] not in self.allowed_origins):
            await reject(403, "invalid_origin")
            return
        # There is no query-string API. Reject all queries so credentials cannot
        # accidentally enter proxy/access logs or become an alternate auth path.
        if scope.get("query_string"):
            await reject(400, "query_parameters_not_supported")
            return
        authorization = headers.getlist("authorization")
        if len(authorization) != 1:
            await reject(401, "unauthorized")
            return
        parts = authorization[0].split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1] or any(char.isspace() for char in parts[1]):
            await reject(401, "unauthorized")
            return
        try:
            principal = await _await(self.service.authenticate(parts[1]))
        except Exception:
            # Fail closed without logging credentials or config-file contents.
            await reject(503, "authentication_unavailable")
            return
        if principal is None:
            await reject(401, "unauthorized")
            return
        scope.setdefault("state", {})["retroone_principal"] = principal
        await self.app(scope, receive, send)


def _principal(ctx: Any) -> Any:
    request = ctx.request
    principal = getattr(getattr(request, "state", None), "retroone_principal", None)
    if principal is None:
        raise MCPError(-32001, "Authentication required")
    return principal


def _result(payload: dict[str, Any], *, error: bool = False) -> types.CallToolResult:
    public = dict(payload)
    picture = public.pop("_image", None)
    content: list[Any] = [types.TextContent(text=json.dumps(public, ensure_ascii=False, allow_nan=False))]
    if picture is not None:
        mime = picture["mimeType"]
        data = picture["data"]
        if mime not in {"image/png", "image/jpeg", "image/webp"} or not isinstance(data, str):
            raise ValueError("Invalid screenshot representation")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid screenshot encoding") from exc
        if not decoded or len(decoded) > 8 * 1024 * 1024:
            raise ValueError("Invalid screenshot size")
        content.append(types.ImageContent(data=data, mime_type=mime))
    return types.CallToolResult(content=content, structured_content=public, is_error=error)


def create_app(
    service: Any, *, allowed_origins: tuple[str, ...] = (),
    allowed_hosts: tuple[str, ...] = DEFAULT_HOSTS,
    extra_routes: tuple[BaseRoute, ...] | list[BaseRoute] = (),
) -> Starlette:
    """Build an injectable app; ``service`` owns policy and runtime state.

    ``authenticate(token)`` and ``tool_names(principal)`` may be sync or async.
    ``call(principal, short_tool_name, arguments)`` and ``close()`` are async.
    ServiceFailure's public ``code``/``message`` are safe to return to clients.
    """
    # Validate deployment configuration eagerly, before the listener starts.
    origins = tuple(_origin(value) for value in allowed_origins)
    hosts = tuple(allowed_hosts)
    if not hosts:
        raise ValueError("At least one explicit allowed host is required")
    for value in hosts:
        _host(value)

    async def authorized_names(principal: Any) -> set[str]:
        names = await _await(service.tool_names(principal))
        return {name.removeprefix(PREFIX) for name in names}

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        allowed = await authorized_names(_principal(ctx))
        return types.ListToolsResult(tools=[
            types.Tool(
                name=PREFIX + name, description=description, input_schema=model.model_json_schema(),
                annotations=types.ToolAnnotations(
                    read_only_hint=readonly, destructive_hint=name in {"launch", "stop", "act"},
                    idempotent_hint=name in {"capabilities", "list_games", "sessions", "observe", "launch", "act", "release_control", "stop"},
                    open_world_hint=False,
                ),
            )
            for name, (model, description, readonly) in TOOL_SPECS.items() if name in allowed
        ])

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        principal = _principal(ctx)
        name = params.name.removeprefix(PREFIX)
        if not params.name.startswith(PREFIX) or name not in TOOL_SPECS or name not in await authorized_names(principal):
            return _result({"error": {"code": "tool_not_available", "message": "Tool is not available for this token"}}, error=True)
        try:
            arguments = TOOL_SPECS[name][0].model_validate(params.arguments or {}).model_dump(mode="json")
        except ValidationError:
            return _result({"error": {"code": "invalid_arguments", "message": "Arguments do not match the tool schema"}}, error=True)
        try:
            payload = await service.call(principal, name, arguments)
            return _result(payload, error=bool(payload.get("error")) or payload.get("ok") is False)
        except Exception as exc:
            # Import lazily so transport-only tests need no runtime dependencies.
            from .service import ServiceFailure

            if isinstance(exc, ServiceFailure):
                return _result({"error": {"code": exc.code, "message": exc.message}}, error=True)
            LOGGER.error("MCP tool %s failed (%s)", name, type(exc).__name__)
            return _result({"error": {"code": "internal_error", "message": "The tool could not complete"}}, error=True)

    def input_schema(name: str) -> dict[str, Any] | None:
        short = name.removeprefix(PREFIX)
        return TOOL_SPECS[short][0].model_json_schema() if name.startswith(PREFIX) and short in TOOL_SPECS else None

    server = Server(
        "RetroOne", version="1.0.0", on_list_tools=list_tools,
        on_call_tool=call_tool, get_tool_input_schema=input_schema,
    )
    # HTTP auth/host/origin checks above are exact and shared by both protocol
    # generations. Stateless transport avoids binding security to an MCP session.
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        custom_starlette_routes=[
            Route("/healthz", lambda request: JSONResponse({"status": "ok", "service": "retroone-mcp"})),
            *extra_routes,
        ],
    )
    app.add_middleware(AuthorizationMiddleware, service=service, allowed_origins=origins, allowed_hosts=hosts)
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Starlette):
        try:
            async with sdk_lifespan(application):
                yield
        finally:
            await service.close()

    app.router.lifespan_context = lifespan
    return app


def _env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.environ.get(name)
    return tuple(item.strip() for item in value.split(",") if item.strip()) if value is not None else default


def main() -> None:
    import uvicorn
    from .service import create_service_from_env
    from .spectate import spectate_routes

    service = create_service_from_env()
    origins = _env_list("RETROONE_MCP_ALLOWED_ORIGINS")
    hosts = _env_list("RETROONE_MCP_ALLOWED_HOSTS", DEFAULT_HOSTS)
    app = create_app(
        service, allowed_origins=origins, allowed_hosts=hosts,
        extra_routes=spectate_routes(service, allowed_hosts=hosts, allowed_origins=origins),
    )
    uvicorn.run(
        app, host=os.environ.get("RETROONE_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("RETROONE_MCP_PORT", "8090")), workers=1,
        access_log=False, proxy_headers=False,
    )


if __name__ == "__main__":
    main()
