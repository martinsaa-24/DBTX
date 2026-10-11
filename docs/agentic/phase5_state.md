# Phase 5 (Runner integration): implementation state

Tracks progress against [orm_implementation_plan.md](orm_implementation_plan.md),
sections D5, 5.2–5.5, 5.7 (the recorder), 6.5.1 / 6.6 (rollout flag) and "Phase 5". If a
session is interrupted, resume from the first unchecked item. Update this file after
every step.

**Phase 5 complete (2026-10-11).** Not committed.

**Branch:** `feature/db-backend`, on top of `3eea72e` (the Phase 4 commit). Already in the
working tree when Phase 5 started, **not part of this phase** and left as found: changes
to `src/dbtx/config.py` (a `--project-dir` that does not exist / is a file),
`src/dbtx/runner.py` (`exit_code` prints dbt's usage errors), `tests/unit/test_config.py`,
the new `tests/unit/test_cli_passthrough.py`, and an untracked
`examples/jaffle_shop.duckdb` (a warehouse written by a run from `examples/`).

**Style rules from the user:** simple, verbose code (repetition is fine where it avoids
abstraction); strict red -> green TDD per functional group, with the red and green
results recorded here (`docs/contributing/guidelines.md`); state comes from test
fixtures, not extra parameters; database connection strings are URIs (`*_uri`); every
test, fixture and helper is type-annotated; a test for some backends only uses an
`ON_*` list, never a skip (`docs/contributing/tests.md`).

## Spikes (dbt 1.12.5, scratch copies of the example project; not production code)

- **Event order** (`parse`, `run-operation`, `build`, `source freshness`, `docs generate`,
  `compile`): the first event a callback sees is always `MainReportVersion`, carrying
  the new `invocation_id`; `get_invocation_started_at()` is set by then (naive UTC).
  `CommandCompleted` is always last. `ConcurrencyLine.target_name` fires for build,
  compile and docs generate; **not** for parse, run-operation, nor for `source
  freshness` on a project without freshness config. So the registered-adapter fallback
  (plan 5.2) applies to every command, not only parse / run-operation
- **Registered adapter:** dbt's `adapter_management()` resets the adapter factory on
  every invoke (right after `MainReportVersion`), so reading it at `CommandCompleted`
  only ever sees this invocation's adapter. Broken YAML: adapter registered
  (`duckdb`, `dev`); no profile: none registered (→ `'unknown'`, plan 5.5)
- **args:** `args_to_dict(get_flags())` at `MainReportVersion` differs from the
  artifact's `args` (`deprecated_env_var_warnings` is filled in later); captured at
  `CommandCompleted` it is **equal** to `RunResultsArtifact...to_dict()["args"]`
- **Serialisers:** `RunResultsArtifact.from_execution_results(...)` and
  `FreshnessExecutionResultArtifact.from_result(...)` both serialise real results.
  `from_result` drops a source whose query failed (not a `SourceFreshnessResult`);
  dbt's own `sources.json` drops it too, so the in-process path matches the file
- Adapter config: `config.project_target_path` is the absolute target dir

## Design

- `src/dbtx/store/recorder.py`:
  - `Recorder.for_invocation(config)` → `NullRecorder` unless platform mode, backend
    enabled, a recorded command (argv table) and a readable project name; then a
    `Recorder` (the store is opened, not connected)
  - `on_event` (dbt callback, any thread): `MainReportVersion` → start (one
    transaction: the invocation row; with a manifest also the manifest sync, the
    report sync and the pending render rows); `ConcurrencyLine` → target;
    `CommandCompleted` → the registered adapter (type, target, target path) and the
    flags as `args`
  - `report_changed(exposure_id, outcome)` (the scheduler observer): one short
    transaction per transition
  - `finish(result)`: one transaction: (pass-through only) the manifest sync, the
    node results, the resolved target / adapter type / args, the invocation's end
  - best-effort (D5): any error is logged through `fire_event` at WARN, once, and the
    recorder stops writing; in strict mode an `UnknownValueError` is raised (D9)
- `ReportScheduler(on_change=...)`: called after every outcome transition, outside the
  lock
- wiring: `runner.build`, `runner.compile_project`, `prepare()`'s failed-parse fallback,
  and the pass-through branch of `cli.main`
- `ENFORCE_DB_REQUIREMENTS` deleted (behaviour as if `True`); live tests' `Project.dbtx`
  passes `--standalone-mode`

## Steps

- [x] 1. Baseline (2026-10-11): full suite **1300 passed**, 0 skipped (2 warnings), 85 s
- [x] 2. argv → command table, one test per row → `tests/unit/store/test_store_values.py`
  - `enums.COMMAND_BY_CLI` + `command_from_cli(command)`: keyed by dbtx's command name
    (`config.command`, which already joins `source` / `docs` with their subcommand)
  - `config.COMMANDS_THAT_USE_THE_DATABASE` stays an explicit tuple (importing
    `dbtx.store.enums` would load the whole store package, SQLAlchemy included, on
    every config load); `test_every_recorded_command_uses_the_database` guards that
    every recorded command is in it
  - Red: 30 new tests, **12 failing** (the 11 rows and the link test, on assertions);
    the 19 "not recorded" cases pass against the stub's None. Green: **58 passed**
- [x] 3. `ReportScheduler(on_change=...)`: fires for every transition, outside the lock → `tests/unit/test_scheduler.py`
  - `on_change(exposure_id, outcome)`: transitions are collected while the lock is held
    and reported after it is released, on the thread that caused them (a render's
    `running` and `rendered` / `error` on the dbt node thread; `skipped` at `finish`)
  - Red: 10 tests, **9 failing** (the stub never calls the observer); "without an
    observer nothing changes" passes against the stub. Green: **21 passed**
- [x] 4. Recorder: gates (`NullRecorder`), lifecycle, target / adapter fallback, failed
      parse, render rows, manifest pairing and `manifest_hash`, best-effort errors,
      strict mode → `tests/unit/store/test_store_recorder.py`
  - `src/dbtx/store/recorder.py` (`Recorder`, `NullRecorder`, `RegisteredAdapter`); dbt's
    state is read through `invocation_started_at`, `flags_args`, `registered_adapter`,
    `result_artifact`, `current_user` (the test fixture `dbt` replaces them; the real
    ones have their own tests)
  - new repository function `invocations.set_resolved(adapter_type, target, args)`:
    the start row carries `'unknown'` placeholders (NOT NULL columns), set at the end
  - **Decisions:**
    - `args` = the flags at `CommandCompleted` (equal to the artifact's, spike), for
      every command; the start's flags until then
    - `manifest_hash`: first a timestamp check (file not older than the start);
      replaced in step 10 by dbt's own `ArtifactWritten` event
    - pass-through commands (no manifest from dbtx): the manifest is dbt's parse result,
      or `<target>/manifest.json` whose `metadata.invocation_id` is this invocation's;
      otherwise nothing is synced. Reports are synced only by `build` / `compile`
      (they need dbtx's templates)
    - `adapter_type`: the manifest, else the registered adapter, else `'unknown'`
    - one warning per recorder (`dbtx: run history not recorded (invocation <id>):
      <first line>`), then no more writes; strict mode re-raises `UnknownValueError`
  - Red: 70 tests, **49 failed + 10 errors** (the `rendering` fixture's assertion on the
    stub's NullRecorder), all assertions or the stub readers' NotImplementedError; the 11
    "records nothing" gates pass against the stub
  - Going green, fixed in the tests: the `dbt` fixture name shadowed the `dbt` module;
    the fake `fire_event` printed `EventLevel.WARN`; the failed-parse test's "stale"
    manifest was newer than the start (now backdated, as a real stale file is); dbt's
    serialiser needs `vars` and `invocation_command` in `args`. Fixed in the code: the
    host is read through `projects.current_host` (the store suite's `host` fixture)
  - Green: **70 passed**
- [x] 5. Wiring in `runner.build` / `compile_project` / `prepare` fallback / pass-through → `tests/unit/test_runner_recording.py`
  - Each recorder is made right before the invoke it records and finished right after:
    the final `build` / `compile` (after `use_manifest`), `runner.invoke_recorded(config,
    argv)` for invokes without a dbtx manifest (`build --no-reports`, `compile
    --no-reports` / `--inline`, `prepare()`'s failed-parse re-run), and the pass-through
    branch of `cli.main` (inline, so the existing `fake_dbt` fixture still replaces it).
    dbtx's internal `parse` / `ls` get no recorder
  - `prepare(command, args, params, config=None)`; the scheduler's
    `on_change=recorder.report_changed`; `recorder.finish` after `scheduler.finish()`
  - Red: 10 tests, **10 failing** (no recorder callback on any invoke), after fixing the
    tests' argv (dbt's click checks that the default `--profiles-dir`, `~/.dbt`, exists;
    the tests now pass `--profiles-dir` as platform mode does). Green: **10 passed**
  - Unit suite: one failure, `test_no_store_is_opened_until_enforced` (the SQLite
    adapter creates `.dbtx/` when the recorder builds its engine): it tests the
    pre-Phase-5 rollout, removed in step 6
- [x] 6. Turn on the requirements: delete `ENFORCE_DB_REQUIREMENTS`; live tests run with
      `--standalone-mode`
  - Red: the `enforce` fixture removed from every test that used it (they described the
    enforced behaviour): **30 failing** (no platform, missing DB_URI, G1, every start-up
    check through `cli.main`)
  - `config.ENFORCE_DB_REQUIREMENTS` deleted with its three warn-only branches (no
    platform → exit 2; missing DB_URI → exit 2; a G1 collision with the current project
    is decided by the start-up checks, no "not enforced yet" warning); `startup.py` no
    longer waits for it; `decide_mode` lost its now unused `warnings` parameter
  - Deleted, as they test the removed rollout: `test_no_platform_falls_back_to_standalone_until_enforced`,
    `test_missing_uri_only_warns_until_enforced`, `test_g1_only_warns_until_enforced`,
    `test_no_store_is_opened_until_enforced`, `test_fallback_to_standalone_says_db_schema_is_ignored`,
    `test_fallback_to_standalone_says_db_adapter_is_ignored`; `test_no_platform_is_an_error_once_enforced`
    → `test_no_platform_is_an_error`, `test_uses_the_database_without_enforcement` →
    `test_uses_the_database`
  - Tests that relied on the fallback without saying so, setup fixed: precedence /
    `DB_SCHEMA` / `resolved_adapter` tests use `dbtx config` (reports instead of
    requiring DB_URI); `test_warnings_go_to_stderr` uses an unknown `DBTX_*` variable;
    `db load`'s unknown-arguments test gets a SQLite URI
  - **Found:** bare `dbtx docs` (prints dbtx's docs help) failed with "no platform
    found" while `dbtx docs --help` worked. `is_help_request` now counts `["docs"]`.
    Red: `test_docs_help_is_ours_not_dbt_s[argv0]` and the new
    `test_help_works_without_a_platform[argv4]`; green
  - Unit suite **1388 passed**
  - Live: `Project.dbtx` and the session build pass `--standalone-mode`; live suite
    **28 passed**
- [x] 7. Live tests on a platform fixture (plan "Phase 5" tests, clean-environment tests) → `tests/live/test_store_recording.py`
  - Fixture `platform` (in the test file): the built example copied to
    `<tmp>/platform/jaffle-shop` (warehouse and `target/` included), a root `dbtx.conf`
    with `DB_URI=sqlite:///.dbtx/dbtx.db`; dbtx runs from the project folder with
    `--platform-dir` (so the profile's relative DuckDB path is the built warehouse)
  - 12 tests: a build (invocation id = run_results' and ≠ manifest's; target `dev`
    without `--target`; `manifest_hash` = the file after the build; args = run_results'
    args; a node result per run result with the same status; 6 reports and 6 rendered
    renders with existing paths; the claim); a failing test → `failure`, two renders
    `skipped` with the reason; the console output with and without the store
    (normalised: times, completion-order numbering, padding; sorted, because dbt's
    threads finish in any order); a store error → exit 0, reports rendered, one warning,
    no rows; `--no-partial-parse` twice → the same version keys, no removals; no
    `target/` → every stored node, nothing removed; broken YAML → a `failure` with the
    error, target and adapter type from the registered adapter, no node or report row
    changed; `--no-write-json` → rows, `manifest_hash` NULL; pass-through `run`
    (manifest synced from its own manifest.json, no reports); `compile` (reports, no
    renders); `parse` (target from the adapter); `ls` → no database created
  - Written against the implemented recorder (the plan's live tests); first run: 4
    failures, all in the tests' expectations: dbt numbers lines in completion order and
    pads to the number's width; a re-parse bumps `last_seen_at` (correct; the plan's
    claim is "no new rows"); `build -s stg_customers` also runs its 2 tests; the
    `top_customers` selection includes the test that warns. The plan's "result=success"
    for a full build is `warning` for the example (one test warns), and it has six
    reports, not three
  - **Found (pre-existing test isolation bug):** dbt-duckdb keeps one DuckDB
    environment per process (`DuckDBConnectionManager._ENV`) and reuses it while the
    credentials compare equal; the example's path is relative, so every in-process live
    test shared the first test's warehouse, and a write failed (`.wal` not found) once
    pytest had deleted that test's directory. Reproduced by running `test_build.py`
    then this file. Fix: autouse `fresh_duckdb_environment` in `tests/live/conftest.py`
    closes the environment after each test
  - Live suite **40 passed**; full suite **1428 passed**; console test stable over 4 runs
- [x] 8. Full suite, Python 3.10, pyflakes / isort, annotations
  - pyflakes clean (`src/dbtx`, `tests`, `scripts`; removed `tests/unit/conftest.py`'s
    `config` import, unused once `enforce` went). isort (`--profile black -l 120`)
    applied to the new files only: `runner.py`, `config.py` and `test_store_db_load.py`
    are flagged on import lines this phase did not write, left as they were. Every new
    test, fixture and helper annotated (the one gap, `scheduler.on_event`'s `event`,
    predates this phase)
  - Full suite **1428 passed** on Python 3.14 and on 3.10 (SQLAlchemy 2.0.54)
  - The real app, on a scratch copy of `examples/`: `cd jaffle_shop && dbtx build
    --platform-dir ..` → exit 0, 6 reports rendered, the SQLite store created
    (warning on stderr); `dbtx db status` → 1 invocation, 48 node results, 6 renders
- [x] 9. Docs: README "Run history store", changelog, `docs/contributing/tests.md`, plan
      Phase 5 status, this file
  - README: the "Run History" feature bullet; the configuration bullets (requirements now
    enforced; G1 refuses); an "Upgrading" note pointing at `--standalone-mode` and
    `--db-backend disabled`; a "Run history store" section (what is recorded, which
    commands, start-up checks, best-effort, failed parse, `--no-write-json`, the
    clean-target caveat, secrets in args, Q5); the dev quick start and the examples
    platform now run from the project folder with `--platform-dir ..` (running from
    `examples/` put the DuckDB warehouse in `examples/`); `examples/dbtx.conf` comment
    likewise
  - New `CHANGELOG.md` (the repository had none): the breaking change and what was added
  - `docs/contributing/tests.md`: new test files, `fresh_duckdb_environment`, the
    standalone live tests and the platform fixture, `enforce` removed
  - Plan: Phase 5 status block with the deviations

- [x] 10. `manifest_hash` from dbt's `ArtifactWritten` event instead of a timestamp check (agreed with the user after review)
  - Why: the timestamp check (file not older than the start) depended on filesystem
    timestamps (coarse resolution, network clocks) and would hash a stale file that
    merely looked recent. dbt announces every manifest it writes:
    `Manifest.write()` fires `ArtifactWritten(artifact_type="WritableManifest",
    artifact_path=...)` (`dbt/contracts/graph/manifest.py:1365`)
  - Spike (scratch copy): the event reaches a `dbtRunner` callback for build (own parse:
    twice; handed a manifest: once, at the end), run-operation, parse and docs
    generate, under `--quiet` and `--log-level none`; `--no-write-json` fires none.
    dbtx's internal parse writes in its own invocation, which has no recorder
  - The user's alternative (each run writes to a temporary target dir, copied back when
    valid) was weighed and not taken: it loses partial parsing, moves dbtx's report
    files, and failed runs must be copied back anyway (`dbt retry` reads them)
  - Red: 3 timestamp tests replaced by 5 (hashed: the announced file; and with no
    registered adapter; NULL: no announcement with a fresh-looking stale file, only
    another artifact announced, the announced file gone) and the failed-parse test no
    longer backdates its stale file: **8 failing**, all on the hash ("file gone" passed)
  - `Recorder.written_manifest` (set on the event); `manifest_hash()` hashes only it
  - Green: recorder tests **74 passed**; full suite **1432 passed** (live included)

## Decisions made during implementation

- **One recorder per recorded invoke**, made right before it and finished right after;
  dbtx's internal parse / ls are never recorded.
- **`args`** from dbt's flags at `CommandCompleted` (equal to the artifact's).
- **`manifest_hash`** of the manifest dbt announced writing in this invocation (`ArtifactWritten`, step 10).
- **Pass-through manifests**: dbt's parse result, or the `manifest.json` this invocation
  wrote (its `metadata.invocation_id`); reports only from `dbtx build` / `compile`.
- **Target fallback** to the registered adapter for every command.
- **Best-effort**: one warning per recorder, then silence; strict mode re-raises only
  unknown app-enforced values.
- **Bare `dbtx docs`** is a help request.

## Deviations from the plan

See the Phase 5 status block in the plan.

## Test environment

- Run the suite with `uv run --extra postgres pytest ...` (psycopg is in the `postgres` extra).
- Python 3.10: `UV_PROJECT_ENVIRONMENT=<scratch>/venv310 uv run --python 3.10 --extra postgres pytest tests` (SQLAlchemy 2.0.54 there).
