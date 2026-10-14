"""Migrations (`dbtx.store.migrate`, `dbtx/store/migrations/`): plan D4 and Phase 3.

Behaviour:
* Alembic runs programmatically, from the package: no `alembic.ini`. The head is `0002`
  (spec v3), after the initial revision `0001`
* `revision_status(connection, schema)`: the database's revision against head, as one
  of `uninitialised` (no revision), `current` (at head), `behind` (an older revision
  dbtx knows) or `unknown` (a revision dbtx does not know: migrated by a newer dbtx)
* `upgrade(store)` migrates to head and returns (revision before, revision after). On
  an empty database it creates exactly the models' schema (`compare_metadata` finds
  no difference) with the models' constraint and index names. Upgrading again changes
  nothing; upgrading from an unknown revision is a StoreError
* `0002` adds `invocation.run_folder`, `node_result.compiled_code` and
  `node_result.relation_name` to a database at `0001`, keeping its rows (the new columns
  NULL); downgrading to `0001` removes them again
* downgrading to base removes every dbtx table
* on Postgres every table, and Alembic's version table, lives in DB_SCHEMA
* the JSON columns use the serving adapter's `json_type()`, a DB_ADAPTER plugin's too
* the `store` fixture (create_all) is at head, like a migrated database

A revision after head comes from the `extra_revision` fixture (a copy of the migrations
plus an empty `0003`), so "behind head" is testable while only `0002` exists.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from dbtx.store import Store, migrate
from dbtx.store import models as m
from dbtx.store import open_store
from dbtx.store.errors import StoreError
from tests.unit.fake_store_adapters import CustomSqliteAdapter

from .conftest import ON_POSTGRES, store_config

MODEL_TABLES = sorted(table.name for table in m.Base.metadata.sorted_tables)


def status(store: Store) -> migrate.RevisionStatus:
    with store.engine.connect() as connection:
        return migrate.revision_status(connection, store.schema)


def model_differences(store: Store) -> list:
    """What Alembic's autogenerate would change to make the database match the models."""
    with store.engine.connect() as connection:
        if store.schema is not None:
            # Reflection reads the default schema; the tables live in DB_SCHEMA.
            connection.exec_driver_sql(f'SET search_path TO "{store.schema}"')
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return compare_metadata(context, m.Base.metadata)


def table_names(store: Store) -> list[str]:
    with store.engine.connect() as connection:
        return sorted(sa.inspect(connection).get_table_names(schema=store.schema))


def migrate_to(store: Store, revision: str) -> None:
    with store.engine.begin() as connection:
        command.upgrade(migrate.alembic_config(connection, store.schema), revision)


def downgrade_to_base(store: Store) -> None:
    with store.engine.begin() as connection:
        command.downgrade(migrate.alembic_config(connection, store.schema), "base")


def database_names(store: Store) -> set[tuple[str, str, str]]:
    """(kind, table, name) of every key, constraint and index the database reports."""
    names = set()
    with store.engine.connect() as connection:
        inspector = sa.inspect(connection)
        for table in MODEL_TABLES:
            names.add(("pk", table, inspector.get_pk_constraint(table, schema=store.schema)["name"]))
            for fk in inspector.get_foreign_keys(table, schema=store.schema):
                names.add(("fk", table, fk["name"]))
            for index in inspector.get_indexes(table, schema=store.schema):
                names.add(("ix", table, index["name"]))
            for unique in inspector.get_unique_constraints(table, schema=store.schema):
                names.add(("uq", table, unique["name"]))
            for check in inspector.get_check_constraints(table, schema=store.schema):
                names.add(("ck", table, check["name"]))
    return names


def model_names() -> set[tuple[str, str, str]]:
    names = set()
    for table in m.Base.metadata.sorted_tables:
        names.add(("pk", table.name, table.primary_key.name))
        for constraint in table.constraints:
            if isinstance(constraint, sa.ForeignKeyConstraint):
                names.add(("fk", table.name, constraint.name))
            elif isinstance(constraint, sa.UniqueConstraint):
                names.add(("uq", table.name, constraint.name))
            elif isinstance(constraint, sa.CheckConstraint):
                names.add(("ck", table.name, constraint.name))
        for index in table.indexes:
            names.add(("ix", table.name, index.name))
    return names


# --------------------------------------------------------------------------------------
# The script directory
# --------------------------------------------------------------------------------------


def test_head_is_the_v3_revision() -> None:
    assert migrate.head_revision() == "0002"


def test_the_extra_revision_fixture_moves_head(extra_revision: str) -> None:
    assert migrate.head_revision() == extra_revision == "0003"


# --------------------------------------------------------------------------------------
# Revision status
# --------------------------------------------------------------------------------------


def test_an_empty_database_is_uninitialised(empty_store: Store) -> None:
    assert status(empty_store) == migrate.RevisionStatus(current=None, head="0002", state="uninitialised")


def test_an_upgraded_database_is_current(empty_store: Store) -> None:
    migrate.upgrade(empty_store)
    assert status(empty_store) == migrate.RevisionStatus(current="0002", head="0002", state="current")


def test_the_store_fixture_is_at_head(store: Store) -> None:
    # `store` builds its tables with create_all (fast), and records head, so the
    # start-up checks see a current database. test_upgrade_creates_exactly_the_models_schema
    # proves the migrations build the same schema.
    assert status(store) == migrate.RevisionStatus(current="0002", head="0002", state="current")


def test_a_database_at_an_older_revision_is_behind(empty_store: Store, extra_revision: str) -> None:
    migrate_to(empty_store, "0002")
    assert status(empty_store) == migrate.RevisionStatus(current="0002", head="0003", state="behind")


def test_a_revision_dbtx_does_not_know_is_unknown(empty_store: Store) -> None:
    migrate.upgrade(empty_store)
    with empty_store.engine.begin() as connection:
        version_table = sa.table("alembic_version", sa.column("version_num"), schema=empty_store.schema)
        connection.execute(sa.update(version_table).values(version_num="ffff"))
    assert status(empty_store) == migrate.RevisionStatus(current="ffff", head="0002", state="unknown")


# --------------------------------------------------------------------------------------
# upgrade
# --------------------------------------------------------------------------------------


def test_upgrade_creates_exactly_the_models_schema(empty_store: Store) -> None:
    assert migrate.upgrade(empty_store) == (None, "0002")
    assert model_differences(empty_store) == []
    assert table_names(empty_store) == sorted(MODEL_TABLES + ["alembic_version"])


def test_upgrade_uses_the_models_constraint_and_index_names(empty_store: Store) -> None:
    migrate.upgrade(empty_store)
    assert database_names(empty_store) == model_names()


def test_upgrade_again_changes_nothing(empty_store: Store) -> None:
    migrate.upgrade(empty_store)
    assert migrate.upgrade(empty_store) == ("0002", "0002")
    assert model_differences(empty_store) == []


def test_upgrade_from_behind_reaches_head(empty_store: Store, extra_revision: str) -> None:
    migrate_to(empty_store, "0002")
    assert migrate.upgrade(empty_store) == ("0002", "0003")
    assert status(empty_store).state == "current"


def test_upgrade_from_an_unknown_revision_is_an_error(empty_store: Store) -> None:
    migrate.upgrade(empty_store)
    with empty_store.engine.begin() as connection:
        version_table = sa.table("alembic_version", sa.column("version_num"), schema=empty_store.schema)
        connection.execute(sa.update(version_table).values(version_num="ffff"))
    with pytest.raises(StoreError) as info:
        migrate.upgrade(empty_store)
    assert str(info.value) == (
        f"the database {empty_store.masked_uri()} is at revision 'ffff', which this dbtx "
        f"does not know (head is '0002'); it was migrated by a newer dbtx"
    )


def column_names(store: Store, table: str) -> list[str]:
    with store.engine.connect() as connection:
        return [column["name"] for column in sa.inspect(connection).get_columns(table, schema=store.schema)]


NEW_IN_0002 = {"invocation": ["run_folder"], "node_result": ["compiled_code", "relation_name"]}


def test_0002_adds_the_v3_columns_and_keeps_the_rows(empty_store: Store) -> None:
    migrate_to(empty_store, "0001")
    for table, columns in NEW_IN_0002.items():
        for column in columns:
            assert column not in column_names(empty_store, table)
    project = sa.table("project", sa.column("project"), sa.column("folder"), sa.column("claimed_at"),
                       sa.column("last_seen_at"), schema=empty_store.schema)
    with empty_store.engine.begin() as connection:
        connection.execute(sa.insert(project).values(
            project="jaffle_shop", folder="jaffle-shop", claimed_at="2026-10-01 00:00:00.000000",
            last_seen_at="2026-10-01 00:00:00.000000"))

    assert migrate.upgrade(empty_store) == ("0001", "0002")
    for table, columns in NEW_IN_0002.items():
        assert column_names(empty_store, table)[-len(columns):] == columns
    assert model_differences(empty_store) == []
    with empty_store.session() as session:
        assert session.get(m.Project, "jaffle_shop").folder == "jaffle-shop"


def test_downgrading_0002_removes_the_v3_columns(empty_store: Store) -> None:
    migrate.upgrade(empty_store)
    migrate_to_downgrade(empty_store, "0001")
    for table, columns in NEW_IN_0002.items():
        for column in columns:
            assert column not in column_names(empty_store, table)
    assert status(empty_store) == migrate.RevisionStatus(current="0001", head="0002", state="behind")


def migrate_to_downgrade(store: Store, revision: str) -> None:
    with store.engine.begin() as connection:
        command.downgrade(migrate.alembic_config(connection, store.schema), revision)


def test_downgrade_to_base_removes_every_table(empty_store: Store) -> None:
    migrate.upgrade(empty_store)
    downgrade_to_base(empty_store)
    assert table_names(empty_store) == ["alembic_version"]
    assert status(empty_store).state == "uninitialised"


@pytest.mark.parametrize("empty_store", ON_POSTGRES, indirect=True)
def test_postgres_tables_and_the_version_table_live_in_db_schema(empty_store: Store) -> None:
    migrate.upgrade(empty_store)
    assert table_names(empty_store) == sorted(MODEL_TABLES + ["alembic_version"])
    with empty_store.engine.connect() as connection:
        public_tables = sa.inspect(connection).get_table_names(schema="public")
    for name in MODEL_TABLES + ["alembic_version"]:
        assert name not in public_tables


def test_migrations_use_the_adapters_json_type(
    tmp_path: Path, adapter_plugins: Callable[[dict[str, str]], None]
) -> None:
    adapter_plugins({"custom_sqlite": "tests.unit.fake_store_adapters:CustomSqliteAdapter"})
    store = open_store(store_config(f"sqlite:///{tmp_path / 'custom.db'}", adapter="custom_sqlite"))
    try:
        assert type(store.adapter) is CustomSqliteAdapter
        migrate.upgrade(store)
        with store.engine.connect() as connection:
            columns = connection.exec_driver_sql("PRAGMA table_info(invocation)").all()
        declared_types = {}
        for column in columns:
            declared_types[column[1]] = column[2]
        assert declared_types["args"] == "CUSTOMJSON"
    finally:
        store.dispose()
