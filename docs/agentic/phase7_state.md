# Phase 7 (Docs history from the database): implementation state

Tracks progress against [orm_implementation_plan.md](orm_implementation_plan.md),
"Phase 7". If a session is interrupted, resume from the first unchecked item. Update
this file after every step.

**Branch:** `feature/db-backend`, on top of `f8045f5` (CI scripts after Phase 6).
Untracked before this phase: `examples/jaffle_shop.duckdb` (left alone).

**Style rules from the user:** simple, verbose code; strict red -> green TDD per
functional group, red and green results recorded here; state from test fixtures, not
extra parameters; `*_uri` names; type-annotated tests; `ON_*` lists, never skips; files
changed only with the Edit / Write tools.

## Decisions agreed with the user (2026-10-14)

- `dbtx docs serve` serves `dbtx_runtime.json` from the database, with a cache, in
  platform mode with the backend enabled; the file otherwise
- the overlay shows "Last ran" and "Updated" (when that version of the node first
  appeared: `node_version.first_seen_at`); "Last compiled" is dropped
- the sidecar is still written after every run
- history only in a sidecar is not shown by a database-backed server (accepted)

## Steps

- [x] 1. Baseline (2026-10-14): full suite **1567 passed**
- [x] 2. The document from the database → `tests/unit/store/test_store_docs_history.py`
  - `src/dbtx/docs/store_history.py`: `project_of(docs_loc)` (docs manifest
    `metadata.project_name`, else HistoryError), `build_document(store, docs_loc,
    project)`: one query, `row_number()` over each node's results ordered by the
    invocation's `start_time` (then id), joined to `node_version` on the run's checksum
    for `updated_at`; only `runs.COMMANDS_THAT_EXECUTE_NODES`; the rows go into a
    `DocsCatalog` parsed from the docs manifest, so staleness, the node set and reports
    are the sidecar's own code
  - `sidecar.document(catalog)` split out of `sidecar.write` (the shape both send);
    `catalog.NodeResult.updated_at` (also written to the sidecar file, None there)
  - Red: 23 tests × 3 backends, **69 failing** (the stub). Green: **69 passed**; sidecar
    tests unchanged (37 passed)
- [x] 3. The cache (probe interval, token, rebuild, database errors) → same file
  - `HistoryCache(store, docs_loc)`: the project read once (`project_of`); `document()`
    under one lock: within `PROBE_INTERVAL_SECONDS` (5) of the last check → the kept
    bytes; else the version (the project's invocation count and newest `end_time`; the
    docs manifest's, sidecar file's and reports files' modification times) and a
    rebuild only when it moved. SQLAlchemyError: kept bytes + one stderr warning until
    the database answers again (checked again only after the interval); nothing kept →
    `HistoryUnavailable`. Clock: `store_history.monotonic()`, replaced by a fixture
  - Tests drop the real tables for database failures (no mocks); a spy counts builds
  - **Added beyond the plan:** the sidecar file (history limit) and the reports folder
    are part of the version, so a new limit or a re-rendered report shows up
  - Red: 13 tests × 3 backends, **39 failing**. Green: file **108 passed**
- [x] 4. The route in `create_app` → `tests/unit/test_docs_server.py`
  - `create_app(docs_loc, history=None)` / `build_server(..., history=None)`: with a
    history, a `/dbtx_runtime.json` route ahead of the static mount (a plain `def`
    endpoint: Starlette's thread pool); `HistoryUnavailable` → 503 `{"error": ...}`.
    `no-store` comes from the existing CachePolicy middleware (the file name)
  - Red: 5 tests; first run failed on the missing parameter, so the parameter was
    stubbed and red run again: **4 failing** ("other files still served" passed: a
    gate). Green: **46 passed**
- [x] 5. `docs serve` uses the database; the CLI hands the cache to the server → `test_config.py`, `test_docs_server_cli.py`, `test_docs_site.py`
  - `config.COMMANDS_THAT_USE_THE_DATABASE` gains `docs serve`: in platform mode with
    the backend enabled it needs `DB_URI` and passes the start-up checks (connect,
    revision; G1/G2 when a project is named) before the port is bound
  - `cli.run_command` hands the config to `docs.command.run(args, config)`; `_serve`
    in platform mode with the backend enabled opens the store, makes a `HistoryCache`
    (HistoryError → exit 2) and calls `server.serve(..., history=...)`; the store is
    disposed when serving ends. Otherwise `history=None` (the file)
  - Tests: platform (with and without `--docs-loc`) → a HistoryCache for `jaffle_shop`;
    `--db-backend disabled` and standalone → None; an uninitialised database → exit 2
    (`dbtx db init`), nothing served; a docs manifest without `project_name` → exit 2.
    `test_serve_defaults` expects `history: None`; `docs serve` left the "does not
    use the database" list. Updated for the new behaviour: the `--docs-loc` default
    tests in `test_docs_site.py` run on an initialised platform, and their fake
    `docs_command` accepts the config
  - Red: **8 failing**. Green: unit suite **1634 passed**
- [x] 6. Overlay wording ("Last ran", "Updated")
  - `dbtx_docs.js` `renderLatest`: "Last ran", then "Updated" (`run.updated_at`) only
    when the run has one (a sidecar file has none); "Last compiled" removed. The
    sidecar keeps writing `last_compiled_at` (no format change)
  - No JS tests exist (none before either); checked in the served page (step 7), not
    in a browser
- [x] 7. Live test on the platform fixture; the real app on a scratch copy of `examples/`
  - New `test_the_docs_server_serves_the_run_history_from_the_database`: docs generate
    → install → build, then the objects `dbtx docs serve` makes (`load_config`,
    `open_store`, `HistoryCache`, `create_app`): the served runs equal the sidecar's
    for every field both have (`last_ran_at` compared as times), `updated_at` set, the
    report attached; a second build is served after the probe interval
  - **Found and fixed (bug, reproduced first):** dbt gives generic tests an empty
    checksum; the sidecar records the node's name instead (`DocsCatalog._node_checksum`),
    the database the empty string, so every generic test was served as **stale**. Red:
    `test_a_generic_test_with_an_empty_checksum_is_not_stale` (3 failing: `('', True,
    ...)`). Fix: `build_document` maps `""` through the catalog's fallback. Their
    `updated_at` is when the test first appeared (the loader stores `""` as a version)
  - Live suite **54 passed**
  - Real app (scratch copy of `examples/`): `db init` → `docs generate` → `docs
    install` → `build` → `build -s customers` → `dbtx docs serve --port 8765`:
    `dbtx_runtime.json` 200, `application/json`, `no-store`; 49 nodes with runs (the
    file: 49), nothing stale, 6 reports; a third build while serving → 3 runs after
    the probe interval; the served `dbtx_docs.js` has "Updated", no "Last compiled"
- [x] 8. Full suite, ruff, Python 3.10
  - Full suite **1691 passed** on Python 3.14 and on 3.10; ruff 0.17.0 (CI's pin) clean
    over `src`, `tests`, `scripts`
- [x] 9. Docs: README, CHANGELOG, flows, plan status
  - README: the Run Results panel ("Updated"), a "Run history from the database
    (platform mode)" section under Docs Server, the cache-policy row, `docs serve` among
    the database commands, the example sequence's serving note
  - CHANGELOG: breaking (`docs serve` needs the database in platform mode; "Last
    compiled" gone) and added; `docs/flows/` (commands, docs_refresh, usage);
    `docs/contributing/tests.md` (the new test file and why it is under `store/`); plan
    Phase 7 status and deviations
  - Not changed (noticed): the CHANGELOG's Phase 6 "Added" entry still says "SQLite
    stores migrate automatically", which step 11 of Phase 6 made untrue

**Phase 7 complete (2026-10-17).** Not committed.

- [x] 10. CI redesigned (user, 2026-10-17; not part of the plan): mount → clear → copy →
      lint / test / package the copy → keep artifacts → empty on success, left on failure
  - `ci/setup.sh`: `mount_workspace` (a `CICD_TMPFS_SIZE` tmpfs at `CICD_ROOT` unless it
    is a mount point; `sudo` unless root; moved here from the workflow),
    `clean_workspace`, `copy_repository` (`git ls-files --cached --others
    --exclude-standard`, files that exist, via tar: the working tree as it is, ignored
    files left out), `install_dependencies`. Gone: the clone and checkout, `CICD_REF`,
    `CICD_REPO` (now always the scripts' repository), `CICD_WORKING_TREE`
  - `clear_workspace` (the refusals) moved to `ci/lib.sh`, shared by setup and run
  - `ci/run.sh`: setup, build, then `keep_artifacts` (`out/` → `.build/`, pass or fail;
    only from a marked workspace), then on success `clear_workspace`, on failure exit 1
    with the workspace left
  - Parallel logs moved from `.build/` to `out/logs/` (kept in `.build/logs/`)
  - `ci.yml`: no mount step, no `CICD_REF`; uploads `.build`
  - Checked by hand (no tests, per the user): full `ci/run.sh` passed (211 files copied,
    unit 1637, live 54, package), `/mnt/cicd` empty after, `.build/` with source.txt,
    unit.xml, live.xml, logs/, dist/; `CICD_PYTHON=3.99` → setup fails, workspace left,
    `.build/source.txt` kept, exit 1; a non-mounted `CICD_ROOT` without sudo → "cannot
    mount a tmpfs ... (needs root or sudo; or mount one yourself)"; a relative
    `CICD_ROOT` refused. The mount itself was not exercised (sudo needs a password here;
    GitHub's runners have passwordless sudo). Not run on GitHub
  - Docs: `docs/contributing/ci.md`, README "Tests", `.gitignore` comment

## Decisions made during implementation

- **No ETag** on the served document: it is `no-store` and the overlay cache-busts, so
  a browser never revalidates.
- **The project is read once**, from the docs manifest, when the server starts.
- **The cache's version includes the sidecar file and the reports folder**, beyond the
  plan's database and docs manifest.
- **Generic tests' empty checksum** is mapped to the node's name (the catalog's rule).

## Test environment

- `uv run --extra postgres pytest ...` (psycopg is in the `postgres` extra).
- Python 3.10: `UV_PROJECT_ENVIRONMENT=<scratch>/venv310 uv run --python 3.10 --extra postgres pytest tests`.
