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
- `postgres`: needs a real Postgres database (from the database phases on). These are
  unit tests by structure and skip unless `DBTX_TEST_POSTGRES_URL` is set.

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
| `tests/conftest.py` | `manifest` (a deep copy of the frozen manifest per test, safe to mutate), `templates`, `reports` |
| `tests/unit/conftest.py` | `clean_env` (autouse: clears `DBTX_*`, `DBT_PROJECT_DIR`, `DBT_PROFILES_DIR`), `enforce`, `config_sample()`, `layout` (a `LayoutBuilder` over `tmp_path`), `platform`, `lone_project`, `fake_dbt` |
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

**Planned: the database store.** `dbtx/store/` will need fixtures of its own (a backend
fixture parametrized over SQLite and Postgres, plus engines and sessions). Its unit tests
therefore get their own folder, `tests/unit/store/` with its own `conftest.py`, instead of
a `test_store_` prefix. A subpackage gets a folder once it has fixtures no other test
uses.
