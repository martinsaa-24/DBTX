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

Two fixtures hand out a database, each parametrised over the three backends:

* `store`: an open `Store` (plan Phase 2), built by `open_store` from a platform-mode
  `DbtxConfig`, exactly as dbtx builds it, with every table created. Adapter tests use
  it. On Postgres the URI asks for a non-UTC session time zone, which the adapter must
  override, and `DB_SCHEMA` is the test's own schema. `empty_store` is the same without
  tables.
* `engine`: a plain SQLAlchemy engine with none of the adapter's settings (no pragmas,
  a non-UTC Postgres session). The column-type and model tests use it, because what
  they prove must hold on any connection, not only on one the adapter configured.

A test for some backends only says so on its signature, with an indirect parametrize
over one of the `ON_*` lists, and is never generated for the others (no skips):

    @pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
    def test_postgres_tables_live_in_db_schema(store): ...
"""

from __future__ import annotations

import uuid
import warnings
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from sqlalchemy.pool import StaticPool

import dbtx.store
import dbtx.store.clock
import dbtx.store.repositories.projects
from dbtx.config import DbtxConfig, Setting
from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store import open_store

SPEC_FILE = Path(dbtx.store.__file__).parent / "schema.yml"

# A time zone with a non-whole-hour offset and no daylight saving time.
POSTGRES_SESSION_TIME_ZONE = "Asia/Kolkata"


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------

CLOCK_START = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


class FakeClock:
    """Stands in for `dbtx.store.clock.now`: a fixed time that moves only when told."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Every store test runs on a fixed clock (`clock.advance(minutes=5)` moves it)."""
    fake = FakeClock(CLOCK_START)
    monkeypatch.setattr(dbtx.store.clock, "now", fake.now)
    return fake


TEST_HOST = "build-01"


@pytest.fixture(autouse=True)
def host(monkeypatch: pytest.MonkeyPatch) -> str:
    """Every store test runs on the host `build-01` (the host name a claim records)."""
    monkeypatch.setattr(dbtx.store.repositories.projects, "current_host", lambda: TEST_HOST)
    return TEST_HOST


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
def postgres_uri(pytestconfig: pytest.Config, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
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


# The backends a test runs on. `store`, `empty_store` and `engine` default to all of
# them; a test for some backends only overrides that with an indirect parametrize:
#   @pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
# Postgres keeps its `postgres` marker either way, so `-m "not postgres"` deselects it.
POSTGRES = pytest.param("postgres", marks=pytest.mark.postgres)
ALL_BACKENDS = ["sqlite-memory", "sqlite-file", POSTGRES]
ON_POSTGRES = [POSTGRES]
ON_SQLITE = ["sqlite-memory", "sqlite-file"]
ON_SQLITE_MEMORY = ["sqlite-memory"]
ON_SQLITE_FILE = ["sqlite-file"]
# Several threads or connections: the in-memory database is one shared connection.
ON_SQLITE_FILE_AND_POSTGRES = ["sqlite-file", POSTGRES]


@pytest.fixture(params=ALL_BACKENDS)
def engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[sa.Engine]:
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


# --------------------------------------------------------------------------------------
# Stores, one per backend (built by open_store, like dbtx builds them)
# --------------------------------------------------------------------------------------


def store_config(uri: str, schema: str | None = None, adapter: str | None = None) -> DbtxConfig:
    """A platform-mode DbtxConfig with the backend enabled, as load_config builds it.

    `schema` None means DB_SCHEMA was not set (the default, "dbtx"). `adapter` None
    means DB_ADAPTER was not set (the default adapter for the URI).
    """
    if adapter is None:
        db_adapter = Setting(None, "default", None)
    else:
        db_adapter = Setting(adapter, "cli", None)
    if schema is None:
        db_schema = Setting("dbtx", "default", None)
    else:
        db_schema = Setting(schema, "cli", None)
    return DbtxConfig(
        mode="platform",
        mode_reason="test",
        platform_root=None,
        project_dir=None,
        config_file=None,
        config_file_reason="test",
        db_backend=Setting("enabled", "default", None),
        db_uri=Setting(uri, "cli", None),
        db_schema=db_schema,
        db_adapter=db_adapter,
        command="build",
        uses_database=True,
        project_name=None,
        colliding_folders=(),
        argv=(),
        warnings=(),
        ignored_env_vars=(),
    )


def postgres_uri_with_session_time_zone(uri: str, time_zone: str) -> str:
    """`uri` asking the server for a session time zone (libpq `options`)."""
    url = sa.make_url(uri).update_query_dict({"options": f"-c timezone={time_zone}"})
    return url.render_as_string(hide_password=False)


def _build_store(request: pytest.FixtureRequest, tmp_path: Path, create_tables: bool) -> Iterator[Store]:
    """Yields a Store on `request.param`'s backend; disposed (Postgres: dropped) after.

    The store gets one test-only attribute, `test_backend` (the parameter). On Postgres
    `store.schema` names the test's own schema.
    """
    backend = request.param

    if backend == "sqlite-memory":
        store = open_store(store_config(SQLITE_MEMORY_URI))
        store.test_backend = backend
        if create_tables:
            m.Base.metadata.create_all(store.engine)
        yield store
        store.dispose()
        return

    if backend == "sqlite-file":
        store = open_store(store_config(sqlite_file_uri(tmp_path)))
        store.test_backend = backend
        if create_tables:
            m.Base.metadata.create_all(store.engine)
        yield store
        store.dispose()
        return

    if backend == "postgres":
        server_uri = request.getfixturevalue("postgres_uri")
        schema_name = f"dbtx_test_{uuid.uuid4().hex[:12]}"
        admin = sa.create_engine(server_uri)
        with admin.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema_name}"'))
        uri = postgres_uri_with_session_time_zone(server_uri, POSTGRES_SESSION_TIME_ZONE)
        store = open_store(store_config(uri, schema_name))
        store.test_backend = backend
        try:
            if create_tables:
                m.Base.metadata.create_all(store.engine)
            yield store
        finally:
            store.dispose()
            with admin.begin() as conn:
                conn.execute(sa.text(f'DROP SCHEMA "{schema_name}" CASCADE'))
            admin.dispose()
        return

    raise AssertionError(f"unknown backend {backend}")


@pytest.fixture(params=ALL_BACKENDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    """An open Store with every table created, on a new, empty database."""
    yield from _build_store(request, tmp_path, create_tables=True)


@pytest.fixture(params=ALL_BACKENDS)
def empty_store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    """An open Store on a new, empty database: no tables."""
    yield from _build_store(request, tmp_path, create_tables=False)

