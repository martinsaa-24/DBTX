"""The docs server: static serving of a patched dbt docs directory.

Exercised through Starlette's TestClient, which drives the ASGI app in-process.
That keeps every behaviour below -- routing, headers, caching -- testable
without binding a port; the real uvicorn boot is covered separately in
tests/integration/test_docs_server_boot.py.
"""

from __future__ import annotations

import json
import os
import socket
import webbrowser
from pathlib import Path

import pytest
import uvicorn
from starlette.testclient import TestClient

from dbtx.docs import injector
from dbtx.docs.server import (
    ServerError,
    bind,
    browser_url,
    build_server,
    create_app,
    serve,
)


@pytest.fixture
def docs_loc(tmp_path: Path) -> Path:
    """A minimal stand-in for a `dbt docs generate` target directory."""
    loc = tmp_path / "target"
    loc.mkdir()
    (loc / "index.html").write_text(
        '<html><body><script src="manifest.json"></script></body></html>',
        encoding="utf-8",
    )
    (loc / "manifest.json").write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    (loc / "catalog.json").write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    return loc


@pytest.fixture
def client(docs_loc: Path) -> TestClient:
    return TestClient(create_app(docs_loc))


def test_root_serves_index(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "manifest.json" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_index_html_path_also_serves_index(client: TestClient):
    assert client.get("/index.html").status_code == 200


def test_create_app_does_not_chdir(docs_loc: Path):
    """dbt's ServeTask chdirs the whole process into target/; this must not."""
    before = os.getcwd()
    create_app(docs_loc)
    assert os.getcwd() == before


def test_docroot_survives_a_later_chdir(docs_loc: Path, tmp_path: Path):
    """The docroot is bound at construction, so the caller's cwd cannot move it."""
    app = create_app(docs_loc)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    before = os.getcwd()
    try:
        os.chdir(elsewhere)
        assert TestClient(app).get("/index.html").status_code == 200
    finally:
        os.chdir(before)


def test_missing_docs_loc_is_rejected(tmp_path: Path):
    with pytest.raises(ServerError):
        create_app(tmp_path / "nope")


def test_docs_loc_without_index_is_rejected(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ServerError, match="dbt docs generate"):
        create_app(empty)


# --- Artifacts, mime types and refusals -------------------------------------
# Largely characterisation tests: they pin behaviour this module gets from
# StaticFiles rather than driving new code, because the overlay and the docs
# SPA both depend on it.

def test_manifest_served_as_json(client: TestClient):
    response = client.get("/manifest.json")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]
    assert response.json() == {"nodes": {}}


def test_catalog_served_as_json(client: TestClient):
    response = client.get("/catalog.json")
    assert response.status_code == 200
    assert "application/json" in response.headers["content-type"]


def test_unknown_path_404s(client: TestClient):
    assert client.get("/nope.json").status_code == 404


def test_head_request_has_no_body_but_reports_length(client: TestClient):
    response = client.head("/manifest.json")
    assert response.status_code == 200
    assert response.text == ""
    assert int(response.headers["content-length"]) > 0


@pytest.mark.parametrize("path", [
    "/../pyproject.toml",
    "/..%2F..%2Fpyproject.toml",
    "/%2e%2e/%2e%2e/pyproject.toml",
])
def test_escaping_the_docroot_is_refused(client: TestClient, path: str):
    """Never serve a file outside docs_loc, however the path is encoded."""
    assert client.get(path).status_code == 404


# --- Cache policy -----------------------------------------------------------
# dbt's SimpleHTTPRequestHandler sends no validators at all, so every reload
# refetches the full manifest. Three different policies are needed here:
# regenerated-in-place files must never be cached, content-hashed URLs can be
# cached forever, and the big artifacts want conditional GETs.

def test_index_html_is_not_cached(client: TestClient):
    """`dbt docs generate` rewrites index.html under a running server."""
    assert "no-store" in client.get("/").headers.get("cache-control", "")
    assert "no-store" in client.get("/index.html").headers.get("cache-control", "")


def test_sidecar_is_not_cached(docs_loc: Path):
    """`dbtx docs patch` rewrites the sidecar under a running server."""
    (docs_loc / "dbtx_runtime.json").write_text(
        json.dumps({"dbtx_schema_version": 1, "nodes": {}}), encoding="utf-8")
    response = TestClient(create_app(docs_loc)).get("/dbtx_runtime.json")
    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")


def test_manifest_has_an_etag(client: TestClient):
    assert client.get("/manifest.json").headers.get("etag")


def test_conditional_get_returns_304(client: TestClient):
    etag = client.get("/manifest.json").headers["etag"]
    response = client.get("/manifest.json", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""


def test_content_hashed_asset_is_cacheable(docs_loc: Path):
    """The overlay's script URL carries ?v=<hash>, so it can be immutable."""
    (docs_loc / "dbtx_docs.js").write_text("/* overlay */", encoding="utf-8")
    response = TestClient(create_app(docs_loc)).get("/dbtx_docs.js?v=abc123def456")
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "max-age=" in cache_control
    assert "no-store" not in cache_control


@pytest.mark.parametrize("path", [
    "/manifest.json?v=abc123def456",
    "/catalog.json?v=1",
])
def test_version_param_alone_does_not_make_a_file_immutable(client: TestClient, path: str):
    """Only the overlay asset is content-hashed, so only it can be immutable.

    `?v=` on a regenerated artifact is not a promise about its bytes: honouring
    it would pin a stale `manifest.json` in the browser for a year with no way
    to invalidate it.
    """
    response = client.get(path)
    assert response.status_code == 200
    assert "immutable" not in response.headers.get("cache-control", "")


def test_version_param_does_not_override_no_store(client: TestClient):
    assert "no-store" in client.get("/index.html?v=1").headers["cache-control"]


# --- Pre-patch state and the overlay asset ----------------------------------

def test_absent_sidecar_404s(client: TestClient):
    """An unpatched docs dir is the normal pre-patch state, not an error.

    The overlay already treats a non-OK sidecar fetch as "no run data" rather
    than a failure (assets/dbtx_docs.js), so the server must not invent an
    empty sidecar body and change that contract.
    """
    assert client.get("/dbtx_runtime.json").status_code == 404


def test_sidecar_served_when_present(docs_loc: Path):
    (docs_loc / "dbtx_runtime.json").write_text(
        json.dumps({"dbtx_schema_version": 1, "history_limit": 5,
                    "nodes": {"model.proj.a": {"runs": []}}}), encoding="utf-8")
    response = TestClient(create_app(docs_loc)).get("/dbtx_runtime.json")
    assert response.status_code == 200
    assert response.json()["dbtx_schema_version"] == 1
    assert "model.proj.a" in response.json()["nodes"]


def test_overlay_asset_falls_back_to_the_packaged_copy(client: TestClient):
    """`dbtx docs serve` works without a prior `dbtx docs install`."""
    response = client.get("/dbtx_docs.js")
    assert response.status_code == 200
    assert response.text == injector.asset_source().read_text(encoding="utf-8")
    assert "javascript" in response.headers["content-type"]


def test_overlay_asset_in_docs_loc_wins(docs_loc: Path):
    """An installed copy shadows the packaged one, so `install` stays authoritative."""
    (docs_loc / "dbtx_docs.js").write_text("/* installed copy */", encoding="utf-8")
    response = TestClient(create_app(docs_loc)).get("/dbtx_docs.js")
    assert response.status_code == 200
    assert response.text == "/* installed copy */"


# --- serve() wiring ---------------------------------------------------------
# The blocking run() is stubbed out here; the real boot is covered in
# tests/integration/test_docs_server_boot.py.

@pytest.fixture
def stub_run(monkeypatch) -> None:
    """Lets serve() wire everything up without blocking on the event loop."""
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: None)


def test_serve_does_not_open_a_browser_by_default(docs_loc: Path, monkeypatch, stub_run):
    """Differs from dbt, whose --browser defaults to True (dbt/cli/params.py)."""
    opened = []
    monkeypatch.setattr(webbrowser, "open_new_tab", lambda url: opened.append(url))
    serve(docs_loc, port=0)
    assert opened == []


def test_serve_opens_a_browser_when_asked(docs_loc: Path, monkeypatch, stub_run):
    opened = []
    monkeypatch.setattr(webbrowser, "open_new_tab", lambda url: opened.append(url))
    serve(docs_loc, port=0, open_browser=True)
    assert len(opened) == 1
    assert opened[0].startswith("http://127.0.0.1:")


def test_browser_opens_at_the_port_the_kernel_chose(docs_loc: Path, monkeypatch, stub_run):
    """With --port 0 the URL has to come from the socket, not the argument."""
    opened = []
    monkeypatch.setattr(webbrowser, "open_new_tab", lambda url: opened.append(url))
    serve(docs_loc, port=0, open_browser=True)
    assert opened[0] != "http://127.0.0.1:0"


def test_serve_prints_the_url_and_the_docroot(docs_loc: Path, capsys, stub_run):
    """Handing uvicorn a bound socket skips its own "Uvicorn running on" line.

    Without this the one thing a reader needs -- where to point a browser --
    never reaches the terminal.
    """
    serve(docs_loc, port=0)
    out = capsys.readouterr().out
    assert "http://127.0.0.1:" in out
    assert "http://127.0.0.1:0" not in out
    assert str(docs_loc.resolve()) in out


def test_browser_opens_only_once_the_port_accepts_connections(
        docs_loc: Path, monkeypatch, stub_run):
    """dbt opens the browser before its server listens; this must not.

    Connecting from inside the `open_new_tab` call is the assertion: it only
    succeeds if `serve` has already bound *and* listened, which is what puts a
    browser's connection in the kernel's backlog instead of having it refused
    while uvicorn starts.
    """
    reached = []

    def connect(url: str) -> None:
        port = int(url.rsplit(":", 1)[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5.0):
            reached.append(port)

    monkeypatch.setattr(webbrowser, "open_new_tab", connect)
    serve(docs_loc, port=0, open_browser=True)
    assert len(reached) == 1


@pytest.mark.parametrize("host,expected", [
    ("127.0.0.1", "http://127.0.0.1:9001"),
    ("localhost", "http://localhost:9001"),
    # A wildcard bind is an instruction to the kernel, not somewhere a browser
    # can usefully navigate.
    ("0.0.0.0", "http://127.0.0.1:9001"),
    ("::", "http://127.0.0.1:9001"),
    # An IPv6 literal has to be bracketed to be a valid URL authority.
    ("::1", "http://[::1]:9001"),
])
def test_browser_url(host: str, expected: str):
    assert browser_url(host, 9001) == expected


def test_build_server_carries_host_and_port(docs_loc: Path):
    instance = build_server(docs_loc, host="0.0.0.0", port=9002)
    assert instance.config.host == "0.0.0.0"
    assert instance.config.port == 9002


# --- bind() -----------------------------------------------------------------
# uvicorn binds inside run(), logging the OSError and calling sys.exit(3);
# binding up front is what keeps a bad port on the ServerError path.

@pytest.mark.parametrize("port", [-1, 65536, 99999])
def test_out_of_range_port_is_refused_before_binding(port: int):
    with pytest.raises(ServerError, match="between 0 and 65535"):
        bind("127.0.0.1", port)


def test_bind_returns_a_listening_socket():
    sock = bind("127.0.0.1", 0)
    try:
        assert sock.getsockname()[1] != 0
        with socket.create_connection(sock.getsockname(), timeout=5.0):
            pass
    finally:
        sock.close()


def test_bind_refuses_a_port_already_in_use():
    """Two `dbtx docs serve` on one port must be an error, not a silent steal."""
    first = bind("127.0.0.1", 0)
    try:
        with pytest.raises(ServerError, match="cannot serve"):
            bind("127.0.0.1", first.getsockname()[1])
    finally:
        first.close()
