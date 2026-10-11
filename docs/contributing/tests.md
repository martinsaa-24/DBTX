# DBTX Testing Info

How the pytest suite is organized, where test fixtures live, and where a new test goes.
The rules for *writing* tests (red -> green TDD, test fixtures, independence) are in
[guidelines.md](guidelines.md).

## Running the tests

```console
pytest -m "not live"          # fast: unit tests, no warehouse, no dbt parse
pytest -m server              # boots the docs server on a loopback socket
pytest                        # everything, including end-to-end builds of the example project
```

Two markers say what a test needs that the plain suite does not have:

- `live`: runs real dbt or a real server process. Every test under `tests/live/`.
- `postgres`: needs a Postgres server. These are unit tests by structure (the
  `postgres` case of the store's `store` and `engine` fixtures), and they run with no
  setup (the `postgres` extra must be installed: `uv run --extra postgres pytest`): the
  session-scoped `postgres_uri` fixture starts a throwaway server from the
  `pixeltable-pgserver` dev dependency (a maintained fork of `pgserver`; the original
  has no wheels for Python 3.13+), once per session, and deletes it at the end. To use
  a server of your own instead (CI, or a specific Postgres version), pass
  `pytest --postgres-uri postgresql+psycopg://user:password@host:5432/dbname`, or set
  `PYTEST_ADDOPTS="--postgres-uri ..."`; then no pgserver is started. A blank value
  stops the run with a usage error. Each test gets its own schema, dropped afterwards.

They combine: `pytest -m "not live and not postgres"` is the fastest run.

## Layout

```
tests/
  conftest.py              # fixtures shared by every tier (frozen manifest, templates, reports)
  fixtures/                # checked-in test data, never generated during a run
    manifest.json          #   frozen `dbt parse` of examples/jaffle_shop
    config/*.conf          #   sample dbtx.conf files, one case per file (+ decoy.env)
  unit/
    conftest.py            # fixtures for unit tests (env, layouts, fake dbt runner)
    test_<module>.py       # one file per source module, or per functional group
    store/                 # dbtx/store: its own folder, because it has its own fixtures
      conftest.py          #   the spec, an empty database per test on each backend
      test_store_<group>.py
  live/
    conftest.py            # builds the example project once; per-test private copies
    test_<command>.py      # one file per user-facing command or workflow
```

## How tests are organized

There are two tiers, and each tier is organized differently.

### Unit tests: by source module, then by functional group

Unit tests mirror `src/dbtx/`. The default is **one test file per source module**:

| Source module | Test file |
|---|---|
| `dbtx/reports.py` | `test_reports.py` |
| `dbtx/runner.py` | `test_runner.py` |
| `dbtx/scheduler.py` | `test_scheduler.py` |
| `dbtx/templates.py` | `test_templates.py` |
| `dbtx/docs/server.py` | `test_docs_server.py` |
| `dbtx/docs/sidecar.py` (with `catalog.py`, `patch.py`) | `test_docs_sidecar.py` |
| `dbtx/docs/exposure_reports.py` | `test_docs_exposure_reports.py` |

A module in a subpackage gets the package name as a prefix (`dbtx/docs/server.py` →
`test_docs_server.py`), so the unit folder stays flat.

**When a module holds several functional groups, it gets one file per group.** A
functional group is a set of functions that together provide one behavior, the unit
the TDD cycle works on. `dbtx/config.py` is the example:

| Functional group in `dbtx/config.py` | Test file |
|---|---|
| the `dbtx.conf` parser | `test_config_file.py` |
| `load_config`: modes, layers, validation, guard G1 | `test_config.py` |
| how `cli.main` uses the config (checked first, argv handed to dbt, `dbtx config`, `dbtx db`) | `test_cli_config.py` |

Name the file `test_<module>_<group>.py` (or `test_<group>.py` when the group name is
already specific, like `test_config_file.py`). Start each file with a docstring that
states the behavior it proves. Within a file, group tests under comment headers by
behavior, in the order the code runs them.

Two existing files are organized by feature rather than by module:
- `test_docs_server_cli.py`: `dbtx docs serve` argument handling (`dbtx/docs/command.py`)
- `test_example_templates.py`: the example project's templates, rendered with fixed rows

Some modules have no file of their own and are covered through the module that drives
them: `docs/catalog.py`, `docs/patch.py` and `docs/injector.py` through
`test_docs_sidecar.py` and `test_docs_exposure_reports.py`, and `cli.py` through
`test_cli_config.py`, `test_docs_server_cli.py` and the live tests. A change to
one of these modules goes in the file that already exercises it. Give a module its own
file once it has behavior worth proving on its own.

### Live tests: by user-facing command or workflow

Live tests run `dbtx` for real, end to end, on throwaway copies of `examples/jaffle_shop`,
with dbt-duckdb as the warehouse. They are organized by **what a user runs**, not by
module:

| Test file | Covers |
|---|---|
| `test_build.py` | `dbtx build`: selection, failing tests and models, report errors, passthrough |
| `test_compile.py` | `dbtx compile`: validation and report SQL |
| `test_docs_server_boot.py` | the docs server under real uvicorn (also marked `server`) |
| `test_frozen_manifest.py` | `tests/fixtures/manifest.json` still matches the example project |

Every test under `tests/live/` is marked `live` automatically by
`tests/live/conftest.py`, so `pytest -m "not live"` skips the slow tier.

## Where test fixtures live

Put a fixture in the **nearest `conftest.py` that covers every test using it**, and no
higher:

| Fixture file | Holds |
|---|---|
| `tests/conftest.py` | the `--postgres-uri` option (registered here so pytest accepts it whatever is selected), `manifest` (a deep copy of the frozen manifest per test, safe to mutate), `templates`, `reports` |
| `tests/unit/conftest.py` | `clean_env` (autouse: clears `DBTX_*`, `DBT_PROJECT_DIR`, `DBT_PROFILES_DIR`), `no_adapter_plugins` (autouse: hides installed store adapter plugins), `adapter_plugins` (installs fake `dbtx.store_adapters` entry points from `tests/unit/fake_store_adapters.py`, which also holds `FakeDbDialect`, a `fakedb` SQLAlchemy dialect for compiling DDL without a database, and `CustomSqliteAdapter` / `OtherSqliteAdapter`, plugins for the built-in `sqlite` dialect that support `pysqlcipher` too, for `DB_ADAPTER` and default-adapter tests), `enforce`, `config_sample()`, `layout` (a `LayoutBuilder` over `tmp_path`), `platform`, `lone_project`, `fake_dbt` |
| `tests/unit/store/conftest.py` | `clock` (autouse `FakeClock` replacing `dbtx.store.clock.now`), `host` (autouse: the host name a claim records is `build-01`), `spec` (the shipped `schema.yml`), `postgres_uri` (session: `--postgres-uri`, or the URI of a throwaway pgserver), `SQLITE_MEMORY_URI` / `sqlite_file_uri()`, `store` / `empty_store` (an open `Store` built by the real `open_store` from `store_config(uri, schema, adapter)`, with or without tables), `ALL_BACKENDS` and the `ON_*` backend lists (`ON_POSTGRES`, `ON_SQLITE`, `ON_SQLITE_MEMORY`, `ON_SQLITE_FILE`, `ON_SQLITE_FILE_AND_POSTGRES`), `engine` (a plain engine without adapter settings), `create_tables_at_head(store)` (what `store` does: `create_all`, then record the head revision), `extra_revision` (the migrations plus an empty `0002`, so the real head is "behind"), `missing_schema` (a Postgres schema name that does not exist yet), `limited_role_uri` (the test server as a role that cannot create schemas) |
| `tests/live/conftest.py` | `built_project`, `project` |
| a test file | fixtures only that file uses (for example `psycopg_missing` in `test_config.py`) |

**Checked-in data goes in `tests/fixtures/`**, grouped by what it feeds (`config/` for
config files). One file per case, named for the case (`invalid_unknown_key.conf`).
Tests read these files directly or copy them into a `tmp_path` layout. They never edit
them in place.

**The one shared, slow fixture.** Building the example project takes seconds, so
`built_project` runs one `dbtx build` per session and is **read-only**. A test that
modifies anything uses `project`, which gives it a private copy (warehouse included) in
its own `tmp_path`. This is how live tests stay independent while sharing one
build.

`manifest.json` is frozen from the example project. After changing
`examples/jaffle_shop`, refresh it with `python scripts/freeze_manifest.py`;
`test_frozen_manifest.py` fails while it is stale.

## Where a new test goes

1. **Is it end to end** (runs `dbtx` against a real project and warehouse)? Then it
   goes in `tests/live/test_<command>.py`, using `project`.
2. **Otherwise it is a unit test.** Find the source module it proves:
   - the module has a test file: add it there, under the matching behavior header
   - the behavior is a new functional group in a large module: start
     `test_<module>_<group>.py`
   - the module has no test file: create `test_<module>.py`
     (`test_<package>_<module>.py` for a subpackage)
3. **It needs state** (env, directories, config files, a fake dbt): use or extend the
   fixtures in the nearest `conftest.py`, and add sample data under `tests/fixtures/`.
   Do not add parameters to production code for it (see guidelines.md).

**The database store.** `dbtx/store/` has fixtures of its own (`store` and `engine`,
parametrized over SQLite and Postgres), so its unit tests have their own folder,
`tests/unit/store/` with its own `conftest.py`, instead of a flat `test_store_` file. A
subpackage gets a folder once it has fixtures no other test uses.

| Functional group | Test file |
|---|---|
| column types (`store/types.py`) | `test_store_types.py` |
| spec ↔ models and enums (`store/schema.yml`, `store/models/`, `store/enums.py`) | `test_store_schema_parity.py` |
| models on a real database: `create_all`, relationships | `test_store_models.py` |
| the test backends themselves: every backend starts empty; `sqlite-memory` is one database across connections; `sqlite-file` is in the test's own `tmp_path`; Postgres available without setup, UTF8, own schema, non-UTC zone | `test_store_backends.py` |
| the adapter registry, built-ins and `dbtx.store_adapters` plugins (`store/adapters/__init__.py`) | `test_store_adapter_registry.py` |
| adapter engines and `open_store`: connection settings, schema, FK and CHECK enforcement | `test_store_adapters.py` |
| `adapter.upsert` merge rules, `greatest` / `least` | `test_store_upsert.py` |
| `adapter.claim_next_job` | `test_store_job_claim.py` |
| several threads on one store: concurrent writers, concurrent claimers | `test_store_concurrency.py` |
| server version floors (`check_server`) | `test_store_server_check.py` |
| guard G2's registry (`store/repositories/projects.py`) | `test_store_projects.py` |
| the start-up checks through `cli.main` (`store/startup.py`): gates, connect, revision (SQLite auto-upgrade), G1 + G2 | `test_store_startup.py` |
| `dbtx db projects` and `rebind` (`store/cli.py`) | `test_store_db_projects.py` |
| migrations (`store/migrate.py`, `store/migrations/`): head, revision status, upgrade vs the models, downgrade | `test_store_migrations.py` |
| `dbtx db init` and `dbtx db upgrade` (`store/cli.py`) | `test_store_db_migrate.py` |
| `dbtx db status` and `StoreAdapter.database_file` (`store/cli.py`) | `test_store_db_status.py` |

Every test, fixture and helper function is **type-annotated**, parameters and return
value, so a reader can tell what `store`, `layout` or `rows` is without finding where
it comes from:

```python
def test_same_folder_bumps_last_seen_at(
    store: Store,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
```

* A test returns `-> None`. A fixture parameter has the fixture's type (a generator
  fixture is declared `Iterator[X]`; its parameter is `X`); a fixture that only sets
  something up is `None` (`enforce: None`). A `parametrize` parameter has the type of
  its values.
* Use pytest's public types for its own objects: `pytest.MonkeyPatch`,
  `pytest.CaptureFixture[str]`, `pytest.FixtureRequest`, `pytest.TempPathFactory`,
  `pytest.Config`; `tmp_path` is a `Path`.
* A signature longer than 100 characters puts one parameter per line.
* `uv run --with pyflakes pyflakes tests` catches a misspelled or unimported name in an
  annotation, which the tests alone cannot: `from __future__ import annotations` never
  evaluates them. Imports are ordered as
  `uv run --with isort isort --profile black -l 100 -p dbtx -p tests tests` leaves them.

Database connection strings are **URIs**, named `*_uri` (as in dbtx's `DB_URI`): every
backend fixture or helper provides one, and engines are built from it. Library names
stay as they are (`sa.make_url`, `engine.url`).

A test that uses `store`, `empty_store` or `engine` runs once per backend
(`ALL_BACKENDS`). A test for some backends only says so on its signature, with an
indirect parametrize over one of the `ON_*` lists in `tests/unit/store/conftest.py`:

```python
@pytest.mark.parametrize("store", ON_POSTGRES, indirect=True)
def test_postgres_tables_live_in_db_schema(store): ...
```

The other backends' cases are never generated, so they are neither run nor skipped.
**Don't skip inside a test for a backend it doesn't apply to.** A skip means "could
have run, but didn't" (pgserver not installed), and the store suite with Postgres
available reports none. `ON_POSTGRES` carries the `postgres` marker, so
`-m "not postgres"` reports those cases as deselected. Branching on
`store.test_backend` inside a test is still fine when a test runs everywhere but
expects different values per backend.
Use `store` for anything that goes through an adapter: it is configured exactly as dbtx
configures it. Use `engine` only for what must hold on a bare connection (column types
and models: a Postgres session that is not UTC, no SQLite pragmas). Tests with several
threads or connections run on `sqlite-file` and `postgres` only: the in-memory database
is one shared connection. A test that holds a transaction open releases it in a
`finally`, so a failure can never hang the run.
