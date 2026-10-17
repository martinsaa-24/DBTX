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
from dbtx.docs.store_history import HistoryCache


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
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Records calls to server.serve instead of starting anything."""
    calls: list[dict] = []
    monkeypatch.setattr(server, "serve", lambda docs_loc, **kw: calls.append(
        {"docs_loc": Path(docs_loc), **kw}) or 0)
    return calls


def test_serve_is_ours_not_passed_through_to_dbt() -> None:
    assert "serve" in cli.DOCS_SUBCOMMANDS


def test_usage_mentions_serve() -> None:
    assert "serve" in USAGE


def test_usage_lists_every_subcommand_and_the_passthrough() -> None:
    """`dbtx docs --help` is the only place the real subcommand set is written."""
    for sub in cli.DOCS_SUBCOMMANDS:
        assert f"dbtx docs {sub}" in USAGE
    assert "generate" in USAGE


@pytest.mark.parametrize("argv", [["docs"], ["docs", "-h"], ["docs", "--help"]])
def test_docs_help_is_ours_not_dbt_s(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """dbt's `docs --help` lists only generate and serve, and documents a
    `serve` taking --project-dir, which is no longer the one that runs."""
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "--docs-loc" in out
    for sub in cli.DOCS_SUBCOMMANDS:
        assert sub in out


def test_serve_requires_docs_loc(served: list[dict], capsys: pytest.CaptureFixture[str]) -> None:
    assert docs_command(["serve"]) == 2
    assert "--docs-loc" in capsys.readouterr().out
    assert served == []


def test_serve_defaults(served: list[dict], docs_loc: Path) -> None:
    assert docs_command(["serve", "--docs-loc", str(docs_loc)]) == 0
    assert served == [{
        "docs_loc": docs_loc,
        "host": server.DEFAULT_HOST,
        "port": server.DEFAULT_PORT,
        "open_browser": False,
        "history": None,
    }]


def test_serve_parses_host_port_and_browser(served: list[dict], docs_loc: Path) -> None:
    code = docs_command([
        "serve", "--docs-loc", str(docs_loc),
        "--host", "0.0.0.0", "--port", "9001", "--browser",
    ])
    assert code == 0
    assert served[0]["host"] == "0.0.0.0"
    assert served[0]["port"] == 9001
    assert served[0]["open_browser"] is True


def test_non_numeric_port_is_rejected(
    served: list[dict],
    docs_loc: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert docs_command(["serve", "--docs-loc", str(docs_loc), "--port", "http"]) == 2
    assert "port" in capsys.readouterr().out
    assert served == []


@pytest.mark.parametrize("port", ["-1", "65536", "99999"])
def test_out_of_range_port_is_rejected(
    served: list[dict],
    docs_loc: Path,
    capsys: pytest.CaptureFixture[str],
    port: str,
) -> None:
    """Left to uvicorn, an unbindable port is an OverflowError traceback."""
    assert docs_command(["serve", "--docs-loc", str(docs_loc), "--port", port]) == 2
    out = capsys.readouterr().out
    assert "dbtx docs:" in out
    assert "--port" in out
    assert served == []


def test_unservable_docs_loc_exits_cleanly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ServerError must become an exit code, not a traceback."""
    assert docs_command(["serve", "--docs-loc", str(tmp_path / "nope")]) == 2
    assert "dbtx docs:" in capsys.readouterr().out


# --- The run history from the database (plan Phase 7) -------------------------
# Platform mode with the backend enabled: `docs serve` uses the database. It passes
# the start-up checks like every database command, then serves the run history from
# the database (a HistoryCache for the docs site's project). Standalone mode, or
# DB_BACKEND=disabled: the sidecar file, as before (no history).


def write_project_docs_site(platform_root: Path, project_name: str | None = "jaffle_shop") -> Path:
    """`jaffle-shop`'s docs site (its target folder): index.html and a manifest."""
    site = platform_root / "jaffle-shop" / "target"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text("<html></html>", encoding="utf-8")
    metadata = {}
    if project_name is not None:
        metadata["project_name"] = project_name
    (site / "manifest.json").write_text(
        json.dumps({"metadata": metadata, "nodes": {}, "exposures": {}}), encoding="utf-8")
    return site


def test_platform_serve_reads_the_history_from_the_database(
    initialised_platform: Path, monkeypatch: pytest.MonkeyPatch, served: list[dict]
) -> None:
    site = write_project_docs_site(initialised_platform)
    monkeypatch.chdir(initialised_platform)

    assert cli.main(["docs", "serve", "--project-dir", "jaffle-shop"]) == 0

    [call] = served
    history = call["history"]
    assert isinstance(history, HistoryCache)
    assert (history.docs_loc, history.project) == (site, "jaffle_shop")


def test_platform_serve_with_an_explicit_docs_loc_reads_the_database_too(
    initialised_platform: Path, monkeypatch: pytest.MonkeyPatch, served: list[dict]
) -> None:
    site = write_project_docs_site(initialised_platform)
    monkeypatch.chdir(initialised_platform)

    assert cli.main(["docs", "serve", "--docs-loc", str(site)]) == 0

    assert served[0]["history"].project == "jaffle_shop"


def test_platform_serve_with_the_backend_disabled_serves_the_file(
    platform: Path, monkeypatch: pytest.MonkeyPatch, served: list[dict]
) -> None:
    write_project_docs_site(platform)
    monkeypatch.chdir(platform)

    assert cli.main(["docs", "serve", "--project-dir", "jaffle-shop", "--db-backend", "disabled"]) == 0

    assert served[0]["history"] is None


def test_standalone_serve_serves_the_file(
    lone_project: Path, monkeypatch: pytest.MonkeyPatch, served: list[dict]
) -> None:
    site = lone_project / "target"
    site.mkdir()
    (site / "index.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.chdir(lone_project)

    assert cli.main(["docs", "serve", "--docs-loc", str(site), "--standalone-mode"]) == 0

    assert served[0]["history"] is None


def test_platform_serve_needs_an_initialised_database(
    platform: Path, monkeypatch: pytest.MonkeyPatch, served: list[dict], capsys: pytest.CaptureFixture[str]
) -> None:
    write_project_docs_site(platform)
    monkeypatch.chdir(platform)

    assert cli.main(["docs", "serve", "--project-dir", "jaffle-shop"]) == 2

    assert "dbtx db init" in capsys.readouterr().err
    assert served == []


def test_platform_serve_needs_a_project_name_in_the_docs_manifest(
    initialised_platform: Path, monkeypatch: pytest.MonkeyPatch, served: list[dict],
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_project_docs_site(initialised_platform, project_name=None)
    monkeypatch.chdir(initialised_platform)

    assert cli.main(["docs", "serve", "--project-dir", "jaffle-shop"]) == 2

    assert "metadata.project_name" in capsys.readouterr().out
    assert served == []
