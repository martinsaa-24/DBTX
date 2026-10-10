# Phase 1 (Foundations): implementation state

Tracks progress against [orm_implementation_plan.md](orm_implementation_plan.md),
sections 1-3 and "Phase 1". If a session is interrupted, resume from the first unchecked
item. Update this file after every step.

**Branch:** `feature/db-backend`, on top of `725da35` (Phase 0 commit). Nothing
committed for Phase 1 (the user has not asked for a commit).

**Style rules from the user:** simple, verbose code (repetition is fine where it avoids
abstraction); strict red -> green TDD per functional group, with the red and green
results recorded here (`docs/contributing/guidelines.md`).

## Steps

- [x] 1. Baseline: unit tests green before any change (`-m "not live"`: 318 passed, 27 deselected, on 2026-10-10)
- [x] 2. Add `alembic` dependency (`uv add`, resolved to **1.20.0**, pulls in `mako`); `postgres` marker in `pyproject.toml`
- [x] 3. Spec moved with `git mv` from `docs/agentic/orm_to_implement.yml` to `src/dbtx/store/schema.yml` (D3: "it moves into the package"; one copy, so nothing can drift). Plan links updated. Package data: hatch already ships every non-ignored file under `src/dbtx`, so no wheel config change; checked with `uv build --wheel` (wheel lists `dbtx/store/schema.yml`)
- [x] 4. Test fixtures: `tests/unit/store/conftest.py`
  - `spec` (the shipped `schema.yml`, freshly parsed) and `spec_tables()`
  - `engine`, parametrised `sqlite-memory` / `sqlite-file` / `postgres`: a new, empty database per test. Postgres gets a uniquely named schema per test (dropped after; the plan said per session, per test keeps tests independent) through `schema_translate_map`, and a session time zone of `Asia/Kolkata` so UTC normalisation is really exercised
  - `DBTX_TEST_POSTGRES_URL` is read at import, because the autouse `clean_env` removes every `DBTX_*` before each test
- [x] 5. Functional group "field types" (`store/types.py`) → `tests/unit/store/test_store_types.py` (14 tests, 32 cases over the backends)
  - **Red:** stubs (`UTCDateTime` passes values through, `JSONType = sa.JSON()`, `in_check` returns `""`): **23 failed**, all on assertions: naive datetimes returned (SQLite) or `ZoneInfo('Asia/Kolkata')` returned (Postgres), `DID NOT RAISE StatementError` for naive input, SQLite `ORDER BY` gave `[4, 5, 3, 1, 2]` instead of `[2, 5, 3, 1, 4]`, raw SQLite text `10:00:00` instead of `04:30:00`, JSON `None` stored as the literal `null` (3 backends), `JSON` instead of `JSONB` in Postgres DDL, `in_check` empty (3). 8 passed with the stub as expected (for example Postgres orders `TIMESTAMPTZ` natively; `None` timestamps were already NULL)
  - **Green:** 31 passed, 1 skipped (the SQLite storage-format test on Postgres)
- [x] 6 + 7. Functional groups "enums" (`store/enums.py`) and "models and metadata" (`store/models/*`) → `tests/unit/store/test_store_schema_parity.py` (98 cases, table-driven from the shipped spec: tables, class name + group module + re-export, columns in order, types, nullability, PK in order, FKs, no cascades, indexes, CHECK constraints == `in_check(spec values)`, enum values in spec order, app-enforced set, conventional names <= 63 chars, unique PK/index names)
  - **Red:** stubs (empty `str, Enum` classes, empty `NODE_RESULT_STATUSES`, `enum_values` returns `[]`, model modules with no classes, `Base` without a naming convention): **97 failed, 1 passed**, all on assertions (`no model for spec table 'job'`, `[] == ['pending', ...]`, `the metadata has no constraints`). Three "members are strings" tests first passed vacuously on empty enums; they now also assert the enum is non-empty. The 1 pass is the guard that every `enforced_by` column in the spec has an entry in the test's value-set table (test-side by design)
  - **Green (enums first):** the 14 enum tests passed, the 84 model tests still failed
  - **Green (models):** 98 passed. No `SAWarning` (`-W error::sqlalchemy.exc.SAWarning`)
  - **Mutation check** (spec edited, run, restored): a type change, index column order, a new enum value, nullability and PK column order each fail the suite
- [x] 8. Functional group "relationships" (read-only navigation) → `tests/unit/store/test_store_models.py`: configuration of the 10 relationships in plan 3.4 (target, `viewonly`, `lazy="raise"`, collection or not; no others exist), each loaded with `selectinload` on a two-project history, edges of another project sharing `unique_id`s never followed, unloaded access raises
  - **Red:** models without relationships: **62 failed**, all on assertions (`Node.parents is not a relationship`, the "no other relationships" list empty). 6 passed: the `create_all` tests (step 9), which already hold because the models exist
  - **Green:** 68 passed on the three backends, with `-W error::sqlalchemy.exc.SAWarning`
  - **Mutation check:** dropping `project` from the `Node.parents` join fails `test_node_edges_of_another_project_are_never_followed`
- [x] 9. `create_all` on SQLite (memory and file) and Postgres 16.2: every spec table created, and reflection (columns in order, nullability, PK, FKs, indexes, CHECK names) matches the spec. Part of `test_store_models.py`; it was green on its first run (see step 8)
- [x] 10. Full suite green, with Postgres: **542 passed, 1 skipped** (345 existing + 197 new; the skip is the SQLite storage-format test on Postgres), live tests included. Without `DBTX_TEST_POSTGRES_URL`: `-m "not live"` 488 passed, 28 skipped. Independence of the 198 store cases (with Postgres): reversed, shuffled (seeds 1-3), each file alone, and **each case alone** (198 separate runs): all pass; no `dbtx_test_*` schema left on the server. Python **3.10.22** (D10 floor, scratch env via `uv run --python 3.10`): 197 passed, 1 skipped
- [x] 11. Plan updated: Phase 1 "Status" block lists the deviations; `docs/contributing/tests.md` describes `tests/unit/store/` and the `postgres` marker

**Phase 1 complete.** Not committed: the user has not asked for a commit.
Files touched: `pyproject.toml` (alembic, `postgres` marker), `uv.lock`, `src/dbtx/store/` (new: `__init__.py`, `schema.yml` moved from `docs/agentic/orm_to_implement.yml`, `types.py`, `enums.py`, `models/{__init__,base,platform,events,dbt_nodes,dbtx_actions}.py`), `tests/unit/store/` (new: `__init__.py`, `conftest.py`, `test_store_types.py`, `test_store_schema_parity.py`, `test_store_models.py`), `docs/contributing/tests.md`, `docs/agentic/orm_implementation_plan.md`, `docs/agentic/phase1_state.md`.

## Deviations from the plan

- **Spec moved, not copied** (`git mv`), per D3 ("it moves into the package"). One copy, nothing to drift.
- **No hatch config change:** the wheel already includes `schema.yml`.
- **`validation.py` and the autouse `DBTX_DB_STRICT=1` deferred to Phase 4,** where loaders first write app-enforced values. Phase 1 ships the value set only.
- **Postgres test schema per test, not per session,** so every test starts from empty tables.
- **`Node.parents` / `Node.children`** return `Node` objects via `node_edge`, joined on `project` too; edges marked removed are included.

## Notes for Phase 2

- FK enforcement on SQLite (`PRAGMA foreign_keys=ON`) is not set yet: the Phase 1 `engine` fixture builds plain engines. The adapter adds it, and the Phase 2 `store` fixture should replace `engine`.
- The Postgres fixture's non-UTC session time zone should stay in the Phase 2 fixture as well (the adapter will set UTC on connect; the type must not rely on it).

## Addendum: throwaway Postgres as a test fixture (2026-10-10)

Requested after Phase 1: add pgserver to the dependencies and run the Postgres tests on
a throwaway pgserver instance shared by the test session.

- [x] Dependency: **`pixeltable-pgserver` 0.6.0** in the `dev` group (not runtime). The original `pgserver` (0.1.4, its latest) has wheels only for CPython 3.9-3.12 and no sdist, so it cannot install into the 3.14 dev venv. `pixeltable-pgserver` is its maintained fork, same API, wheels for 3.10-3.14 on Linux/macOS/Windows. Bundles **PostgreSQL 18.4**; initdb uses UTF8
- [x] Fixture `postgres_url` (session scope, `tests/unit/store/conftest.py`): `DBTX_TEST_POSTGRES_URL` when set; otherwise `get_server(<pytest tmp>/pgserver/pgdata, cleanup_mode="delete")`, stopped and deleted at session end. Requested lazily by the `engine` fixture's `postgres` case, so `-m "not postgres"` never starts it. Skips only if the package is not installed. Each test still gets its own schema
- [x] `postgres` marker description updated in `pyproject.toml`
- [x] Tests: `tests/unit/store/test_store_backends.py` (server available without configuration, UTF8, own schema + non-UTC session zone)
  - **Red:** stub `postgres_url` returning `DBTX_TEST_POSTGRES_URL` (unset): **2 failed** on assertions (`no Postgres server: postgres_url is empty`); every Postgres case skipped (29 skipped)
  - **Green:** store tests with no configuration: **200 passed, 1 skipped** (the SQLite storage-format test on Postgres). `-m "not postgres"`: 170 passed, no server started
  - The import of `pixeltable_pgserver` raised a `RuntimeDirWarning` (platformdirs, `XDG_RUNTIME_DIR` unset); the import now sits inside the fixture's warning filter
- [x] SQLite cases added to `test_store_backends.py` (the file first covered Postgres only; the module-wide `postgres` marker is now per test, so `-m "not postgres"` still runs the SQLite ones): every backend starts empty; `sqlite-memory` is one database across connections (`StaticPool`); `sqlite-file` lives in the test's own `tmp_path`. These describe fixture behaviour that already existed, so they were green on their first run (8 passed). **Mutation checks** instead of a red run: removing `StaticPool` fails the memory test (the other tests still pass, because SQLAlchemy's default pool for `:memory:` reuses one connection per thread, which is exactly the hidden dependency this test now pins); one database file shared by all tests fails both new SQLite tests and makes the model tests error on leftover rows
- [x] Override: with `DBTX_TEST_POSTGRES_URL` pointing at a separate PostgreSQL 16.2, 200 passed, no pgserver directory created, no `dbtx_test_*` schema left
- [x] Full suite with no configuration: **545 passed, 1 skipped**, live included; `-m "not live and not postgres"`: 488 passed. Store tests reversed, shuffled (seeds 1-3), each file alone, each new test alone: all pass. Python 3.10.22: 200 passed, 1 skipped. No postgres process or socket directory left after a run
- [x] Docs: `docs/contributing/tests.md`, `docs/contributing/guidelines.md` ("Network and services"), plan sections 7 and 8

- [x] **Env override removed, `URI` naming** (requested 2026-10-10): `DBTX_TEST_POSTGRES_URL` and the module-level `POSTGRES_URL` are gone; the tests always use their own pgserver. The fixture is `postgres_uri`, and every backend has a URI the engine is built from (`SQLITE_MEMORY_URI`, `sqlite_file_uri(tmp_path)`, `postgres_uri`), matching `DB_URI`. `config.KNOWN_ENV_VARS` no longer lists `DBTX_TEST_POSTGRES_URL`, so dbtx now warns about it like any unknown `DBTX_*` variable
  - No new test: dropping a name from `KNOWN_ENV_VARS` is a data change, and "an unknown `DBTX_*` variable warns" is already proven by `test_unknown_dbtx_env_vars_warn`. (A name-specific test was written and then deleted at the user's request: it only pinned one list entry.)
  - The "Override" item above describes the removed behaviour

- [x] **Server of your own, as a pytest option** (requested 2026-10-10, replacing the env variable): `pytest --postgres-uri URI` (registered with `pytest_addoption` in `tests/conftest.py`, so it is accepted whichever tests are selected; also settable through `PYTEST_ADDOPTS`). `postgres_uri` returns it when given (`is not None`) and starts no pgserver. `config.KNOWN_ENV_VARS` is unchanged: no env variable
  - Two subprocess tests (a working URI starts no pgserver; an unreachable URI makes the inner run fail) were written red -> green and then **deleted at the user's request**: they cost about 7 s and only guarded against someone deleting the option branch
  - Verified by hand instead, against the separate PostgreSQL 16.2: `--postgres-uri` and `PYTEST_ADDOPTS`: 207 passed, 1 skipped each, no pgserver directory created, no `dbtx_test_*` schema left; `--postgres-uri` with only non-store tests selected: accepted
- [x] **A blank `--postgres-uri` is an error**, not a silent fallback to pgserver (for example an unset CI variable in `PYTEST_ADDOPTS`): `check_postgres_uri_option()` in `tests/conftest.py`, called from `pytest_configure`, raises `pytest.UsageError` (pytest exits 4 before any test runs)
  - **Red:** stub that accepts everything: 2 failed (`DID NOT RAISE UsageError` for `""` and `"   "`); the 2 valid cases (no value, a URI) passed
  - **Green:** 4 passed; `pytest --postgres-uri ""` prints the message and exits 4. Full suite: **554 passed, 1 skipped**

**How Python 3.10 is checked:** locally only, with a scratch environment:
`UV_PROJECT_ENVIRONMENT=<scratch>/venv310 uv run --python 3.10 --extra postgres pytest tests/unit/store`
(uv-managed CPython 3.10.22, deps from `uv.lock`, the repo `.venv` untouched). There is no CI in the repo yet; the plan's 3.10 + 3.14 matrix (section 8) is still to do.

## Test environment

No Postgres server is installed on this machine. During Phase 1 a throwaway PostgreSQL
16.2 (the `pgserver` wheel, in the session scratchpad) was used through
`DBTX_TEST_POSTGRES_URL`. Since the addendum above, the suite starts its own server, so
no setup is needed.
