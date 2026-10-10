"""cli.main with configuration: checked first, before dbt is ever started.

State comes from the test fixtures in tests/unit/conftest.py: `layout` builds a clean
directory structure per test, sample config files come from tests/fixtures/config, and
`fake_dbt` records what would have been handed to dbt.
"""

from __future__ import annotations

import pytest

from dbtx import cli


@pytest.fixture
def postgres_platform(layout):
    """A platform whose dbtx.conf holds a Postgres URI with a password."""
    root = layout.platform_root(sample="platform_postgres.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    return root


# --------------------------------------------------------------------------------------
# Configuration is checked first
# --------------------------------------------------------------------------------------


def test_bad_config_exits_2_without_starting_dbt(layout, monkeypatch, capsys, fake_dbt):
    root = layout.platform_root(sample="invalid_unknown_key.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    # The dbt args are invalid too; the config problem is still the one reported.
    code = cli.main(["run", "--project-dir", "jaffle-shop", "--no-such-dbt-option"])
    assert code == 2
    assert fake_dbt.calls == []
    assert "unknown key 'DB_URL'" in capsys.readouterr().err


def test_bad_config_never_reaches_build(layout, monkeypatch):
    root = layout.platform_root(sample="bad_backend_value.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    called = []
    monkeypatch.setattr(cli, "build", lambda *a, **k: called.append(a) or 0)
    assert cli.main(["build", "--project-dir", "jaffle-shop"]) == 2
    assert called == []


def test_warnings_go_to_stderr(lone_project, monkeypatch, capsys, fake_dbt):
    monkeypatch.chdir(lone_project)
    cli.main(["ls"])
    captured = capsys.readouterr()
    assert "dbtx: warning: no dbtx platform found" in captured.err
    assert "no dbtx platform found" not in captured.out


@pytest.mark.parametrize("argv", [["--help"], ["-h"], []])
def test_help_works_with_no_configuration(layout, monkeypatch, enforce, fake_dbt, argv):
    monkeypatch.chdir(layout.directory("empty"))
    assert cli.main(argv) == 0


@pytest.mark.parametrize("argv", [["--help"], ["-h"], []])
def test_help_shows_dbtx_usage_before_dbt_help(layout, monkeypatch, capsys, fake_dbt, argv):
    monkeypatch.chdir(layout.directory("empty"))
    cli.main(argv)
    out = capsys.readouterr().out
    # dbtx's own commands and flags are listed...
    assert out.startswith("usage: dbtx")
    for text in ["dbtx build", "dbtx compile", "dbtx templates", "dbtx docs", "dbtx config"]:
        assert text in out
    for flag in ["--platform-dir", "--standalone-mode", "--config-file", "--db-backend", "--db-uri"]:
        assert flag in out
    # ...and dbt's help still follows, for the commands passed through to it.
    assert fake_dbt.calls == [["--help"]]


def test_subcommand_help_still_goes_to_dbt(layout, monkeypatch, capsys, fake_dbt):
    monkeypatch.chdir(layout.directory("empty"))
    cli.main(["run", "--help"])
    assert "usage: dbtx" not in capsys.readouterr().out
    assert fake_dbt.calls == [["run", "--help"]]


# --------------------------------------------------------------------------------------
# The argv dbt receives
# --------------------------------------------------------------------------------------


def test_platform_argv_reaches_dbt_rewritten(platform, monkeypatch, fake_dbt):
    monkeypatch.chdir(platform)
    project = str(platform / "jaffle-shop")
    assert cli.main(["run", "--project-dir", "jaffle-shop", "-s", "x"]) == 0
    assert fake_dbt.calls == [
        ["run", "-s", "x", "--project-dir", project, "--profiles-dir", project]
    ]


def test_standalone_argv_reaches_dbt_untouched(platform, monkeypatch, fake_dbt):
    monkeypatch.chdir(platform / "jaffle-shop")
    assert cli.main(["run", "-s", "x", "--standalone-mode"]) == 0
    assert fake_dbt.calls == [["run", "-s", "x"]]


def test_build_receives_the_config(platform, monkeypatch):
    monkeypatch.chdir(platform)
    received = {}

    def fake_build(args, reports_enabled=True, config=None):
        received["args"] = args
        received["config"] = config
        return 0

    monkeypatch.setattr(cli, "build", fake_build)
    assert cli.main(["build", "--project-dir", "jaffle-shop", "--no-reports"]) == 0
    assert received["config"].mode == "platform"
    assert "--no-reports" not in received["args"]
    assert "--profiles-dir" in received["args"]


# --------------------------------------------------------------------------------------
# dbtx config
# --------------------------------------------------------------------------------------


def test_dbtx_config_in_platform_mode(postgres_platform, monkeypatch, capsys):
    monkeypatch.chdir(postgres_platform)
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "platform (current directory holds dbtx.conf)" in out
    assert f"{postgres_platform / 'dbtx.conf'} (platform root)" in out
    assert "enabled  [default]" in out
    assert "postgresql+psycopg://dbtx:***@db/dbtx" in out
    assert "s3cret" not in out


def test_dbtx_config_in_standalone_mode(layout, monkeypatch, capsys):
    monkeypatch.chdir(layout.directory("empty"))
    monkeypatch.setenv("DBTX_DB_URI", "sqlite://")
    assert cli.main(["config", "--standalone-mode"]) == 0
    out = capsys.readouterr().out
    assert "standalone (forced by --standalone-mode)" in out
    assert "n/a (standalone mode has no database)" in out
    assert "DBTX_DB_URI (standalone)" in out


def test_dbtx_config_exits_2_on_invalid_values(platform, monkeypatch):
    monkeypatch.chdir(platform)
    assert cli.main(["config", "--db-uri", "nope"]) == 2


# --------------------------------------------------------------------------------------
# dbtx db
# --------------------------------------------------------------------------------------


def test_dbtx_db_requires_platform_mode(layout, monkeypatch, capsys):
    monkeypatch.chdir(layout.directory("empty"))
    assert cli.main(["db", "status", "--standalone-mode"]) == 2
    assert "require platform mode" in capsys.readouterr().err


def test_dbtx_db_is_not_available_yet(platform, monkeypatch, capsys):
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status"]) == 2
    assert "not available yet" in capsys.readouterr().err
