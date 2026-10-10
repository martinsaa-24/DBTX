"""The test backends themselves (`tests/unit/store/conftest.py`).

Every backend:
* `engine` hands each test a new, empty database.

SQLite:
* `sqlite-memory` is one database for the whole test: every connection sees the same
  in-memory database (without `StaticPool`, each new connection would open a new, empty
  one).
* `sqlite-file` is a file inside the test's own `tmp_path`, so no two tests share it.

Postgres:
* A server is available with no configuration: a throwaway server (pgserver) is started
  once for the test session, and `postgres_uri` is its connection URI.
* `pytest --postgres-uri URI` runs the Postgres tests against that server instead. A
  blank value stops the run with a usage error, never a silent fallback to pgserver.
* The server is UTF8, so text comes back as `str`, never `bytes`.
* The `postgres` case runs in its own schema, with a session time zone that is not UTC.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from ...conftest import check_postgres_uri_option
from .conftest import POSTGRES_SESSION_TIME_ZONE, reflection_schema


# --------------------------------------------------------------------------------------
# Every backend
# --------------------------------------------------------------------------------------


def test_engine_starts_with_an_empty_database(engine):
    inspector = sa.inspect(engine)

    assert inspector.get_table_names(schema=reflection_schema(engine)) == []


# --------------------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["sqlite-memory"], indirect=True)
def test_sqlite_memory_is_one_database_across_connections(engine):
    assert engine.dialect.name == "sqlite"
    assert isinstance(engine.pool, StaticPool)

    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO probe (id) VALUES (1)"))

    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id FROM probe")).scalars().all()

    assert rows == [1]


@pytest.mark.parametrize("engine", ["sqlite-file"], indirect=True)
def test_sqlite_file_lives_in_the_tests_own_tmp_path(engine, tmp_path):
    database_file = Path(engine.url.database)

    assert engine.dialect.name == "sqlite"
    assert database_file.parent == tmp_path

    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE probe (id INTEGER PRIMARY KEY)"))

    assert database_file.is_file()


# --------------------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------------------


@pytest.mark.postgres
def test_postgres_server_is_available_without_configuration(postgres_uri):
    assert postgres_uri, "no Postgres server: postgres_uri is empty"
    assert sa.make_url(postgres_uri).drivername == "postgresql+psycopg"


@pytest.mark.postgres
def test_postgres_server_is_utf8(postgres_uri):
    assert postgres_uri, "no Postgres server: postgres_uri is empty"
    engine = sa.create_engine(postgres_uri)
    try:
        with engine.connect() as conn:
            encoding = conn.execute(sa.text("SHOW server_encoding")).scalar_one()
    finally:
        engine.dispose()

    assert encoding == "UTF8"


@pytest.mark.postgres
@pytest.mark.parametrize("engine", ["postgres"], indirect=True)
def test_postgres_engine_uses_its_own_schema_and_a_non_utc_time_zone(engine):
    with engine.connect() as conn:
        time_zone = conn.execute(sa.text("SHOW timezone")).scalar_one()
        schema_exists = conn.execute(
            sa.text("SELECT count(*) FROM information_schema.schemata WHERE schema_name = :name"),
            {"name": reflection_schema(engine)},
        ).scalar_one()

    assert time_zone == POSTGRES_SESSION_TIME_ZONE
    assert reflection_schema(engine).startswith("dbtx_test_")
    assert schema_exists == 1



# --------------------------------------------------------------------------------------
# --postgres-uri
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [None, "postgresql+psycopg://user:password@localhost:5432/dbtx_test"],
    ids=["not_given", "a_uri"],
)
def test_postgres_uri_option_accepts_no_value_or_a_uri(value):
    check_postgres_uri_option(value)


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
def test_postgres_uri_option_rejects_a_blank_value(value):
    with pytest.raises(pytest.UsageError, match="--postgres-uri"):
        check_postgres_uri_option(value)
