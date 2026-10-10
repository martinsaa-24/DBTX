"""The docs server under a real uvicorn, on a real loopback socket.

The port is bound by the test before uvicorn starts and handed over via
`sockets=[...]`, so the test knows the port without racing the server's
startup and without hard-coding one that might be in use.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from dbtx.docs import server

pytestmark = pytest.mark.server

BOOT_TIMEOUT = 10.0


@pytest.fixture
def docs_loc(tmp_path: Path) -> Path:
    loc = tmp_path / "target"
    loc.mkdir()
    (loc / "index.html").write_text(
        '<html><body><script src="manifest.json"></script></body></html>',
        encoding="utf-8")
    (loc / "manifest.json").write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    return loc


def _wait_until_up(base_url: str) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/index.html", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError as exc:  # not listening yet
            last = exc
            time.sleep(0.05)
    raise AssertionError(f"server did not come up within {BOOT_TIMEOUT}s: {last}")


@contextlib.contextmanager
def running(docs_loc: Path) -> Iterator[str]:
    """Runs the docs server in a background thread, yielding its base URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    instance = server.build_server(docs_loc, host="127.0.0.1", port=port)
    thread = threading.Thread(target=lambda: instance.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_until_up(base_url)
        yield base_url
    finally:
        instance.should_exit = True
        thread.join(timeout=BOOT_TIMEOUT)
        with contextlib.suppress(OSError):
            sock.close()


def test_server_boots_and_serves_index(docs_loc: Path):
    with running(docs_loc) as base_url:
        response = httpx.get(f"{base_url}/", timeout=5.0)
        assert response.status_code == 200
        assert "manifest.json" in response.text


def test_server_serves_concurrent_requests(docs_loc: Path):
    """What dbt's single-threaded TCPServer cannot do."""
    with running(docs_loc) as base_url:
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            with ThreadPoolExecutor(max_workers=10) as pool:
                codes = list(pool.map(
                    lambda _: client.get("/manifest.json").status_code, range(10)))
    assert codes == [200] * 10


def test_serving_does_not_chdir(docs_loc: Path):
    """dbt's ServeTask chdirs the process into target/ and never restores it."""
    before = os.getcwd()
    with running(docs_loc):
        assert os.getcwd() == before
    assert os.getcwd() == before


def test_serving_a_busy_port_raises_rather_than_exiting(docs_loc: Path):
    """A second serve on a live port is a ServerError, not uvicorn's sys.exit(3).

    `serve` is the path the CLI takes, so this is what keeps a busy port on the
    `dbtx docs: ...` and exit 2 convention the other errors use.
    """
    with running(docs_loc) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
        with pytest.raises(server.ServerError, match="cannot serve"):
            server.serve(docs_loc, port=port)
