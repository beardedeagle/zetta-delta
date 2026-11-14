"""Temporary local compatibility adapter for Delta's Kimi settings schema."""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    DummyCookieJar,
    TCPConnector,
    TraceConfig,
    web,
)
from multidict import CIMultiDict, CIMultiDictProxy

UPSTREAM = "https://api.kimi.ai/coding/v1/chat/completions"
MODELS_UPSTREAM = "https://api.kimi.ai/coding/v1/models"
CLIENT = web.AppKey("client", ClientSession)
QUIET_LOGGER = logging.getLogger("delta_kimi_adapter")
QUIET_LOGGER.addHandler(logging.NullHandler())
QUIET_LOGGER.propagate = False
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass
class RequestTiming:
    """Keep request-local timing without retaining request or response content.

    Attributes:
        request_id (str): Random correlation ID unrelated to caller data.
        started (float): Monotonic request start time in seconds.
        phase (str): Current transport phase, selected by adapter code.
        phase_started (float): Monotonic start time of the current phase.
    """

    request_id: str = field(default_factory=lambda: secrets.token_hex(16))
    started: float = field(default_factory=time.monotonic)
    phase: str = "request_body"
    phase_started: float = field(init=False)

    def __post_init__(self) -> None:
        """Initialize the first phase from the same request start instant."""
        self.phase_started = self.started

    def start_phase(self, phase: str) -> None:
        """Start an adapter-owned transport phase.

        Args:
            phase (str): Fixed phase name, never a caller-supplied value.
        """
        self.phase = phase
        self.phase_started = time.monotonic()

    def emit(self, event: str, *, status: int | None = None,
             corrections: int | None = None, error_type: str | None = None) -> None:
        """Write one sanitized JSON timing record.

        Args:
            event (str): Fixed adapter lifecycle event name.
            status (int | None): Upstream HTTP status, when known.
            corrections (int | None): Count of the exact supported schema rewrite.
            error_type (str | None): Exception class name, never its message.
        """
        now = time.monotonic()
        record = {"timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                  "request_id": self.request_id, "event": event, "phase": self.phase,
                  "elapsed_ms": round((now - self.started) * 1000, 3),
                  "phase_ms": round((now - self.phase_started) * 1000, 3)}
        for name, value in (("status", status), ("schema_corrections", corrections),
                            ("error_type", error_type)):
            if value is not None:
                record[name] = value
        try:
            print(json.dumps(record, separators=(",", ":")), flush=True)
        except (OSError, ValueError):
            # A closed diagnostic sink must not alter the forwarded response.
            pass


def timing_trace() -> TraceConfig:
    """Build connection tracing over request-local, content-free timing state.

    Returns:
        TraceConfig: Signals for connection setup/reuse and sent request headers.
    """
    trace = TraceConfig()

    async def connecting(_session: ClientSession, context: SimpleNamespace, _params: object) -> None:
        """Mark connection setup without inspecting signal parameters."""
        timing = context.trace_request_ctx
        timing.start_phase("connect")
        timing.emit("connect_start")

    async def connected(_session: ClientSession, context: SimpleNamespace, _params: object) -> None:
        """Record connection duration without retaining peer details."""
        timing = context.trace_request_ctx
        timing.emit("connected")
        timing.start_phase("request_send")

    async def reused(_session: ClientSession, context: SimpleNamespace, _params: object) -> None:
        """Distinguish a reused connection from new connection setup."""
        timing = context.trace_request_ctx
        timing.start_phase("request_send")
        timing.emit("connection_reused")

    async def sent(_session: ClientSession, context: SimpleNamespace, _params: object) -> None:
        """Start response-header waiting without inspecting sent headers."""
        timing = context.trace_request_ctx
        timing.emit("request_headers_sent")
        timing.start_phase("response_headers")

    trace.on_connection_create_start.append(connecting)
    trace.on_connection_create_end.append(connected)
    trace.on_connection_reuseconn.append(reused)
    trace.on_request_headers_sent.append(sent)
    return trace


def load_capability(path: Path) -> str:
    """Load or create a private local capability.

    Args:
        path (Path): Private capability file in the adapter state directory.

    Returns:
        str: Persistent URL-safe capability.

    Raises:
        OSError: The file cannot be safely opened or created.
        ValueError: Existing file ownership, permissions, or content is unsafe.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="ascii") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                raise ValueError("Capability file must be private and owned by this user")
            value = source.read(129)
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
                raise ValueError("Invalid local capability")
            return value
    value = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w", encoding="ascii") as destination:
        destination.write(value)
        destination.flush()
        os.fsync(destination.fileno())
    return value


def forwarded_headers(
    headers: CIMultiDictProxy[str], omit: tuple[str, ...] = ()
) -> CIMultiDict[str]:
    """Retain end-to-end headers without HTTP hop metadata.

    Args:
        headers (CIMultiDictProxy[str]): Incoming request or provider headers.
        omit (tuple[str, ...]): Additional lowercase headers invalidated locally.

    Returns:
        CIMultiDict[str]: Headers preserving duplicate end-to-end fields.
    """
    blocked = HOP_HEADERS | set(omit)
    for connection in headers.getall("Connection", []):
        blocked.update(value.strip().lower() for value in connection.split(","))
    return CIMultiDict(
        (name, value) for name, value in headers.items() if name.lower() not in blocked
    )


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Decode an object without silently discarding duplicate JSON keys.

    Args:
        pairs (list[tuple[str, object]]): Decoded JSON object members.

    Returns:
        dict[str, object]: Object retaining all unique members.

    Raises:
        ValueError: An object contains duplicate keys.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(value: str) -> None:
    """Reject a non-JSON numeric constant.

    Args:
        value (str): Unsupported numeric constant.

    Raises:
        ValueError: Always, because JSON does not support these constants.
    """
    raise ValueError("Non-JSON numeric constant")


def normalize(body: bytes) -> tuple[bytes, int]:
    """Return request bytes and the number of corrected settings schemas.

    Args:
        body (bytes): UTF-8 JSON chat-completions request.

    Returns:
        tuple[bytes, int]: Request bytes and correction count.

    Raises:
        ValueError: Input is invalid JSON, contains duplicate keys, or cannot
            retain JSON values during serialization.
    """
    request = json.loads(body, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    tools = request.get("tools") if isinstance(request, dict) else None
    count = 0
    for tool in tools if isinstance(tools, list) else []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not (
            isinstance(function, dict)
            and tool.get("type") == "function"
            and function.get("name") == "delta_settings"
        ):
            continue
        schema = function
        for key in ("parameters", "properties", "value"):
            schema = schema.get(key) if isinstance(schema, dict) else None
        branches = schema.get("anyOf") if isinstance(schema, dict) else None
        for branch in branches if isinstance(branches, list) else []:
            if (
                isinstance(branch, dict)
                and branch.get("items") is True
                and branch == {"type": "array", "items": True}
            ):
                branch["items"] = {}
                count += 1
    if not count:
        return body, 0
    return json.dumps(
        request, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode(), count


def create_app(capability: str) -> web.Application:
    """Build the loopback application.

    Args:
        capability (str): Private local access capability.

    Returns:
        web.Application: Application forwarding Kimi chat completions and model discovery.

    Raises:
        ValueError: The local capability is too short or not URL-safe.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", capability):
        raise ValueError("A private URL-safe capability is required")
    app = web.Application(client_max_size=0, logger=QUIET_LOGGER)

    async def client_context(application: web.Application) -> AsyncIterator[None]:
        """Own the upstream session for the application's lifetime.

        Args:
            application (web.Application): Local adapter application.

        Yields:
            None: Session remains available until application cleanup.
        """
        async with ClientSession(
            connector=TCPConnector(ssl=ssl.create_default_context()),
            timeout=ClientTimeout(total=None, sock_connect=30),
            auto_decompress=False,
            cookie_jar=DummyCookieJar(),
            trust_env=False,
            trace_configs=[timing_trace()],
        ) as client:
            application[CLIENT] = client
            yield

    async def forward(request: web.Request) -> web.StreamResponse:
        """Validate a local request and forward it to its fixed Kimi endpoint.

        Args:
            request (web.Request): Native chat-completions or model-discovery request.

        Returns:
            web.StreamResponse: Provider response or sanitized connection failure.

        Raises:
            web.HTTPException: Local routing, authorization, or JSON validation fails.
        """
        local = request.transport.get_extra_info("sockname") if request.transport else None
        if (
            request.remote != "127.0.0.1"
            or not local
            or request.host != f"127.0.0.1:{local[1]}"
            or "Origin" in request.headers
        ):
            raise web.HTTPForbidden(text="Loopback client required")
        if request.query_string:
            raise web.HTTPBadRequest(text="Query parameters are unsupported")
        authorizations = request.headers.getall("Authorization", [])
        if (
            len(authorizations) != 1
            or not authorizations[0].startswith("Bearer ")
            or not authorizations[0][7:].strip()
        ):
            raise web.HTTPUnauthorized(text="Provider authorization required")
        body = None
        corrections = 0
        timing = RequestTiming()
        timing.emit("request_started")
        target = MODELS_UPSTREAM
        if request.method == "POST":
            target = UPSTREAM
            if request.content_type != "application/json":
                timing.emit("rejected", status=415)
                raise web.HTTPUnsupportedMediaType(text="JSON request required")
            try:
                body, corrections = normalize(await request.read())
            except asyncio.CancelledError:
                timing.emit("cancelled", error_type="CancelledError")
                raise
            except (ValueError, UnicodeError, RecursionError):
                timing.emit("rejected", status=400)
                raise web.HTTPBadRequest(text="Invalid JSON request") from None
            except (ClientError, OSError, TimeoutError) as error:
                timing.emit("error", error_type=type(error).__name__)
                raise
        headers = forwarded_headers(request.headers, ("host", "content-length", "content-encoding"))
        response = None
        try:
            timing.start_phase("connection_queue")
            timing.emit("upstream_started")
            async with request.app[CLIENT].request(
                request.method, target, data=body, headers=headers, allow_redirects=False,
                trace_request_ctx=timing,
            ) as upstream:
                timing.emit("response_headers", status=upstream.status, corrections=corrections)
                timing.start_phase("response_stream")
                response = web.StreamResponse(
                    status=upstream.status, headers=forwarded_headers(upstream.headers)
                )
                await response.prepare(request)
                first_chunk = True
                async for chunk in upstream.content.iter_any():
                    if first_chunk:
                        timing.emit("first_chunk", status=upstream.status)
                        first_chunk = False
                    await response.write(chunk)
                await response.write_eof()
                timing.emit("complete", status=upstream.status)
                return response
        except asyncio.CancelledError:
            timing.emit("cancelled", error_type="CancelledError")
            raise
        except (ClientError, OSError, TimeoutError) as error:
            timing.emit("error", error_type=type(error).__name__)
            if response is not None and response.prepared:
                if request.transport:
                    request.transport.close()
                return response
            return web.json_response({"error": {"message": "Kimi connection failed"}}, status=502)

    app.cleanup_ctx.append(client_context)
    app.router.add_post(f"/{capability}/coding/v1/chat/completions", forward)
    app.router.add_get(f"/{capability}/coding/v1/models", forward, allow_head=False)
    return app


async def serve(port: int, state: Path) -> None:
    """Run one loopback adapter with private persistent local state.

    Args:
        port (int): Loopback port; zero selects an available port.
        state (Path): Private directory holding capability and endpoint metadata.

    Raises:
        OSError: Binding, locking, or private state operations fail.
        ValueError: State ownership or permissions are unsafe.
    """
    state.mkdir(mode=0o700, parents=False, exist_ok=True)
    metadata = state.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("State directory must be private and owned by this user")
    descriptor = os.open(
        state / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    with os.fdopen(descriptor, "r+") as lock:
        metadata = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("State lock must be a private regular file")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        capability = load_capability(state / "capability")
        runner = web.AppRunner(
            create_app(capability),
            access_log=None,
            logger=QUIET_LOGGER,
            handler_cancellation=True,
            shutdown_timeout=5,
        )
        await runner.setup()
        endpoint = state / "endpoint.json"
        published = False
        temporary = None
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", port))
            listener.setblocking(False)
            port = listener.getsockname()[1]
            await web.SockSite(runner, listener).start()
            payload = {
                "pid": os.getpid(),
                "base_url": f"http://127.0.0.1:{port}/{capability}/coding/v1",
            }
            with tempfile.NamedTemporaryFile(
                mode="w", dir=state, delete=False, encoding="utf-8"
            ) as destination:
                temporary = Path(destination.name)
                json.dump(payload, destination)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, endpoint)
            temporary = None
            published = True
            print(f"Adapter ready on 127.0.0.1:{port}; private endpoint metadata saved", flush=True)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await stop.wait()
        finally:
            await runner.cleanup()
            listener.close()
            if published:
                endpoint.unlink(missing_ok=True)
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main() -> int:
    """Parse local startup arguments and run the adapter.

    Returns:
        int: Zero after normal shutdown, one for a sanitized startup failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18773)
    parser.add_argument("--state", type=Path, default=Path(__file__).parent / "state")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    try:
        asyncio.run(serve(args.port, args.state))
    except (OSError, ValueError) as error:
        print(f"Adapter startup failed ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
