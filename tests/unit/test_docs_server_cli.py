"""`dbtx docs serve` argument handling.

`serve` is the first `dbtx docs` subcommand that shadows a real dbt command,
so these tests pin both the routing decision (it is ours, not passed through)
and the option parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dbtx import cli
from dbtx.docs import server
from dbtx.docs.command import USAGE
from dbtx.docs.command import run as docs_command


@pytest.fixture
def docs_loc(tmp_path: Path) -> Path:
    loc = tmp_path / "target"
    loc.mkdir()
    (loc / "index.html").write_text(
        '<html><body><script src="manifest.json"></script></body></html>',
        encoding="utf-8")
    (loc / "manifest.json").write_text(json.dumps({"nodes": {}}), encoding="utf-8")
    return loc


@pytest.fixture
def served(monkeypatch) -> list[dict]:
    """Records calls to server.serve instead of starting anything."""
    calls: list[dict] = []
    monkeypatch.setattr(server, "serve", lambda docs_loc, **kw: calls.append(
        {"docs_loc": Path(docs_loc), **kw}) or 0)
    return calls


def test_serve_is_ours_not_passed_through_to_dbt():
    assert "serve" in cli.DOCS_SUBCOMMANDS


def test_usage_mentions_serve():
    assert "serve" in USAGE


def test_usage_lists_every_subcommand_and_the_passthrough():
    """`dbtx docs --help` is the only place the real subcommand set is written."""
    for sub in cli.DOCS_SUBCOMMANDS:
        assert f"dbtx docs {sub}" in USAGE
    assert "generate" in USAGE


@pytest.mark.parametrize("argv", [["docs"], ["docs", "-h"], ["docs", "--help"]])
def test_docs_help_is_ours_not_dbt_s(argv: list[str], capsys):
    """dbt's `docs --help` lists only generate and serve, and documents a
    `serve` taking --project-dir, which is no longer the one that runs."""
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "--docs-loc" in out
    for sub in cli.DOCS_SUBCOMMANDS:
        assert sub in out


def test_serve_requires_docs_loc(served: list[dict], capsys):
    assert docs_command(["serve"]) == 2
    assert "--docs-loc" in capsys.readouterr().out
    assert served == []


def test_serve_defaults(served: list[dict], docs_loc: Path):
    assert docs_command(["serve", "--docs-loc", str(docs_loc)]) == 0
    assert served == [{
        "docs_loc": docs_loc,
        "host": server.DEFAULT_HOST,
        "port": server.DEFAULT_PORT,
        "open_browser": False,
    }]


def test_serve_parses_host_port_and_browser(served: list[dict], docs_loc: Path):
    code = docs_command([
        "serve", "--docs-loc", str(docs_loc),
        "--host", "0.0.0.0", "--port", "9001", "--browser",
    ])
    assert code == 0
    assert served[0]["host"] == "0.0.0.0"
    assert served[0]["port"] == 9001
    assert served[0]["open_browser"] is True


def test_non_numeric_port_is_rejected(served: list[dict], docs_loc: Path, capsys):
    assert docs_command(["serve", "--docs-loc", str(docs_loc), "--port", "http"]) == 2
    assert "port" in capsys.readouterr().out
    assert served == []


@pytest.mark.parametrize("port", ["-1", "65536", "99999"])
def test_out_of_range_port_is_rejected(served: list[dict], docs_loc: Path, capsys,
                                       port: str):
    """Left to uvicorn, an unbindable port is an OverflowError traceback."""
    assert docs_command(["serve", "--docs-loc", str(docs_loc), "--port", port]) == 2
    out = capsys.readouterr().out
    assert "dbtx docs:" in out
    assert "--port" in out
    assert served == []


def test_unservable_docs_loc_exits_cleanly(tmp_path: Path, capsys):
    """A ServerError must become an exit code, not a traceback."""
    assert docs_command(["serve", "--docs-loc", str(tmp_path / "nope")]) == 2
    assert "dbtx docs:" in capsys.readouterr().out
