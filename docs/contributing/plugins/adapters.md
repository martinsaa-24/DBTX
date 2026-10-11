# Store Adapter Plugins

dbtx records jobs, invocations, nodes and reports in a database (`DB_URI`). SQLite and
Postgres are built in. A **store adapter plugin** is a separately installed Python
package that provides a `StoreAdapter` subclass and registers it, **under a name**, in
the `dbtx.store_adapters` entry-point group. A plugin can:

- **add a backend** dbtx has no built-in for, e.g. DuckDB (`duckdb+duckdb_engine://`)
- **add a driver** for a built-in backend, e.g. SQLite through `pysqlcipher`
  (`sqlite+pysqlcipher://`)
- **change behaviour** for a built-in backend and driver, e.g. a Postgres adapter tuned
  for a particular hosted service. Users opt in with `DB_ADAPTER`

Everything that differs between database backends lives in the adapter. The
repositories and the start-up checks never branch on the dialect; they ask the adapter.
So a plugin is the whole job of supporting a backend. No other part of dbtx changes.

Where to look in the code:

| Module | What it holds |
|---|---|
| [`src/dbtx/store/adapters/base.py`](../../../src/dbtx/store/adapters/base.py) | `StoreAdapter`, the interface. The docstrings there are the reference. |
| [`src/dbtx/store/adapters/__init__.py`](../../../src/dbtx/store/adapters/__init__.py) | The registry: `ENTRY_POINT_GROUP`, `adapter_names`, `adapter_class_by_name`, `default_adapter`, `resolve_adapter`, `get_adapter` |
| [`src/dbtx/store/adapters/sqlite.py`](../../../src/dbtx/store/adapters/sqlite.py) | The SQLite adapter, a worked example without schemas |
| [`src/dbtx/store/adapters/postgres.py`](../../../src/dbtx/store/adapters/postgres.py) | The Postgres adapter, a worked example with schemas and row locks |
| [`src/dbtx/store/types.py`](../../../src/dbtx/store/types.py) | The column types every model uses (`UTCDateTime`, `JSONType`) |
| [`src/dbtx/config.py`](../../../src/dbtx/config.py) | `validate_db_uri` / `validate_db_adapter_name` / `validate_db_schema`: the checks an adapter's attributes drive |

## Which adapter serves `DB_URI`

dbtx reads the **backend** and **driver** from `DB_URI`
(`make_url(uri).get_backend_name()` / `get_driver_name()`): `sqlite+pysqlcipher:///x.db`
is backend `sqlite`, driver `pysqlcipher`. Then:

1. **`DB_ADAPTER` set**: the adapter with that name. Its `dialect` must be the URI's
   backend, and it must support the URI's driver. Otherwise it is a config error.
2. **`DB_ADAPTER` not set**: the built-in for the backend, if it supports the driver.
   **Installing a plugin never changes this.** This path loads no plugin.
3. Otherwise, **the one plugin** whose `dialect` is the backend and which supports the
   driver. If several plugins qualify, it is a config error asking for `DB_ADAPTER`.
   If none do, it is an "unsupported" error.

With a plugin `custom_sqlite` installed that supports `pysqlite` and `pysqlcipher`:

| `DB_URI` | `DB_ADAPTER` | Adapter |
|---|---|---|
| `sqlite:///x.db` | not set | `sqlite` (built in) |
| `sqlite+pysqlite:///x.db` | not set | `sqlite` (built in) |
| `sqlite+pysqlcipher:///x.db` | not set | `custom_sqlite`: the built-in lacks `pysqlcipher` |
| `sqlite:///x.db` | `sqlite` | `sqlite` |
| `sqlite:///x.db` | `custom_sqlite` | `custom_sqlite` |
| `sqlite+pysqlcipher:///x.db` | `sqlite` | error: `store adapter 'sqlite' does not support driver 'pysqlcipher'; it supports: pysqlite` |

`dbtx config` shows the result, e.g. `DB_ADAPTER  custom_sqlite  [default for
sqlite+pysqlcipher]` or `DB_ADAPTER  custom_sqlite  [file /srv/dbtx/dbtx.conf:2]`.

### Loading, and the errors you may see

Plugin **names** are read from the installed entry points without importing anything.
A plugin is **loaded** when `DB_ADAPTER` names it, or when step 2 fails and dbtx has to
look at every plugin (step 3). Every failure is a config error (exit 2) before dbt
starts:

```
dbtx: DB_ADAPTER (cli): unknown store adapter 'nope'; available: sqlite, postgresql, custom_sqlite
dbtx: DB_ADAPTER (cli): store adapter 'postgresql' serves postgresql, not sqlite
dbtx: DB_URI (cli): several store adapters serve sqlite+pysqlcipher: custom_sqlite, other_sqlite; choose one with DB_ADAPTER
dbtx: DB_URI (cli): unsupported backend 'duckdb'; supported: sqlite, postgresql
dbtx: DB_URI (cli): store adapter plugin 'duckdb' (dbtx_duckdb.adapter:DuckdbAdapter) could not be loaded: No module named 'duckdb_engine'
dbtx: DB_URI (cli): store adapter plugin 'duckdb' (...) is not a StoreAdapter subclass
dbtx: DB_URI (cli): store adapter plugin 'duckdb' (...): driver_package has no entry for driver 'x'
dbtx: DB_URI (cli): two store adapter plugins are named 'duckdb' (a.mod:A, b.mod:B); uninstall one
```

In step 3 a broken plugin is an error even when another plugin would have served the
URI. Fix or uninstall it. SQLite and Postgres users on the built-in drivers never reach
step 3, so a broken plugin cannot affect them.

## Registering the entry point

In the plugin's `pyproject.toml`:

```toml
[project]
name = "dbtx-duckdb"
dependencies = [
    "dbtx>=0.1,<0.2",        # the StoreAdapter interface you implement
    "duckdb-engine>=0.13",   # the SQLAlchemy dialect and its DBAPI driver
]

[project.entry-points."dbtx.store_adapters"]
duckdb = "dbtx_duckdb.adapter:DuckdbAdapter"
```

- **The name** (`duckdb`) is the adapter's name: what users write in `DB_ADAPTER`, and
  what errors and `dbtx config` show. Choose a stable, specific name, because renaming
  it breaks configs. `sqlite` and `postgresql` are reserved for the built-ins: an entry
  point with either name is ignored. Two installed plugins must not share a name.
- **The value** is `module:ClassName`, pointing at the `StoreAdapter` subclass.
- **Depend on the SQLAlchemy dialect and the DBAPI driver** your adapter uses, so that
  installing the plugin installs everything the backend needs. If a driver is optional,
  set `driver_package` and `install_hint` so that dbtx can say what is missing (see
  below).
- **Pin a dbtx version range.** The store is still being built out in phases (see
  [`docs/agentic/orm_implementation_plan.md`](../../agentic/orm_implementation_plan.md)),
  and later phases may add abstract methods to `StoreAdapter`. If that happens, a plugin
  written against an older interface fails to instantiate.

## The interface

Subclass `dbtx.store.adapters.base.StoreAdapter`. To change one thing about a built-in
backend, subclass `SqliteAdapter` or `PostgresAdapter` instead and override only what
differs. Set every class attribute and implement every abstract method. `base.py`
documents each one; this section adds what dbtx does with them. For why the interface
looks the way it does, see [the adapter architecture](../architecture/adapters.md).

### Class attributes

| Attribute | Type | What dbtx does with it |
|---|---|---|
| `dialect` | `str` | The backend this adapter serves. Must equal `make_url(uri).get_backend_name()` and the SQLAlchemy dialect's `name` (see [the dialect name](#the-dialect-name-must-match)) |
| `display_name` | `str` | The backend's name in messages ("SQLite", "Postgres") |
| `driver` | `str` or `list[str]` | The DBAPI driver(s) this adapter supports, e.g. `"pysqlite"` or `["pysqlite", "pysqlcipher"]`. Compared with `make_url(uri).get_driver_name()`; any other driver is refused |
| `driver_package` | `str \| None`, or `dict[str, str \| None]` | The module each driver needs importable when the backend is enabled. One value applies to every driver; a dict needs an entry per driver in `driver`. Checked with `importlib.util.find_spec`, never imported. `None` when the driver ships with Python |
| `driver_hint` | `str` | Shown when the driver is wrong: what to write in `DB_URI` instead, e.g. `"duckdb+duckdb_engine://"` |
| `install_hint` | `str` | Shown when a driver package is missing, e.g. `"pip install dbtx-duckdb"` |
| `supports_schemas` | `bool` | Whether `DB_SCHEMA` applies. If `False`, setting `DB_SCHEMA` explicitly is a config error, and `create_engine` receives `schema=None` |
| `minimum_server_version` | `tuple[int, ...]` | The oldest server version that works. `check_server` refuses older ones at start-up |
| `minimum_server_reason` | `str` | Why that version, shown in the refusal ("RETURNING needs 3.35") |

### Abstract methods

**`create_engine(uri, schema) -> sa.Engine`**
Build the engine and attach `on_connect` (and any other event listeners) to it. Do
**not** connect: `open_store` builds the engine before any connection is wanted. With
`supports_schemas = True`, `schema` is `DB_SCHEMA` and every dbtx table must live in it.
The models have no schema of their own, so the Postgres adapter uses
`execution_options(schema_translate_map={None: schema})`. Raise `StoreError` for a
URI or schema you cannot serve. If you support several drivers, the URI tells you
which one is in use.

**`on_connect(dbapi_connection, connection_record) -> None`**
Runs on every new DBAPI connection. Use it for per-connection settings: foreign-key
enforcement, time zone, busy timeouts, journal mode.

**`server_version(connection) -> tuple[int, ...]`**
The server's version as integers. `check_server` compares it with
`minimum_server_version`.

**`insert(table)`**
The dialect's `insert()` construct. It must support `.excluded`,
`.on_conflict_do_update(index_elements=..., set_=...)` and
`.on_conflict_do_nothing(index_elements=...)`, as SQLAlchemy's SQLite and Postgres
inserts do. `upsert()` is built on it.

**`greatest(*columns)` / `least(*columns)`**
The largest or smallest of two or more values, as a **scalar** expression (not an
aggregate). Raise `ValueError` for fewer than two values, and keep the type of the
first column (`type_=columns[0].type`).

**`claim_next_job(session)`**
Atomically move the oldest pending job to running and return it, or return `None`.
"Oldest" means ordered by `(Job.requested_at, Job.job_id)`. Set `status` to
`JobStatus.RUNNING.value` and `start_time` to `dbtx.store.clock.now()`. The rule that
matters: **two workers calling this at the same time must never get the same job.**
SQLite gets this from `BEGIN IMMEDIATE` plus a single `UPDATE ... RETURNING`, and
Postgres from `SELECT ... FOR UPDATE SKIP LOCKED`. Return the ORM object with current
values (`populate_existing=True`). Do not commit: the caller owns the transaction.

### Concrete methods

`check_server()`, `upsert()` and `json_type()` are implemented in the base class.

**`json_type() -> TypeEngine`**: override this when the backend has a better JSON type.
The base class returns `sa.JSON(none_as_null=True)`. `PostgresAdapter` returns
`JSONB(none_as_null=True)`. Every JSON column in the models (`types.JSONType`) is
stored in the type the serving adapter returns here. A `DB_ADAPTER` override is
included: `open_store` attaches the adapter to the engine, and `JSONType` asks it. Two
rules:

- Keep `none_as_null=True`. Python `None` must be stored as SQL `NULL`, never as the
  JSON literal `null`.
- Return **your dialect's own type**. dbtx passes it through
  `dialect.type_descriptor()`, which uses the dialect's `colspecs` to swap generic types
  for the dialect's own versions. A plain `sa.JSON` subclass of your own is swapped
  for the dialect's generic JSON unless your dialect lists it in `colspecs`, as SQLite
  lists its `JSONB`. Types that subclass the dialect's own JSON implementation, like
  Postgres's `JSONB`, are kept.

`upsert()` checks its arguments before building any SQL, so that a mistake cannot write
different data on different backends. If you override it, keep those checks.

### The dialect name must match

Your adapter's `dialect` must equal both:

- **the URI's backend name**, `make_url(uri).get_backend_name()`, which is how dbtx
  matches an adapter to `DB_URI`
- **the SQLAlchemy dialect's `name`**. For a SQLAlchemy dialect that no store built,
  for example in a DDL script, `JSONType` uses the built-in for `dialect.name`, or
  else the default adapter for `(dialect.name, dialect.driver)`

For SQLite, Postgres and almost every third-party dialect these are the same string.
The adapter's **name** (the entry point) is separate and can be anything.

### What the backend itself must provide

The adapter cannot paper over everything. The shared models assume a backend that:

- enforces **foreign keys** and **CHECK constraints** (turn them on in `on_connect` if
  they are off by default)
- can store JSON in the type `json_type()` returns (generic `sa.JSON` unless you
  override it)
- stores `sa.DateTime(timezone=True)`, either time-zone aware or as naive UTC.
  `UTCDateTime` converts every value to UTC before writing, and reads naive values back
  as UTC
- supports `INSERT ... ON CONFLICT` (through `insert()` above) and transactions that
  give `claim_next_job` its atomicity
- accepts the named constraints from `NAMING_CONVENTION` in `models/base.py`

### Skeletons

A new backend:

```python
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.store.adapters.base import StoreAdapter


class DuckdbAdapter(StoreAdapter):
    dialect = "duckdb"
    display_name = "DuckDB"
    driver = "duckdb_engine"
    driver_package = "duckdb_engine"
    driver_hint = "duckdb+duckdb_engine:///path.db"
    install_hint = "pip install dbtx-duckdb"
    supports_schemas = False
    minimum_server_version = (1, 0)
    minimum_server_reason = "the oldest DuckDB release this plugin is tested against"

    # Optional: the default, sa.JSON(none_as_null=True), is often enough.
    # def json_type(self) -> sa.types.TypeEngine: ...

    def create_engine(self, uri: str, schema: Optional[str]) -> sa.Engine: ...
    def on_connect(self, dbapi_connection, connection_record) -> None: ...
    def server_version(self, connection: sa.Connection) -> tuple[int, ...]: ...
    def insert(self, table: sa.Table): ...
    def greatest(self, *columns): ...
    def least(self, *columns): ...
    def claim_next_job(self, session: Session): ...
```

Another driver for a built-in backend, reusing the built-in's behaviour:

```python
from dbtx.store.adapters.sqlite import SqliteAdapter


class CipherSqliteAdapter(SqliteAdapter):
    display_name = "SQLite (SQLCipher)"
    driver = ["pysqlite", "pysqlcipher"]
    driver_package = {"pysqlite": None, "pysqlcipher": "sqlcipher3"}
    driver_hint = "sqlite+pysqlcipher://:passphrase@/path.db"
    install_hint = "pip install 'dbtx-sqlcipher'"
```

```toml
[project.entry-points."dbtx.store_adapters"]
sqlcipher = "dbtx_sqlcipher.adapter:CipherSqliteAdapter"
```

`sqlite+pysqlcipher://` URIs then use it by default, while `sqlite://` keeps the
built-in unless a user sets `DB_ADAPTER=sqlcipher`.

These are illustrations only. dbtx does not ship or endorse a DuckDB or SQLCipher
plugin.

## Installing a plugin

dbtx discovers plugins through `importlib.metadata.entry_points()` **in the Python
environment dbtx itself runs in**. A plugin is visible exactly when its package's
distribution metadata (the `entry_points.txt` that a build backend writes) is installed
into that environment.

### Supported install methods

| How dbtx is installed | How to add the plugin |
|---|---|
| A virtualenv / `pip` | `pip install dbtx-duckdb` with the same environment active (or `path/to/venv/bin/pip install ...`) |
| A `uv` project | `uv add dbtx-duckdb`, or `uv pip install dbtx-duckdb` into the project's `.venv` |
| `pipx` | `pipx inject dbtx dbtx-duckdb` |
| `uv tool` | `uv tool install dbtx --with dbtx-duckdb` (repeat the command with every `--with` you need, because it replaces the tool's environment) |
| Plugin development | `pip install -e path/to/dbtx-duckdb` or `uv pip install -e ...`. An editable install writes the entry-point metadata too. **Reinstall after changing the entry point** in `pyproject.toml`, because the metadata is written only at install time |

Any installer that installs a standard wheel or sdist into dbtx's environment works:
the requirement is only the installed metadata. To check, run `dbtx config` from a
platform root with a `DB_ADAPTER` nobody provides. The error lists every adapter dbtx
can see, without loading any:

```console
$ cd /srv/dbtx && dbtx config --db-adapter nosuch
dbtx: DB_ADAPTER (cli): unknown store adapter 'nosuch'; available: sqlite, postgresql, duckdb
```

### Not supported

- **Putting the module on `PYTHONPATH` or `sys.path`** without installing it. Importable
  code without distribution metadata registers no entry point, so dbtx never sees it.
- **Installing into a different environment** than the one running `dbtx`, e.g. the
  system Python while dbtx runs from a pipx or `uv tool` environment.
- **Naming code from configuration.** `DB_ADAPTER` takes an adapter **name** from an
  installed entry point, never a module path or class.
- **Taking over a built-in silently.** A plugin that serves `sqlite+pysqlite` or
  `postgresql+psycopg` is used only when `DB_ADAPTER` names it. Entry points named
  `sqlite` or `postgresql` are ignored.
- **Two plugins with the same name.** dbtx refuses to choose between them.

### `DB_URI` for a plugin backend

Write the driver explicitly, `backend+driver://...`, matching one of the adapter's
`driver`s and its `driver_hint`. The driver check refuses any driver the adapter does
not list. Without `+driver`, SQLAlchemy has to load the backend's dialect to find its
default driver. If the dialect is not installed, dbtx refuses the URI and shows the
plugin's `install_hint` (when a driver package is missing) or its `driver_hint`.
dbtx resolves relative paths only in SQLite URIs, whichever adapter serves them. A
plugin's `create_engine` receives any other URI exactly as written.

## Testing a plugin

dbtx's own unit tests hide any plugin installed on the machine (the autouse
`no_adapter_plugins` fixture), so installing a plugin into a dbtx dev environment does
not change dbtx's test results. To see how the registry treats valid and broken plugins,
read `tests/unit/store/test_store_adapter_registry.py` and the fake adapters in
`tests/unit/fake_store_adapters.py` (`CustomSqliteAdapter` is a plugin for the built-in
`sqlite` dialect). The adapter behaviour tests that a real backend should also pass are
in `tests/unit/store/test_store_adapters.py`, `test_store_upsert.py`,
`test_store_job_claim.py` and `test_store_concurrency.py`. See
[`docs/contributing/tests.md`](../tests.md).
