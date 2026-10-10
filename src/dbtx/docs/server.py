"""An ASGI server for the generated docs site.

dbt's own `docs serve` is a single-threaded `socketserver.TCPServer` that it
points at the docs by `chdir`-ing the whole process into `target/`. Both of
those hurt here: the overlay adds a third artifact to the page load, so
requests queueing behind one multi-megabyte `manifest.json` is immediately
visible, and a process-global `chdir` is hostile to anything that embeds dbtx.

This module keeps two seams. `create_app` is a pure factory -- it validates the
docs directory and returns an ASGI app, touching neither the event loop nor the
process's cwd -- and `serve` is the only thing that knows about uvicorn. That
split is what lets the routing and header behaviour be tested in-process
without binding a port.
"""

from __future__ import annotations

import socket
import sys
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dbtx.docs.injector import ASSET_FILENAME, asset_source
from dbtx.docs.sidecar import SIDECAR_FILENAME

INDEX_FILENAME: str = "index.html"

DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8080

MAX_PORT: int = 65535

#: Rewritten in place under a running server -- by `dbt docs generate` and by
#: `dbtx docs patch` respectively -- so a cached copy is a stale copy.
NEVER_CACHED: frozenset[str] = frozenset({INDEX_FILENAME, SIDECAR_FILENAME})

#: Hosts that tell the kernel "every interface" rather than naming an address a
#: browser can navigate to.
WILDCARD_HOSTS: frozenset[str] = frozenset({"0.0.0.0", "::", ""})

#: Only ASSET_FILENAME is served under a content-hashed URL -- injector writes
#: `?v=<hash>` into the script tag it installs, and nothing else here does --
#: so it is the only name whose bytes a given URL is guaranteed to keep.
IMMUTABLE: str = "public, max-age=31536000, immutable"


class ServerError(Exception):
    """Raised when a directory cannot be served as a docs site."""


def _cache_control(name: str, query_string: bytes) -> Optional[str]:
    """The cache policy for one file, or None to keep the static default."""
    if name in NEVER_CACHED:
        return "no-store"
    if name == ASSET_FILENAME and "v" in parse_qs(query_string.decode("latin-1")):
        return IMMUTABLE
    return None


def _served_name(path: str) -> str:
    """The file name a URL path resolves to, matching `StaticFiles(html=True)`.

    A directory path is served by the `index.html` inside it, the one case
    where the file served is not the URL's last segment.
    """
    return INDEX_FILENAME if path.endswith("/") else path.rsplit("/", 1)[-1]


class CachePolicy:
    """Applies the docs site's cache policies to every response.

    A pure ASGI middleware rather than a `StaticFiles` subclass: the obvious
    seam there is `file_response`, which is undocumented and carries no
    signature promise across the `starlette<1` range this depends on. Rewriting
    the outgoing `http.response.start` touches only the ASGI contract, and it
    covers the overlay route as well, so a request for `dbtx_docs.js` gets the
    same headers whichever route answers it.

    Everything `_cache_control` leaves alone keeps StaticFiles' own ETag and
    Last-Modified, which is what turns a reload of a multi-megabyte
    `manifest.json` into a 304 instead of a full refetch.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        policy = _cache_control(
            _served_name(scope["path"]), scope.get("query_string", b""))
        if policy is None:
            await self.app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["cache-control"] = policy
            await send(message)

        await self.app(scope, receive, send_with_policy)


def _overlay_route(loc: Path) -> Route:
    """Serves the overlay asset, falling back to the copy inside the package.

    Without this, `dbtx docs serve` would 404 on `dbtx_docs.js` until someone
    had run `dbtx docs install` into this directory. An installed copy still
    wins, so `install` remains authoritative for what a reader gets.
    """

    async def endpoint(request: Request) -> Response:
        installed = loc / ASSET_FILENAME
        return FileResponse(installed if installed.is_file() else asset_source())

    return Route(f"/{ASSET_FILENAME}", endpoint=endpoint, methods=["GET", "HEAD"])


def _require_docs_loc(docs_loc: Path) -> Path:
    """Validates the docroot and pins it to an absolute path.

    Resolving here rather than per request means a later `chdir` by the caller
    cannot move the docroot out from under a running server.
    """
    loc = Path(docs_loc).resolve()
    if not loc.is_dir():
        raise ServerError(f"{loc} is not a directory -- run `dbt docs generate` first")
    if not (loc / INDEX_FILENAME).is_file():
        raise ServerError(
            f"no {INDEX_FILENAME} in {loc} -- run `dbt docs generate` first")
    return loc


def create_app(docs_loc: Path) -> Starlette:
    """An ASGI app serving the docs site rooted at `docs_loc`."""
    loc = _require_docs_loc(docs_loc)
    return Starlette(
        routes=[
            _overlay_route(loc),
            Mount("/", app=StaticFiles(directory=loc, html=True), name="docs"),
        ],
        middleware=[Middleware(CachePolicy)],
    )


def browser_url(host: str, port: int) -> str:
    """The URL to open for a server bound to `host:port`."""
    if host in WILDCARD_HOSTS:
        return f"http://127.0.0.1:{port}"
    if ":" in host:  # an IPv6 literal, which a URL has to bracket
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def bind(host: str, port: int) -> socket.socket:
    """The listening socket for `host:port`, bound before uvicorn starts.

    uvicorn binds inside `run`, where it logs an OSError and calls
    `sys.exit(3)` -- bypassing the `dbtx docs: ...` and exit 2 that every other
    error in this command group reports through. Binding here turns a busy or
    out-of-range port back into a ServerError, and listening here means a
    browser opened at the URL has its connection queued by the kernel instead
    of refused while uvicorn is still starting.
    """
    if not 0 <= port <= MAX_PORT:
        raise ServerError(f"port must be between 0 and {MAX_PORT}, got {port}")

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    # Deliberately not on Windows, where SO_REUSEADDR lets a second bind take a
    # port that is already being served rather than failing -- uvicorn sets it
    # unconditionally, which is how `uvicorn --port` can double-bind there.
    # Everywhere else it is what lets a restart reuse a port still in TIME_WAIT.
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen()
    except OSError as exc:
        sock.close()
        raise ServerError(f"cannot serve on {host}:{port} -- {exc}") from exc
    return sock


def build_server(docs_loc: Path, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT, log_level: str = "info") -> uvicorn.Server:
    """A configured uvicorn server, not yet running.

    Separate from `serve` so a test can drive it with an already-bound socket
    via `run(sockets=[...])` and know the port without racing startup.
    """
    config = uvicorn.Config(
        create_app(docs_loc), host=host, port=port, log_level=log_level)
    return uvicorn.Server(config)


def serve(docs_loc: Path, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          open_browser: bool = False) -> None:
    """Serves the docs at `host:port` until interrupted.

    `open_browser` is opt-in, unlike dbt's `--browser`, which defaults to True:
    this server is also meant to be startable from a script or CI job, where
    launching a browser is a surprise rather than a convenience.
    """
    # Built before bind(), so an unservable docs directory is reported without
    # having taken a port first.
    instance = build_server(docs_loc, host=host, port=port)
    sock = bind(host, port)
    try:
        # getsockname, not `port`: with `--port 0` the kernel chose one. Also
        # why the URL is printed here rather than left to uvicorn -- its own
        # "Uvicorn running on ..." line comes from the bind inside `run`, which
        # handing it an already-bound socket skips.
        url = browser_url(host, sock.getsockname()[1])
        # flush: every other dbtx command prints and exits, so buffering is
        # invisible there. This one then blocks in run() forever, which would
        # strand the banner in the buffer whenever stdout is not a tty.
        print(f"dbtx docs: serving {_require_docs_loc(docs_loc)}")
        print(f"  {url}  (ctrl-c to quit)", flush=True)
        if open_browser:
            webbrowser.open_new_tab(url)
        instance.run(sockets=[sock])
    finally:
        sock.close()
