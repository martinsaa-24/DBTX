"""cli.main with configuration: checked first, before dbt is ever started.

State comes from the test fixtures in tests/unit/conftest.py: `layout` builds a clean
directory structure per test, sample config files come from tests/fixtures/config, and
`fake_dbt` records what would have been handed to dbt.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from dbtx import cli
from dbtx.config import DbtxConfig
from tests.unit.conftest import FakeDbtRunner, LayoutBuilder


@pytest.fixture
def postgres_platform(layout: LayoutBuilder) -> Path:
    """A platform whose dbtx.conf holds a Postgres URI with a password."""
    root = layout.platform_root(sample="platform_postgres.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    return root


# --------------------------------------------------------------------------------------
# Configuration is checked first
# --------------------------------------------------------------------------------------


def test_bad_config_exits_2_without_starting_dbt(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_dbt: type[FakeDbtRunner],
) -> None:
    root = layout.platform_root(sample="invalid_unknown_key.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    # The dbt args are invalid too; the config problem is still the one reported.
    code = cli.main(["run", "--project-dir", "jaffle-shop", "--no-such-dbt-option"])
    assert code == 2
    assert fake_dbt.calls == []
    assert "unknown key 'DB_URL'" in capsys.readouterr().err


def test_bad_config_never_reaches_build(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = layout.platform_root(sample="bad_backend_value.conf")
    layout.project(root, "jaffle-shop", "jaffle_shop")
    monkeypatch.chdir(root)
    called = []
    monkeypatch.setattr(cli, "build", lambda *a, **k: called.append(a) or 0)
    assert cli.main(["build", "--project-dir", "jaffle-shop"]) == 2
    assert called == []


def test_warnings_go_to_stderr(
    lone_project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_dbt: type[FakeDbtRunner],
) -> None:
    monkeypatch.chdir(lone_project)
    cli.main(["ls"])
    captured = capsys.readouterr()
    assert "dbtx: warning: no dbtx platform found" in captured.err
    assert "no dbtx platform found" not in captured.out


@pytest.mark.parametrize("argv", [["--help"], ["-h"], []])
def test_help_works_with_no_configuration(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    enforce: None,
    fake_dbt: type[FakeDbtRunner],
    argv: list[str],
) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    assert cli.main(argv) == 0


@pytest.mark.parametrize("argv", [["--help"], ["-h"], []])
def test_help_shows_dbtx_usage_before_dbt_help(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_dbt: type[FakeDbtRunner],
    argv: list[str],
) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    cli.main(argv)
    out = capsys.readouterr().out
    # dbtx's own commands and flags are listed...
    assert out.startswith("usage: dbtx")
    for text in ["dbtx build", "dbtx compile", "dbtx templates", "dbtx docs", "dbtx config"]:
        assert text in out
    for flag in [
        "--platform-dir", "--standalone-mode", "--config-file", "--db-backend", "--db-uri",
        "--db-schema", "--db-adapter",
    ]:
        assert flag in out
    # ...and dbt's help still follows, for the commands passed through to it.
    assert fake_dbt.calls == [["--help"]]


def test_subcommand_help_still_goes_to_dbt(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_dbt: type[FakeDbtRunner],
) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    cli.main(["run", "--help"])
    assert "usage: dbtx" not in capsys.readouterr().out
    assert fake_dbt.calls == [["run", "--help"]]


# --------------------------------------------------------------------------------------
# The argv dbt receives
# --------------------------------------------------------------------------------------


def test_platform_argv_reaches_dbt_rewritten(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_dbt: type[FakeDbtRunner],
) -> None:
    monkeypatch.chdir(platform)
    project = str(platform / "jaffle-shop")
    assert cli.main(["run", "--project-dir", "jaffle-shop", "-s", "x"]) == 0
    assert fake_dbt.calls == [
        ["run", "-s", "x", "--project-dir", project, "--profiles-dir", project]
    ]


def test_standalone_argv_reaches_dbt_untouched(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_dbt: type[FakeDbtRunner],
) -> None:
    monkeypatch.chdir(platform / "jaffle-shop")
    assert cli.main(["run", "-s", "x", "--standalone-mode"]) == 0
    assert fake_dbt.calls == [["run", "-s", "x"]]


def test_build_receives_the_config(platform: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(platform)
    received = {}

    def fake_build(args: list[str], reports_enabled: bool = True, config: DbtxConfig | None = None) -> int:
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


def test_dbtx_config_in_platform_mode(
    postgres_platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(postgres_platform)
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "platform (current directory holds dbtx.conf)" in out
    assert f"{postgres_platform / 'dbtx.conf'} (platform root)" in out
    assert "enabled  [default]" in out
    assert "postgresql+psycopg://dbtx:***@db/dbtx" in out
    assert "s3cret" not in out


def test_dbtx_config_shows_db_schema(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = layout.platform_root(sample="platform_postgres_schema.conf")
    monkeypatch.chdir(root)
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert f"DB_SCHEMA      analytics_dbtx  [file {root / 'dbtx.conf'}:2]" in out


def test_dbtx_config_db_schema_with_sqlite(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["config"]) == 0
    assert "DB_SCHEMA      n/a (SQLite has no schemas)" in capsys.readouterr().out


def test_dbtx_config_db_schema_without_a_uri(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    monkeypatch.chdir(root)
    assert cli.main(["config"]) == 0
    assert "DB_SCHEMA      dbtx  [default]" in capsys.readouterr().out


def test_dbtx_config_shows_the_default_adapter(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["config"]) == 0
    assert "DB_ADAPTER     sqlite  [default for sqlite+pysqlite]" in capsys.readouterr().out


def test_dbtx_config_shows_db_adapter(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": "tests.unit.fake_store_adapters:CustomSqliteAdapter"})
    root = layout.platform_root(sample="platform_sqlite_adapter.conf")
    monkeypatch.chdir(root)
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert f"DB_ADAPTER     custom_sqlite  [file {root / 'dbtx.conf'}:2]" in out


def test_dbtx_config_db_adapter_without_a_uri(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    monkeypatch.chdir(root)
    assert cli.main(["config"]) == 0
    assert "DB_ADAPTER     not set (the default for DB_URI)" in capsys.readouterr().out


def test_dbtx_config_in_standalone_mode(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    monkeypatch.setenv("DBTX_DB_URI", "sqlite://")
    assert cli.main(["config", "--standalone-mode"]) == 0
    out = capsys.readouterr().out
    assert "standalone (forced by --standalone-mode)" in out
    assert "DB_SCHEMA      n/a (standalone mode has no database)" in out
    assert "DB_ADAPTER     n/a (standalone mode has no database)" in out
    assert "DBTX_DB_URI (standalone)" in out


def test_dbtx_config_exits_2_on_invalid_values(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["config", "--db-uri", "nope"]) == 2


# --------------------------------------------------------------------------------------
# dbtx db
# --------------------------------------------------------------------------------------


def test_dbtx_db_requires_platform_mode(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(layout.directory("empty"))
    assert cli.main(["db", "status", "--standalone-mode"]) == 2
    assert "require platform mode" in capsys.readouterr().err


def test_dbtx_db_is_not_available_yet(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status"]) == 2
    assert "not available yet" in capsys.readouterr().err
