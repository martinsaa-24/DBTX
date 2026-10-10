"""Test fixtures for the database store (`dbtx.store`).

Every database a test sees is new and empty, built for that one test:

* `sqlite-memory`: an in-memory SQLite database (one shared connection).
* `sqlite-file`: a SQLite file inside the test's own `tmp_path`.
* `postgres`: a uniquely named schema on the session's Postgres server, dropped after
  the test. The session time zone is deliberately **not** UTC, so tests prove that
  timestamps come back in UTC whatever the server's session settings are.

The Postgres server is the one given with `pytest --postgres-uri URI` (a server of your
own: CI, or a specific Postgres version). Otherwise it is a throwaway one, started from
the `pixeltable-pgserver` dev dependency **once per test session** in a pytest temp
directory, and stopped and deleted when the session ends. It starts only when a
`postgres` test runs, so `pytest -m "not postgres"` never starts it.

Every backend has a connection URI (`SQLITE_MEMORY_URI`, `sqlite_file_uri()`,
`postgres_uri`), and each engine is built from it, the way dbtx builds one from `DB_URI`.

Store adapters (plan Phase 2) do not exist yet, so these fixtures build plain
SQLAlchemy engines.
"""

from __future__ import annotations

import uuid
import warnings
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy.pool import StaticPool

import dbtx.store

SPEC_FILE = Path(dbtx.store.__file__).parent / "schema.yml"

# A time zone with a non-whole-hour offset and no daylight saving time.
POSTGRES_SESSION_TIME_ZONE = "Asia/Kolkata"


# --------------------------------------------------------------------------------------
# The spec (src/dbtx/store/schema.yml)
# --------------------------------------------------------------------------------------


def load_spec() -> dict:
    """The spec file as shipped in the package, freshly parsed."""
    with SPEC_FILE.open() as handle:
        return yaml.safe_load(handle)


def spec_tables(spec: dict) -> dict[str, tuple[str, dict]]:
    """Every entity in the spec: {table name: (group name, table spec)}."""
    tables = {}
    for group_name, group in spec["entities"].items():
        for table_name, table_spec in group.items():
            tables[table_name] = (group_name, table_spec)
    return tables


@pytest.fixture
def spec() -> dict:
    return load_spec()


# --------------------------------------------------------------------------------------
# The Postgres server
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def postgres_uri(pytestconfig, tmp_path_factory):
    """URI of this test session's Postgres server: `--postgres-uri`, or a throwaway one."""
    given_uri = pytestconfig.getoption("postgres_uri")
    if given_uri is not None:
        yield given_uri
        return

    pgdata = tmp_path_factory.mktemp("pgserver") / "pgdata"
    with warnings.catch_warnings():
        # platformdirs (imported by pgserver) warns when XDG_RUNTIME_DIR is unset.
        warnings.simplefilter("ignore")
        try:
            import pixeltable_pgserver
        except ImportError:
            pytest.skip("pixeltable-pgserver is not installed (uv sync installs the dev group)")
        server = pixeltable_pgserver.get_server(pgdata, cleanup_mode="delete")
    try:
        uri = sa.make_url(server.get_uri()).set(drivername="postgresql+psycopg")
        yield uri.render_as_string(hide_password=False)
    finally:
        server.cleanup()


# --------------------------------------------------------------------------------------
# Engines, one per backend
# --------------------------------------------------------------------------------------


SQLITE_MEMORY_URI = "sqlite+pysqlite:///:memory:"


def sqlite_file_uri(tmp_path: Path) -> str:
    """URI of the SQLite file database inside one test's own `tmp_path`."""
    return f"sqlite+pysqlite:///{tmp_path / 'store.db'}"


def _sqlite_memory_engine(uri: str) -> sa.Engine:
    return sa.create_engine(
        uri,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def _sqlite_file_engine(uri: str) -> sa.Engine:
    return sa.create_engine(uri)


def _postgres_engine(uri: str, schema_name: str) -> sa.Engine:
    engine = sa.create_engine(
        uri,
        connect_args={"options": f"-c timezone={POSTGRES_SESSION_TIME_ZONE}"},
    )
    return engine.execution_options(schema_translate_map={None: schema_name})


@pytest.fixture(
    params=[
        "sqlite-memory",
        "sqlite-file",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
def engine(request, tmp_path):
    """A new, empty database on one backend. Disposed (and on Postgres dropped) after."""
    backend = request.param

    if backend == "sqlite-memory":
        engine = _sqlite_memory_engine(SQLITE_MEMORY_URI)
        yield engine
        engine.dispose()
        return

    if backend == "sqlite-file":
        engine = _sqlite_file_engine(sqlite_file_uri(tmp_path))
        yield engine
        engine.dispose()
        return

    if backend == "postgres":
        uri = request.getfixturevalue("postgres_uri")
        schema_name = f"dbtx_test_{uuid.uuid4().hex[:12]}"
        admin = sa.create_engine(uri)
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema_name}"'))
        engine = _postgres_engine(uri, schema_name)
        engine.dbtx_test_schema = schema_name
        try:
            yield engine
        finally:
            engine.dispose()
            with admin.begin() as conn:
                conn.execute(sa.text(f'DROP SCHEMA "{schema_name}" CASCADE'))
            admin.dispose()
        return

    raise AssertionError(f"unknown backend {backend}")


def reflection_schema(engine: sa.Engine) -> str | None:
    """The schema to pass to the inspector: the test's own schema on Postgres."""
    return getattr(engine, "dbtx_test_schema", None)
