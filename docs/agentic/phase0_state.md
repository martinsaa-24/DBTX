# Phase 0 (Configuration): implementation state

Tracks progress against [orm_implementation_plan.md](orm_implementation_plan.md),
section 6 and "Phase 0". If a session is interrupted, resume from the first unchecked
item. Update this file after every step.

**Branch:** `feature/db-backend`. Nothing committed for Phase 0 (the user has not asked
for a commit). `docs/` is still untracked.

**Style rule from the user:** simple, verbose code. Repetition is fine where it avoids
abstraction, as long as it does not risk regressions.

## Steps

- [x] 1. Baseline: unit tests green before any change (162 passed on 2026-10-10)
- [x] 2. Add `sqlalchemy` dependency (`uv add`), used for `make_url` only. Resolved to SQLAlchemy **2.1.4**. Also added the `postgres` extra (`psycopg[binary]>=3.1,<4`, resolved to 3.3.6) so the "install dbtx[postgres]" message points at something real.
- [x] 3. `src/dbtx/config.py`
  - [x] argv extraction (`--db-backend`, `--db-uri`, `--config-file`, `--platform-dir`, `--standalone-mode`)
  - [x] mode detection (rules 0/1/2, "no platform" error, contradictions)
  - [x] project dir resolution and argv injection (`--project-dir`, `--profiles-dir`)
  - [x] profiles-dir conflict check
  - [x] config file parser (strict `KEY=VALUE`)
  - [x] env layer (+ unknown `DBTX_*` warnings, standalone ignores)
  - [x] layered resolution with sources
  - [x] value validation (backend, URI, driver)
  - [x] command classes + requirement check
  - [x] SQLite relative path normalisation
  - [x] G1 filesystem scan
  - [x] `ENFORCE_DB_REQUIREMENTS` rollout constant
  - [x] frozen `DbtxConfig`
- [x] 4. `cli.main`: load config first, print warnings, dispatch with rewritten argv (existing 162 unit tests still pass)
- [x] 5. `dbtx config` command; `dbtx db` stub (standalone gate only)
- [x] 6. `build` / `compile_project` accept `config`
- [x] 7. Tests: `tests/unit/test_config.py` (101), `tests/unit/test_cli_config.py` (12), all passing; mutation check (disabling the profiles check, G1, or argv injection) makes the suite fail
- [x] 8. Full suite green: **302 passed** (189 existing + 113 new), integration and server tests included. End-to-end check: a platform root in the scratchpad with a copy of `examples/jaffle_shop`; `dbtx config`, `dbtx build --project-dir jaffle-shop` from the root (6 reports rendered, the project's own `profiles.yml` used), `DBT_PROFILES_DIR` conflict → exit 2, `--standalone-mode` from inside the project
- [x] 9. README "Configuration" section (under Usage), `examples/dbtx.conf.example` (parses with the real parser), `postgres` extra in Requirements
- [x] 10. Plan updated: Phase 0 "Status" block lists the deviations

**Phase 0 complete.** Not committed: the user has not asked for a commit.
Files touched: `pyproject.toml`, `uv.lock`, `src/dbtx/config.py` (new), `src/dbtx/cli.py`, `src/dbtx/runner.py`, `tests/unit/test_config.py` (new), `tests/unit/test_cli_config.py` (new), `README.md`, `examples/dbtx.conf.example` (new), `docs/agentic/*`.

## Decisions made during implementation

- SQLAlchemy 2.1 defaults a bare `postgresql://` URL to the psycopg 3 driver (2.0 defaulted to psycopg2). Validation therefore checks `url.get_driver_name()`: `sqlite` needs `pysqlite`, `postgresql` needs `psycopg`; any other driver is rejected.

## Deviations from the plan

- **Config errors and warnings go to stderr** (the plan did not say). Printing them on stdout would corrupt output other tools parse, e.g. `dbtx ls --output json`.
- **Commands without a project** (`config`, `init`, `docs serve/patch/install/status`, `db init/upgrade/status`) need no project dir in platform mode, so they work from the platform root. The plan's "must be a direct child" rule applies to commands that run against one project.
- **Platform mode rejects a command line that starts with an option** (`dbtx --debug build`): without knowing the command, dbtx cannot scope the project and profiles dir. Standalone mode passes it through as before.
- **Only `psycopg` (3) and `pysqlite` drivers are accepted** in `DB_URI` (see decisions).
- `--db-schema` is not extracted yet: it belongs to Phase 2 with the `DB_SCHEMA` option.
- `dbtx db` is a stub: exit 2 with "requires platform mode" in standalone, "not available yet" in platform mode.

## Test rework: test fixtures and independent tests (2026-10-10)

Requested after Phase 0: state comes from **test fixtures**, not extra parameters on
production code; tests are independent; parser tests read checked-in sample files;
layout-dependent tests build a clean tmp directory structure per test.

- [x] Guidelines updated (`docs/contributing/guidelines.md`: "Test fixtures",
      "Independent tests"; the old "pass now/ids as parameters" line replaced)
- [x] Sample config files: `tests/fixtures/config/*.conf` (13 files, one case each) and `decoy.env` (proves `.env` is never read)
- [x] Shared fixtures: `tests/unit/conftest.py` (`clean_env` autouse, `enforce`,
      `config_sample()`, `LayoutBuilder` / `layout`, `platform`, `lone_project`, `fake_dbt`)
- [x] Functional group "config file parser" → `tests/unit/test_config_file.py`
  - **Red:** 14 tests run against a stub `parse_config_file = lambda path: {}`:
    14 failed, all on assertions (7 `DID NOT RAISE ConfigError`, 7 value mismatches)
  - **Green:** against the real parser: 14 passed
- [x] `tests/unit/test_config.py` rewritten on the shared fixtures (no inline config text; parser cases moved to `test_config_file.py`)
  - **Red:** each functional group's function replaced in turn by a do-nothing stub; every stub fails its group's tests:
    `decide_mode` 76, `resolve_platform_project_dir` 5, `check_profiles_dir` 2, `inject_project_and_profiles_dir` 4,
    `locate_config_file` 6, `resolve_db_uri` 20, `validate_db_uri` 6, `normalise_sqlite_path` 2,
    `check_project_name_collisions` 5. Mostly assertion failures; the rest are downstream consequences of the stub
    (for example `AttributeError` reading DB settings when the mode stub returns standalone), never import errors
  - **Green:** 124 passed
- [x] `tests/unit/test_cli_config.py` rewritten on the shared fixtures
  - **Red:** stub `cli.load_config` (accepts everything) → 10 failed; stub `show_config` → 2; stub `db_command` → 2
  - **Green:** 12 passed
- [x] `tmp_path_retention_policy = "failed"` in pyproject (checked: a green run's base temp dir is deleted at session end)
- [x] Independence check on the 150 new tests: reversed, shuffled (seeds 1-3), each file alone, and **each test alone** (150 separate runs): all pass
- [x] Full suite green: **339 passed** (189 existing + 150 new), integration and server tests included

**Test rework complete.** Not committed.
