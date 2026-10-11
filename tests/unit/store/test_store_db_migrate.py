"""`dbtx db init` and `dbtx db upgrade` (plan 6.7, Phase 3).

Behaviour:

`dbtx db init`
* a backend with schemas (Postgres): DB_SCHEMA is created when it does not exist
  (`created schema '<name>'`); one that cannot be created (no privilege) → exit 2
  naming the schema and the way out. Nothing else is done then
* then migrates to head, printing what changed: `created dbtx's tables in <uri>
  (revision 0001)`, `upgraded <uri> from revision X to Y`, or `<uri> is already at
  revision X (head)`. Exit 0
* a revision this dbtx does not know (migrated by a newer dbtx) → exit 2, unchanged

`dbtx db upgrade`
* migrates to head with the same messages (an empty database gets its tables)
* Postgres: a missing DB_SCHEMA → exit 2 with `run dbtx db init`; nothing is created
* an unknown revision → exit 2

Both need platform mode, the backend enabled and DB_URI, like every `dbtx db` command
that uses the database (the same checks as `dbtx db projects`). An unknown `dbtx db`
subcommand → exit 2 with the usage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from dbtx import cli
from dbtx.store import Store, migrate

from .conftest import ON_POSTGRES, ON_SQLITE_FILE_AND_POSTGRES


def db_flags(store: Store) -> list[str]:
    flags = ["--db-uri", store.uri]
    if store.schema is not None:
        flags += ["--db-schema", store.schema]
    return flags


def revision(store: Store) -> migrate.RevisionStatus:
    with store.engine.connect() as connection:
        return migrate.revision_status(connection, store.schema)


def set_revision(store: Store, revision_id: str) -> None:
    with store.engine.begin() as connection:
        version_table = sa.table("alembic_version", sa.column("version_num"), schema=store.schema)
        connection.execute(sa.update(version_table).values(version_num=revision_id))


def schema_tables(postgres_uri: str, schema: str) -> list[str]:
    engine = sa.create_engine(postgres_uri)
    try:
        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            if not inspector.has_schema(schema):
                return []
            return sorted(inspector.get_table_names(schema=schema))
    finally:
        engine.dispose()


def schema_exists(postgres_uri: str, schema: str) -> bool:
    engine = sa.create_engine(postgres_uri)
    try:
        with engine.connect() as connection:
            return sa.inspect(connection).has_schema(schema)
    finally:
        engine.dispose()


def masked(uri: str) -> str:
    return sa.make_url(uri).render_as_string(hide_password=True)


# --------------------------------------------------------------------------------------
# dbtx db init
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("empty_store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_init_creates_the_tables(
    empty_store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "init", *db_flags(empty_store)]) == 0
    assert capsys.readouterr().out == (
        f"created dbtx's tables in {empty_store.masked_uri()} (revision 0001)\n"
    )
    assert revision(empty_store).state == "current"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_init_again_changes_nothing(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "init", *db_flags(store)]) == 0
    assert capsys.readouterr().out == f"{store.masked_uri()} is already at revision 0001 (head)\n"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_init_upgrades_a_store_behind_head(
    store: Store,
    extra_revision: str,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_revision(store, "0001")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "init", *db_flags(store)]) == 0
    assert capsys.readouterr().out == f"upgraded {store.masked_uri()} from revision 0001 to 0002\n"
    assert revision(store).current == "0002"


@pytest.mark.postgres
def test_init_creates_a_missing_postgres_schema(
    postgres_uri: str,
    missing_schema: str,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    flags = ["--db-uri", postgres_uri, "--db-schema", missing_schema]
    assert cli.main(["db", "init", *flags]) == 0
    assert capsys.readouterr().out == (
        f"created schema '{missing_schema}'\n"
        f"created dbtx's tables in {masked(postgres_uri)} (revision 0001)\n"
    )
    assert "alembic_version" in schema_tables(postgres_uri, missing_schema)
    assert "project" in schema_tables(postgres_uri, missing_schema)


@pytest.mark.postgres
def test_init_reports_a_schema_it_cannot_create(
    postgres_uri: str,
    limited_role_uri: str,
    missing_schema: str,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    flags = ["--db-uri", limited_role_uri, "--db-schema", missing_schema]
    assert cli.main(["db", "init", *flags]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        f"dbtx: cannot create the schema '{missing_schema}' in {masked(limited_role_uri)}: "
    ) in captured.err
    assert (
        "; create it (or ask an administrator to), then run `dbtx db init` again"
    ) in captured.err
    assert not schema_exists(postgres_uri, missing_schema)


@pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
def test_init_on_an_existing_schema_does_not_try_to_create_it(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The store's schema exists already: no `created schema` line.
    monkeypatch.chdir(platform)
    assert cli.main(["db", "init", *db_flags(store)]) == 0
    assert "created schema" not in capsys.readouterr().out


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_init_refuses_a_revision_this_dbtx_does_not_know(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_revision(store, "ffff")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "init", *db_flags(store)]) == 2
    assert (
        f"dbtx: the database {store.masked_uri()} is at revision 'ffff', which this dbtx "
        f"does not know (head is '0001'); it was migrated by a newer dbtx"
    ) in capsys.readouterr().err
    assert revision(store).current == "ffff"


def test_init_needs_the_backend_enabled(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "init", "--db-backend", "disabled"]) == 2
    assert "`dbtx db init` needs the database backend, which is disabled" in capsys.readouterr().err


# --------------------------------------------------------------------------------------
# dbtx db upgrade
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_upgrade_reaches_head_from_behind(
    store: Store,
    extra_revision: str,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_revision(store, "0001")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "upgrade", *db_flags(store)]) == 0
    assert capsys.readouterr().out == f"upgraded {store.masked_uri()} from revision 0001 to 0002\n"
    assert revision(store).current == "0002"


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_upgrade_at_head_changes_nothing(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "upgrade", *db_flags(store)]) == 0
    assert capsys.readouterr().out == f"{store.masked_uri()} is already at revision 0001 (head)\n"


@pytest.mark.parametrize("empty_store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_upgrade_creates_the_tables_of_an_empty_database(
    empty_store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "upgrade", *db_flags(empty_store)]) == 0
    assert capsys.readouterr().out == (
        f"created dbtx's tables in {empty_store.masked_uri()} (revision 0001)\n"
    )
    assert revision(empty_store).state == "current"


@pytest.mark.postgres
def test_upgrade_needs_the_postgres_schema(
    postgres_uri: str,
    missing_schema: str,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    flags = ["--db-uri", postgres_uri, "--db-schema", missing_schema]
    assert cli.main(["db", "upgrade", *flags]) == 2
    assert (
        f"dbtx: the schema '{missing_schema}' does not exist in {masked(postgres_uri)}; "
        f"run `dbtx db init`"
    ) in capsys.readouterr().err
    assert not schema_exists(postgres_uri, missing_schema)


@pytest.mark.parametrize("store", ON_SQLITE_FILE_AND_POSTGRES, indirect=True)
def test_upgrade_refuses_a_revision_this_dbtx_does_not_know(
    store: Store,
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_revision(store, "ffff")
    monkeypatch.chdir(platform)
    assert cli.main(["db", "upgrade", *db_flags(store)]) == 2
    assert "is at revision 'ffff', which this dbtx does not know" in capsys.readouterr().err
    assert revision(store).current == "ffff"


# --------------------------------------------------------------------------------------
# dbtx db dispatch
# --------------------------------------------------------------------------------------


def test_an_unknown_db_subcommand_shows_the_usage(
    platform: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(platform)
    assert cli.main(["db", "nope"]) == 2
    assert capsys.readouterr().err == (
        "dbtx: unknown command `dbtx db nope`; usage: dbtx db init | upgrade | status | "
        "load | projects [rebind <name> --folder <folder> [--force]]\n"
    )
