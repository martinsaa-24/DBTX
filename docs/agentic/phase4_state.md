# Phase 4 (Loaders and repositories): implementation state

Tracks progress against [orm_implementation_plan.md](orm_implementation_plan.md),
sections 5.1–5.4 and 5.6 (the write paths), 6.7 (`dbtx db load`), 8 (frozen fixtures,
strict mode), D9 and "Phase 4". If a session is interrupted, resume from the first
unchecked item. Update this file after every step.

**Phase 4 complete (2026-10-11).** Committed in `3eea72e`.

**Branch:** `feature/db-backend`, on top of `5d69c94` (the Phase 3 commit). Phase 4 is
committed in `3eea72e`.

**Style rules from the user:** simple, verbose code (repetition is fine where it avoids
abstraction); strict red -> green TDD per functional group, with the red and green
results recorded here (`docs/contributing/guidelines.md`); state comes from test
fixtures, not extra parameters; database connection strings are URIs (`*_uri`); every
test, fixture and helper is type-annotated; a test for some backends only uses an
`ON_*` list, never a skip (`docs/contributing/tests.md`).

**Scope boundary with Phase 5:** Phase 4 delivers every write path (loaders and
repositories, including the report-render rows) and the post-hoc `dbtx db load`, which
exercises them without the runner. Phase 5 wires them into `runner.build` /
`compile_project` / pass-through (the recorder, `ReportScheduler(on_change=...)`), and
the argv → command mapping the recorder needs.

## Steps

- [x] 1. Baseline (2026-10-11): full suite **1057 passed**, 0 skipped
- [x] 2. `which` → `command` table (`enums.py`); app-enforced values (`validation.py`, D9: strict raises, lenient warns once per value); strict mode autouse for the suite (`DBTX_DB_STRICT=1`) → `tests/unit/store/test_store_values.py`
  - `enums.COMMAND_BY_WHICH` + `command_from_which()`; `validation.check_known()` / `UnknownValueError` (a StoreError), warnings on the `dbtx.store` logger, once per (column, value)
  - `store_strict_mode` (autouse, root `tests/conftest.py`, so live tests too); unit `clean_env` keeps `DBTX_DB_STRICT` while clearing the other `DBTX_*` variables
  - Red: 27 tests, **15 failing** (the stubs' None, no raise, no warning, the env var unset). Green: **27 passed**; unit suite **1057 passed**
- [x] 3. Frozen fixtures: `tests/fixtures/run_results.json` (a real build of the example project, scrubbed, by `scripts/freeze_manifest.py`) and a hand-written `tests/fixtures/sources.json` (a `pass` and a `runtime error` entry); a live drift test for the run results
  - `scripts/freeze_manifest.py` also builds a temporary copy of the example (from inside it: the profile's DuckDB path is relative to the cwd) and scrubs run_results: fixed `invocation_id` / `invocation_started_at` / `generated_at`, `elapsed_time`, per-result timing / `execution_time` / `thread_id`, path args, `invocation_command`; results sorted by `unique_id` (threads finish in any order). `examples/` untouched, `manifest.json` unchanged
  - `sources.json` built from dbt's own classes (`SourceFreshnessOutput`, `SourceFreshnessRuntimeError`, `FreshnessMetadata`) serialised one by one (the union field of `FreshnessExecutionResultArtifact` would not serialise), so the keys are dbt 1.12's
  - `tests/conftest.py`: `FROZEN_RUN_RESULTS`, `FROZEN_SOURCES`
  - Red: `test_frozen_run_results_match_example_project` (no `frozen_run_results`). Green: both drift tests pass, twice in a row (deterministic)
  - The frozen build: 48 results (25 pass, 15 success, 7 no-op exposures, 1 warn)
- [x] 4. `loaders/manifest.py`: a manifest (object, or `manifest.json` read into one) → project, adapter type, `seen_at`, node / node-version / edge rows → `tests/unit/store/test_store_loaders_manifest.py`
  - `read_manifest(path)` (`WritableManifest.read_and_check_versions` + `Manifest.from_writable_manifest`, as the test suite's `manifest` fixture does): the post-hoc path becomes a Manifest too, so `manifest_rows` is the one mapping
  - Edges come from each stored node's `depends_on.nodes` (what dbt builds `parent_map` from): a Manifest object has no `parent_map` until `build_parent_and_child_maps()` mutates it. The test checks the result equals the frozen manifest's `parent_map`
  - `generated_at`: dbt fills it with "now" when the JSON lacks it (the frozen fixture strips it), so tests set it
  - Red: 8 tests, **8 failing** (the stub's None). Green: **8 passed**
- [x] 5. `repositories/nodes.py`: `sync_manifest` (upserts with the merge rules, edges with both ends stored, removals, re-adds, checksum reverts, out-of-order loads, chunks of 500) → `tests/unit/store/test_store_nodes.py`
  - **Deviation (out-of-order correctness):** the plan's node upsert updates `resource_type` / `package_name` / `name` / `checksum` and clears `removed_at` unconditionally, so an older manifest loaded later would restore an old checksum and undo a removal, and a node only in the older manifest would never be marked removed. Instead: (1) upsert merging only first/last seen (least / greatest; a new row is inserted whole); (2) attributes and `removed_at = NULL` via an executemany UPDATE on rows whose `last_seen_at` equals this manifest's time after the merge (this manifest is their newest); (3) removals against the project's newest manifest time (`max(node.last_seen_at)`), which equals `seen_at` when loads come in order. No dialect branch: Core UPDATEs and the adapter's `upsert`
  - Returns `SyncCounts(nodes, versions, edges, removed_nodes, removed_edges)`
  - Red: 12 tests, **33 failing** across the backends; 3 passing (the "same manifest again" snapshot compares two empty tables with the stub, meaningful only once rows are written). Green: **36 passed**
- [x] 6. `loaders/run_results.py`: run_results and sources.json shapes → invocation fields and node-result rows; secrets scrubbed from `args` → `tests/unit/store/test_store_loaders_run_results.py`
  - `InvocationRecord` (id, command, start / end, dbt version, target, selector, args, job id); `invocation_from_run_results` / `invocation_from_sources`; `run_result_rows(run_results, project, checksums)` / `freshness_result_rows(sources, project)`
  - Timestamps parsed by `parse_timestamp` (dbt writes `...Z`; Python 3.10's `fromisoformat` rejects it). Start = `invocation_started_at`, else `generated_at` minus `elapsed_time`; end = `generated_at`
  - **Decision:** post-hoc `target` = `args.target` when given, else `'unknown'`: run_results only carries `target` when `--target` was passed, and dbtx never resolves profiles itself (plan 5.2). The in-process recorder (Phase 5) gets the real target from `ConcurrencyLine`
  - `args` scrubbed with dbt's `env_secrets()` / `scrub_secrets()` over every string, on a copy
  - Red: 21 tests, **20 failing** (stub None); 1 passing (the "artifacts unchanged" check, trivially true with the stub). Green: **21 passed**
- [x] 7. `repositories/jobs.py`: `enqueue`, `claim_next`, `finish`, `cancel`, `JobStateError`; external jobs for invocations launched outside the queue → `tests/unit/store/test_store_jobs.py`
  - `enqueue` (uuid4 by default; an existing id → JobStateError), `claim_next` (the adapter's claim), `finish` (only `running`; result checked against `RunResult` → ValueError), `cancel` (only `pending`), all through one guarded `move()`: `UPDATE ... WHERE status = :expected`, and on no change JobStateError `no job 'x'` / `job 'x' is <status>, not <expected>`
  - `record_external(session, adapter, job_id, project, at)`: upsert-do-nothing of a finished `external` job (requested / start / end = `at`)
  - Red: 16 tests, **45 failing**; 3 passing (empty-queue claim → None, which the stub returns). Green: **48 passed**
- [x] 8. `repositories/invocations.py` (`start`, `finish`, result from the node statuses) and `repositories/results.py` (`load`: unknown nodes skipped, app-enforced status, idempotent) → `tests/unit/store/test_store_invocations.py`
  - `invocations.start(session, adapter, project=, adapter_type=, record=, run_by=, host=)`: upsert on `invocation_id` (a reload resets and re-sets the row in the same transaction); an unrecorded command → StoreError; a job id the queue never saw → `jobs.record_external` first. `finish(...)` → StoreError for an invocation never started. `result_from_statuses`: the plan's rule (`partial success` counts as success, as the rule says nothing about it)
  - `results.load(session, adapter, project, rows)` → (stored, skipped): stored node ids looked up in chunks of 500; `check_known("node_result.status", ...)` per kept row (strict raises before anything is written); debug log of the skipped count; upsert on the PK
  - **Decision:** a post-hoc load stores `run_by` / `host` as NULL: the artifacts do not record who ran dbt or where, and the loading user and host would be wrong. Phase 5's recorder sets them
  - Red: 16 tests, **43 failing**; 3 passing (a queued job left alone: the do-nothing stub cannot touch it). Green: **46 passed**
- [x] 9. `repositories/reports.py`: `sync` (merge rules, removals) and the render lifecycle (pending, running, finished) → `tests/unit/store/test_store_reports.py`
  - `sync(session, adapter, project, reports, seen_at)` → (stored, removed): the node sync's rules (merge sighting times; other columns and `removed_at = NULL` only where this manifest is the newest; removals at the project's newest manifest time, `newest_sighting` from the node table, since the manifest sync runs first). JSON columns updated through typed bind parameters
  - Render rows: `insert_pending_renders` (upsert-do-nothing), `mark_render_running` (`started_at` = `clock.now()`), `mark_render_finished(status=, detail=, paths=)` (`completed_at` = `clock.now()`; a non-final status → ValueError), `render_paths([Path])` → {suffix: path} for the Phase 5 recorder
  - `ReportRender.paths` annotated `dict[str, str] | None` (was `list[str]`): the spec says json, "one per format", and the plan stores {format: path}. Annotation only, no DDL change
  - Red: 10 tests, **25 failing**. Green: **25 passed**
- [x] 10. `dbtx db load [--run-loc DIR]`: the post-hoc import. Done when: loading the example project's artifacts twice gives identical row counts and no errors → `tests/unit/store/test_store_db_load.py`
  - `store/cli.py`: `load_command` / `parse_load_args` (`--project-dir` accepted and skipped: config resolved it; `--run-loc` relative to the cwd, default `<project>/target`), `load_artifacts`: checks (manifest present; run_results and/or sources present; readable; the manifest's `project_name` is the folder's) before opening the store; `open_checked_store` (SQLite auto-upgrade, revision) then `check_project_claim` (G1 + G2, as a run); then one transaction: `sync_manifest`, `reports.sync` (reports from `discover_reports` with the project's templates; `ReportDefinitionError` / `TemplateError` → warning, skipped), and per artifact `load_invocation` (start, `results.load`, finish)
  - **Decision:** `manifest_hash` only on the invocation whose id is the manifest's `metadata.invocation_id` (plain dbt writes the manifest in the same invocation); otherwise NULL. The result comes from all the artifact's statuses (a failed hook counts, even though its row is skipped)
  - `dbtx --help` lists `dbtx db load`; `DB_USAGE` includes `load`. The two "not available yet" tests are gone (nothing in `dbtx db` is unavailable now; the unknown-subcommand test covers dispatch)
  - Red: 12 tests, **26 failing** with the help and usage tests (`not available yet`). Green: **23 passed**; two expectations corrected while going green (`test_unknown_arguments` also sees config's not-enforced DB_URI warning; the usage strings now include `load`)
  - Full suite **1290 passed**
  - Observed, not caused by this phase: `pytest tests/unit/store/A.py tests/unit/test_cli_config.py tests/unit/store/B.py` (store files on both sides of a unit file) reports "fixture 'store' not found" for the second store file; identical with Phase 4 stashed. Running from `tests/` or per directory is unaffected
- [x] 11. Full suite, Python 3.10, pyflakes / isort, annotations
  - pyflakes clean (`tests`, `src/dbtx`, `scripts`); isort clean after sorting `test_store_invocations.py` and `store/cli.py`; every test, fixture and helper annotated
  - **Found on Python 3.10:** `datetime.fromisoformat` before 3.11 accepts only 3- or 6-digit fractions (`...:00.5` failed). `parse_timestamp` now pads / trims the fraction to 6 digits (the 3.10 failure of `test_timing_spans_the_earliest_start_and_the_latest_completion` was the red)
  - Full suite **1290 passed** on Python 3.14 and on 3.10.22 (SQLAlchemy 2.0.54)
- [x] 12. Docs: README (`dbtx db load`), plan Phase 4 status, this file
  - README "Importing plain dbt runs: `dbtx db load`"; `docs/contributing/tests.md` (frozen run results and sources, strict mode, the new test files); plan Phase 4 status block with the deviations

## Decisions made during implementation

- **One manifest mapping:** the post-hoc path reads `manifest.json` into a Manifest; edges from `depends_on.nodes`.
- **Out-of-order correctness** for nodes, edges and reports (see the deviation in the plan's Phase 4 status).
- **Post-hoc unknowns:** `target` = `args.target` or `'unknown'`; `run_by` / `host` NULL.
- **`manifest_hash`** only on the invocation the manifest names in its metadata.
- **Render rows are repository functions** taking times from `dbtx.store.clock`; the Phase 5 recorder calls them.
- **The invocation result** comes from all of an artifact's statuses, including results whose node is not stored (a failed hook fails the run).
- **`dbtx db load` checks everything it can before opening the store** (files, readability, project name), then claims the name (G2), then writes in one transaction: a strict-mode refusal leaves no rows.
- **Timestamps** are parsed by `parse_timestamp`, which normalises `Z` and the fraction for Python 3.10.

## Deviations from the plan

See the Phase 4 status block in the plan: out-of-order correctness; one manifest mapping; post-hoc target / user / host; `manifest_hash` pairing; render rows here, observer in Phase 5; argv mapping left for Phase 5.

## Test environment

- Run the suite with `uv run --extra postgres pytest ...` (psycopg is in the `postgres` extra).
- Python 3.10: `UV_PROJECT_ENVIRONMENT=<scratch>/venv310 uv run --python 3.10 --extra postgres pytest tests` (SQLAlchemy 2.0.54 there).
