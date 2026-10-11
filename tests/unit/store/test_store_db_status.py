"""`dbtx db status` (plan 6.7, Phase 3): reports on the database, never changes it.

Behaviour (exit 0 whenever it can report; it is in the "reports on the database"
command class, so a disabled backend or a missing DB_URI is reported, not an error):
* always: `DB_BACKEND` and `DB_URI` (masked) with their sources. Disabled backend or no
  URI → nothing else, nothing opened
* `DB_SCHEMA` for a backend with schemas; `adapter`: the adapter's name and why
  (`[default for <backend>+<driver>]`, or DB_ADAPTER's source)
* a local database file that does not exist yet (SQLite) → says so; nothing is created
  (not the file, not its directory)
* `server`: the server's name and version
* `revision`: "0001 (head)"; "none; run `dbtx db init`"; "0001; head is 0002, run
  `dbtx db upgrade`"; or a revision this dbtx does not know
* `tables`: the row count of every table, in dependency order, when at head
* an unreachable server → exit 2 with the reason
* warnings on stderr, for a local database file (SQLite):
  * it holds more than one project: one file suits one host
  * it sits inside a platform project's dbt clean target (`clean-targets`, or by
    default `target-path`, normally `target`): `dbt clean` deletes it
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from dbtx import cli
from dbtx.store import Store, migrate
from dbtx.store import models as m
from dbtx.store.adapters import PostgresAdapter, SqliteAdapter
from tests.unit.conftest import LayoutBuilder

from .conftest import ON_POSTGRES, ON_SQLITE_FILE, ON_SQLITE_FILE_AND_POSTGRES

OCT_1 = datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc)


def db_flags(store: Store) -> list[str]:
    flags = ["--db-uri", store.uri]
    if store.schema is not None:
        flags += ["--db-schema", store.schema]
    return flags


def add_claim(store: Store, project: str, folder: str) -> None:
    with store.begin() as session:
        session.add(
            m.Project(project=project, folder=folder, claimed_at=OCT_1, claimed_by_host="build-01", last_seen_at=OCT_1)
        )


def set_revision(store: Store, revision_id: str) -> None:
    with store.engine.begin() as connection:
        version_table = sa.table("alembic_version", sa.column("version_num"), schema=store.schema)
        connection.execute(sa.update(version_table).values(version_num=revision_id))


def server_line(store: Store) -> str:
    with store.engine.connect() as connection:
        version = store.adapter.server_version(connection)
    text = ".".join(str(part) for part in version)
    return f"  server      {store.adapter.display_name} {text}"


def adapter_line(store: Store) -> str:
    if store.schema is None:
        return "  adapter     sqlite  [default for sqlite+pysqlite]"
    return "  adapter     postgresql  [default for postgresql+psycopg]"


def status_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


# --------------------------------------------------------------------------------------
# A reachable database
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_status_of_a_current_store(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status", *db_flags(store)]) == 0
    lines = status_lines(capsys)
    assert lines[0] == "dbtx db status"
    assert lines[1] == "  DB_BACKEND  enabled  [default]"
    assert lines[2] == f"  DB_URI      {store.masked_uri()}  [cli]"
    rest = lines[3:]
    if store.schema is not None:
        assert rest[0] == f"  DB_SCHEMA   {store.schema}  [cli]"
        rest = rest[1:]
    assert rest == [
        adapter_line(store),
        server_line(store),
        "  revision    0001 (head)",
        "  tables      project 1, job 0, node 0, invocation 0, node_edge 0, node_version 0, "
        "report 0, node_result 0, report_render 0",
    ]


@pytest.mark.parametrize("empty_store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_status_of_an_uninitialised_store_changes_nothing(
    empty_store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An existing, empty database (on SQLite, connecting creates the file).
    with empty_store.engine.connect():
        pass
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status", *db_flags(empty_store)]) == 0
    lines = status_lines(capsys)
    assert lines[-1] == "  revision    none; run `dbtx db init`"
    with empty_store.engine.connect() as connection:
        assert migrate.revision_status(connection, empty_store.schema).state == "uninitialised"
        assert sa.inspect(connection).get_table_names(schema=empty_store.schema) == []


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_status_of_a_store_behind_head(
    store: Store,
    extra_revision: str,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_revision(store, "0001")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status", *db_flags(store)]) == 0
    lines = status_lines(capsys)
    assert lines[-1] == "  revision    0001; head is 0002, run `dbtx db upgrade`"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_status_of_a_revision_this_dbtx_does_not_know(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_revision(store, "ffff")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status", *db_flags(store)]) == 0
    lines = status_lines(capsys)
    assert lines[-1] == (
        "  revision    'ffff', which this dbtx does not know (head is '0001'); it was "
        "migrated by a newer dbtx"
    )


def test_status_with_db_adapter_names_its_source(
    platform: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": "tests.unit.fake_store_adapters:CustomSqliteAdapter"})
    monkeypatch.chdir(platform)
    uri = f"sqlite:///{tmp_path / 'custom.db'}"
    assert cli.main(["db", "init", "--db-uri", uri, "--db-adapter", "custom_sqlite"]) == 0
    capsys.readouterr()
    assert cli.main(["db", "status", "--db-uri", uri, "--db-adapter", "custom_sqlite"]) == 0
    assert "  adapter     custom_sqlite  [cli]" in status_lines(capsys)


# --------------------------------------------------------------------------------------
# Nothing to connect to
# --------------------------------------------------------------------------------------


def test_status_with_the_backend_disabled(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status", "--db-backend", "disabled"]) == 0
    assert status_lines(capsys) == [
        "dbtx db status",
        "  DB_BACKEND  disabled  [cli]",
        f"  DB_URI      sqlite:///{platform / '.dbtx' / 'dbtx.db'}  [file {platform / 'dbtx.conf'}:1]",
    ]
    assert not (platform / ".dbtx").exists()


def test_status_without_a_uri(
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = layout.platform_root(sample="backend_without_uri.conf")
    monkeypatch.chdir(root)
    assert cli.main(["db", "status"]) == 0
    assert status_lines(capsys) == [
        "dbtx db status",
        f"  DB_BACKEND  enabled  [file {root / 'dbtx.conf'}:2]",
        "  DB_URI      not set",
    ]


def test_status_of_a_sqlite_file_that_does_not_exist_yet(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status"]) == 0
    database = platform / ".dbtx" / "dbtx.db"
    lines = status_lines(capsys)
    assert lines[-1] == (
        f"  database    {database} does not exist yet; the first run or `dbtx db init` "
        f"creates it"
    )
    assert not (platform / ".dbtx").exists()


def test_status_of_an_unreachable_server(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    uri = "postgresql+psycopg://dbtx:s3cret@127.0.0.1:1/dbtx?connect_timeout=2"
    assert cli.main(["db", "status", "--db-uri", uri]) == 2
    err = capsys.readouterr().err
    assert "dbtx: cannot connect to the database postgresql+psycopg://dbtx:***@127.0.0.1:1/dbtx" in err
    assert "s3cret" not in err


# --------------------------------------------------------------------------------------
# Warnings for a local database file
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE, indirect=True)
def test_a_sqlite_store_with_several_projects_warns(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    add_claim(store, "other_shop", "other-shop")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status", *db_flags(store)]) == 0
    assert capsys.readouterr().err == (
        "dbtx: warning: this SQLite store holds 2 projects (jaffle_shop, other_shop); one "
        "database file suits one host: projects that build at the same time, or on "
        "several hosts, should share a Postgres database\n"
    )


@pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
def test_a_postgres_store_with_several_projects_does_not_warn(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_claim(store, "jaffle_shop", "jaffle-shop")
    add_claim(store, "other_shop", "other-shop")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "status", *db_flags(store)]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "dbt_project_text, clean_dir",
    [
        ("name: deep_shop\nprofile: deep_shop\n", "target"),
        ("name: deep_shop\nprofile: deep_shop\ntarget-path: out\n", "out"),
        ("name: deep_shop\nprofile: deep_shop\nclean-targets: [build, logs]\n", "logs"),
    ],
    ids=["default-target", "target-path", "clean-targets"],
)
def test_a_sqlite_store_inside_a_clean_target_warns(
    platform: Path,
    layout: LayoutBuilder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dbt_project_text: str,
    clean_dir: str,
) -> None:
    project = layout.project(platform, "deep-shop", "deep_shop", dbt_project_text=dbt_project_text)
    database = project / clean_dir / "dbtx.db"
    monkeypatch.chdir(platform)
    uri = f"sqlite:///{database}"
    assert cli.main(["db", "init", "--db-uri", uri]) == 0
    capsys.readouterr()
    assert cli.main(["db", "status", "--db-uri", uri]) == 0
    assert capsys.readouterr().err == (
        f"dbtx: warning: the SQLite store {database} is inside {project / clean_dir}, a "
        f"clean target of the project in deep-shop: `dbt clean` deletes it\n"
    )


def test_a_sqlite_store_outside_clean_targets_does_not_warn(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "init"]) == 0
    capsys.readouterr()
    assert cli.main(["db", "status"]) == 0
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------------------------
# The adapter: is the database a local file?
# --------------------------------------------------------------------------------------


def test_database_file_of_a_sqlite_uri(tmp_path: Path) -> None:
    assert SqliteAdapter().database_file(f"sqlite:///{tmp_path / 'a.db'}") == tmp_path / "a.db"
    assert SqliteAdapter().database_file(f"sqlite+pysqlite:///{tmp_path / 'b.db'}") == tmp_path / "b.db"


@pytest.mark.parametrize("uri", ["sqlite://", "sqlite:///:memory:", "sqlite:///file:shared?mode=memory&uri=true"])
def test_database_file_of_an_in_memory_sqlite_uri(uri: str) -> None:
    assert SqliteAdapter().database_file(uri) is None


def test_database_file_of_a_server_is_none() -> None:
    assert PostgresAdapter().database_file("postgresql+psycopg://dbtx@db/dbtx") is None


def test_dbtx_db_without_a_subcommand_shows_the_usage(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db"]) == 2
    assert capsys.readouterr().err == (
        "dbtx: usage: dbtx db init | upgrade | status | load | projects [rebind <name> "
        "--folder <folder> [--force]]\n"
    )
