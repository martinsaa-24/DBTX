"""Store adapters: engines and connections, `open_store`, and integrity enforcement.

Behaviour (plan sections 3.2 and 4):

`open_store(config)`
* builds the engine through the adapter for DB_URI (DB_ADAPTER when set, else the
  default for the URI), with DB_SCHEMA only on a backend that has schemas; it does not
  connect
* attaches that adapter to the engine's dialect, so JSON columns use its `json_type()`
* refuses a configuration without a database: standalone mode, a disabled backend, or
  no DB_URI
* `store.begin()` is a unit of work: committed on exit, rolled back on an error

SQLite (`SqliteAdapter`)
* every connection, not only the first: `foreign_keys=ON`, `busy_timeout=5000`,
  `synchronous=NORMAL`, and `journal_mode=WAL` for a file
* every transaction takes the write lock when it begins (`BEGIN IMMEDIATE`), so two
  writers queue on `busy_timeout` instead of failing with "database is locked"
* a file's parent directory is created; `:memory:` is one database across connections
* a schema is refused: SQLite has none

Postgres (`PostgresAdapter`)
* every connection's session time zone is UTC, whatever the URI or server asks for
* every table lives in DB_SCHEMA (`schema_translate_map`); a schema is required
* a connection the server dropped is replaced, not handed out (`pool_pre_ping`)

Every backend
* foreign keys and CHECK constraints are enforced by the database
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from dbtx.config import Setting
from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store import open_store
from dbtx.store.adapters import PostgresAdapter, SqliteAdapter, attached_adapter
from dbtx.store.errors import StoreError
from dbtx.store.types import JSONType
from tests.unit.fake_store_adapters import CustomSqliteAdapter, FakeDbAdapter

from .conftest import (
    ON_POSTGRES,
    ON_SQLITE,
    ON_SQLITE_FILE,
    ON_SQLITE_MEMORY,
    load_spec,
    spec_tables,
    store_config,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def a_project(name: str = "jaffle_shop") -> m.Project:
    return m.Project(project=name, folder="jaffle-shop", claimed_at=T0, last_seen_at=T0)


# --------------------------------------------------------------------------------------
# open_store
# --------------------------------------------------------------------------------------


def test_open_store_uses_the_adapter_for_the_uri(store: Store) -> None:
    if store.test_backend == "postgres":
        assert type(store.adapter) is PostgresAdapter
    else:
        assert type(store.adapter) is SqliteAdapter
    assert store.engine.dialect.name == store.adapter.dialect


def test_open_store_passes_db_schema_only_to_backends_with_schemas(store: Store) -> None:
    if store.test_backend == "postgres":
        assert store.schema is not None and store.schema.startswith("dbtx_test_")
    else:
        # The config holds the default "dbtx"; SQLite has no schemas.
        assert store.schema is None


def test_open_store_attaches_its_adapter_to_the_dialect(store: Store) -> None:
    assert attached_adapter(store.engine.dialect) is store.adapter


def json_column_ddl(store: Store) -> str:
    table = sa.Table("json_probe", sa.MetaData(), sa.Column("doc", JSONType, nullable=True))
    return str(sa.schema.CreateTable(table).compile(dialect=store.engine.dialect))


def test_open_store_uses_db_adapter(adapter_plugins: Callable[[dict[str, str]], None]) -> None:
    adapter_plugins({"custom_sqlite": "tests.unit.fake_store_adapters:CustomSqliteAdapter"})
    store = open_store(store_config("sqlite://", adapter="custom_sqlite"))
    try:
        assert type(store.adapter) is CustomSqliteAdapter
        assert attached_adapter(store.engine.dialect) is store.adapter
        # JSON columns use the attached adapter's type, not the built-in's.
        assert "doc CUSTOMJSON" in json_column_ddl(store)
    finally:
        store.dispose()


def test_open_store_without_db_adapter_uses_the_default(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": "tests.unit.fake_store_adapters:CustomSqliteAdapter"})
    store = open_store(store_config("sqlite://"))
    try:
        assert type(store.adapter) is SqliteAdapter
        assert "doc JSON" in json_column_ddl(store)
        assert "CUSTOMJSON" not in json_column_ddl(store)
    finally:
        store.dispose()


def test_which_adapters_upgrade_automatically() -> None:
    # A SQLite file is local and dbtx owns it: the start-up checks migrate it. A shared
    # server is upgraded only by `dbtx db upgrade`. Plugins inherit the default (no).
    assert SqliteAdapter.upgrades_automatically is True
    assert PostgresAdapter.upgrades_automatically is False
    assert FakeDbAdapter.upgrades_automatically is False


def test_open_store_refuses_standalone_mode() -> None:
    config = replace(store_config("sqlite://"), mode="standalone", db_backend=None, db_uri=None, db_schema=None)
    with pytest.raises(StoreError, match="standalone mode has no database"):
        open_store(config)


def test_open_store_refuses_a_disabled_backend() -> None:
    config = replace(store_config("sqlite://"), db_backend=Setting("disabled", "cli", None))
    with pytest.raises(StoreError, match="the database backend is disabled"):
        open_store(config)


def test_open_store_refuses_a_missing_uri() -> None:
    config = replace(store_config("sqlite://"), db_uri=Setting(None, "unset", None))
    with pytest.raises(StoreError, match="DB_URI is not set"):
        open_store(config)


def test_open_store_does_not_connect() -> None:
    # Nothing listens on port 1: connecting fails at once, so this needs no server.
    config = store_config("postgresql+psycopg://dbtx@127.0.0.1:1/dbtx?connect_timeout=2")
    store = open_store(config)
    try:
        with pytest.raises(sa.exc.OperationalError):
            with store.engine.connect():
                pass
    finally:
        store.dispose()


def test_begin_commits_a_unit_of_work(store: Store) -> None:
    with store.begin() as session:
        session.add(a_project())
    with store.session() as session:
        assert session.scalars(sa.select(m.Project.project)).all() == ["jaffle_shop"]


def test_begin_rolls_back_on_an_error(store: Store) -> None:
    with pytest.raises(RuntimeError):
        with store.begin() as session:
            session.add(a_project())
            session.flush()
            raise RuntimeError("boom")
    with store.session() as session:
        assert session.scalars(sa.select(m.Project.project)).all() == []


def test_masked_uri_hides_the_password() -> None:
    store = open_store(store_config("postgresql+psycopg://dbtx:s3cret@db/dbtx"))
    try:
        assert store.masked_uri() == "postgresql+psycopg://dbtx:***@db/dbtx"
    finally:
        store.dispose()


# --------------------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------------------


def sqlite_settings(dbapi_connection: sqlite3.Connection) -> dict[str, object]:
    cursor = dbapi_connection.cursor()
    settings = {}
    for pragma in ["foreign_keys", "busy_timeout", "synchronous", "journal_mode"]:
        settings[pragma] = cursor.execute(f"PRAGMA {pragma}").fetchone()[0]
    cursor.close()
    return settings


@pytest.mark.parametrize("store", ON_SQLITE, indirect=True)
def test_sqlite_settings_on_every_connection(store: Store) -> None:
    expected_journal_mode = "wal" if store.test_backend == "sqlite-file" else "memory"
    expected = {
        "foreign_keys": 1,
        "busy_timeout": 5000,
        "synchronous": 1,  # NORMAL
        "journal_mode": expected_journal_mode,
    }
    # Raw DBAPI connections, so no transaction is begun. Two checked out at once are
    # two connections on a file (QueuePool); the memory database has one (StaticPool).
    first = store.engine.raw_connection()
    second = store.engine.raw_connection()
    try:
        assert sqlite_settings(first.driver_connection) == expected
        assert sqlite_settings(second.driver_connection) == expected
    finally:
        second.close()
        first.close()
    # And after the pool is emptied, a brand-new connection gets them too.
    store.engine.dispose()
    third = store.engine.raw_connection()
    try:
        assert sqlite_settings(third.driver_connection) == expected
    finally:
        third.close()


@pytest.mark.parametrize("store", ON_SQLITE_FILE, indirect=True)
def test_sqlite_transactions_take_the_write_lock_when_they_begin(store: Store) -> None:
    path = store.engine.url.database
    with store.engine.connect() as conn:
        with conn.begin():
            # Only a read so far. A deferred transaction would hold no write lock yet.
            conn.execute(sa.select(m.Project.project)).all()
            other = sqlite3.connect(path, timeout=0, isolation_level=None)
            try:
                with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                    other.execute("BEGIN IMMEDIATE")
            finally:
                other.close()
    # Committed: the lock is released.
    other = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
    finally:
        other.close()


@pytest.mark.parametrize("store", ON_SQLITE_MEMORY, indirect=True)
def test_sqlite_memory_is_one_database_across_connections(store: Store) -> None:
    with store.begin() as session:
        session.add(a_project())

    # From another thread too (a per-thread pool would open a new, empty database).
    seen_by_thread = []

    def read() -> None:
        with store.engine.connect() as conn:
            seen_by_thread.extend(conn.execute(sa.select(m.Project.project)).scalars().all())

    thread = threading.Thread(target=read)
    thread.start()
    thread.join(timeout=10)
    assert seen_by_thread == ["jaffle_shop"]


def test_sqlite_file_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "platform" / ".dbtx" / "dbtx.db"
    store = open_store(store_config(f"sqlite:///{path}"))
    try:
        assert path.parent.is_dir()
        with store.engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        assert path.is_file()
    finally:
        store.dispose()


def test_sqlite_adapter_refuses_a_schema() -> None:
    with pytest.raises(StoreError, match="SQLite has no schemas"):
        SqliteAdapter().create_engine("sqlite://", "dbtx")


# --------------------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
def test_postgres_session_time_zone_is_utc_on_every_connection(store: Store) -> None:
    # The fixture's URI asks for Asia/Kolkata (libpq `options`); the adapter wins.
    assert "timezone" in store.uri
    with store.engine.connect() as first, store.engine.connect() as second:
        assert first.exec_driver_sql("SHOW TimeZone").scalar() == "UTC"
        assert second.exec_driver_sql("SHOW TimeZone").scalar() == "UTC"


@pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
def test_postgres_utc_survives_rollbacks_and_pool_reuse(store: Store) -> None:
    store.engine.dispose()
    # A new connection, used only in a transaction that is rolled back...
    with store.engine.connect() as conn:
        pid = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar()
        conn.rollback()
    # ...and rolled back again when the pool takes it back. Reused, it is still UTC.
    with store.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT pg_backend_pid()").scalar() == pid
        assert conn.exec_driver_sql("SHOW TimeZone").scalar() == "UTC"


@pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
def test_postgres_tables_live_in_db_schema(store: Store) -> None:
    inspector = sa.inspect(store.engine)
    # Alembic's version table lives there too (Phase 3).
    expected = sorted(list(spec_tables(load_spec())) + ["alembic_version"])
    assert sorted(inspector.get_table_names(schema=store.schema)) == expected


def test_postgres_adapter_needs_a_schema(postgres_uri: str) -> None:
    with pytest.raises(StoreError, match="needs a schema"):
        PostgresAdapter().create_engine(postgres_uri, None)


@pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
def test_postgres_replaces_a_connection_the_server_dropped(store: Store) -> None:
    with store.engine.connect() as conn:
        pid = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar()
    # The connection is back in the pool. Kill it on the server.
    killer = sa.create_engine(store.uri, poolclass=sa.pool.NullPool)
    try:
        with killer.connect() as conn:
            assert conn.execute(sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}).scalar()
    finally:
        killer.dispose()
    with store.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT 1").scalar() == 1


# --------------------------------------------------------------------------------------
# Integrity: enforced by the database on every backend
# --------------------------------------------------------------------------------------


def test_valid_rows_are_accepted(store: Store) -> None:
    with store.begin() as session:
        session.add(a_project())
        session.flush()
        session.add(
            m.Node(
                project="jaffle_shop", unique_id="model.jaffle_shop.orders", resource_type="model",
                package_name="jaffle_shop", name="orders", first_seen_at=T0, last_seen_at=T0,
            )
        )
    with store.session() as session:
        assert session.scalars(sa.select(m.Node.unique_id)).all() == ["model.jaffle_shop.orders"]


def test_a_dangling_foreign_key_is_rejected(store: Store) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        with store.begin() as session:
            session.add(
                m.Node(
                    project="no_such_project", unique_id="model.x.y", resource_type="model",
                    package_name="x", name="y", first_seen_at=T0, last_seen_at=T0,
                )
            )
    with store.session() as session:
        assert session.scalars(sa.select(m.Node)).all() == []


@pytest.mark.parametrize(
    "values",
    [
        {"status": "queued"},
        {"status": "finished", "result": "ok"},
    ],
    ids=["job.status", "job.result"],
)
def test_a_value_outside_a_check_constraint_is_rejected(
    store: Store,
    values: dict[str, str],
) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        with store.begin() as session:
            session.add(m.Job(job_id="j1", trigger="manual", requested_at=T0, **values))
    with store.session() as session:
        assert session.scalars(sa.select(m.Job)).all() == []
