# dbtx store: implementation plan

Implements the schema in [`src/dbtx/store/schema.yml`](../../src/dbtx/store/schema.yml) (version 2; until Phase 1 it lived at `docs/agentic/orm_to_implement.yml`) as a
SQLAlchemy 2.x data layer with pluggable **store adapters**. SQLite and PostgreSQL are the
two supported backends. Other dialects can be added later through the adapter registry.

The store records what dbtx and dbt do: jobs, invocations, the nodes of the project
over time, per-node results, and report definitions and renders. It **adds** to the
current file outputs (`target/`, `dbtx_runtime.json`) and does not replace them in
this plan.

**Checked against:** dbt-core 1.12.5 in the project venv (Python 3.14.8, bundled SQLite
3.53.1). Statements about dbt internals below name the module they were read from.
Section 11 lists what changed in the plan after those checks and later review.

---

## 1. Design decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **SQLAlchemy 2.x declarative ORM** (`Mapped[...]`, `mapped_column`) for models. **Core** `insert()` with dialect `on_conflict_do_update` for bulk upserts. | Typed models for reads and for tests. Core for the manifest sync, which upserts thousands of rows per run. |
| D2 | **Adapter layer** (`StoreAdapter`) owns every dialect difference: engine/connection setup, upsert construct, `greatest`/`least`, job claiming, JSON type variant. Repositories never branch on dialect. | This is the "adapter style" the request asks for, the same approach dbt uses for warehouses. A third backend means adding one subclass. |
| D3 | **The YAML is the source of truth.** It moves into the package as `src/dbtx/store/schema.yml`. Models are hand-written, and a **parity test** fails if `Base.metadata` and the YAML disagree. | Hand-written models keep IDE support and type checking. The parity test stops the models and the spec from drifting apart. Building models from YAML at runtime would lose both of those benefits. |
| D4 | **Alembic** migrations ship inside the package and run programmatically, so users need no `alembic.ini`. `render_as_batch=True` handles SQLite. | SQLite cannot `ALTER` constraints. Batch mode is the standard workaround. |
| D5 | **Fail fast at start-up; best-effort once dbt is running.** Configuration, driver, connectivity and schema-version problems exit 2 **before** dbt starts (section 6). After dbt has started, every write from the recorder is best-effort: an exception is caught, logged through `fire_event` at WARN, and the run continues with its exit code unchanged. | Configuration is checked first (6.1), so a misconfigured backend is found before any work is done. A failure partway through a run must not discard a build that already succeeded. Strict mode (D9) is for tests. |
| D6 | **Short-lived sessions, one per unit of work.** No session is shared across dbt threads. | Report renders and node events arrive on dbt's worker threads (see `scheduler.py`). |
| D7 | **All entities in one namespace.** The YAML groups (`events`, `dbt_nodes`, `dbtx_actions`) become Python modules, not database schemas. On Postgres, all tables go into a configurable schema (default `dbtx`) via `schema_translate_map`. | Table names such as `job`, `node` and `report` are generic and would clash in a shared Postgres database. SQLite has no schemas, so a single namespace keeps both backends identical. |
| D8 | **Timestamps:** one `UTCDateTime` `TypeDecorator`. Naive datetimes are rejected on bind. Aware ones are converted to UTC. Results always come back aware UTC. | Required by the spec ("always stored and returned as UTC"). SQLite returns naive strings and Postgres returns times in the session's timezone, so both need normalising. |
| D9 | **`enforced_by: app`** values are checked in Python against a known set. In production an unknown value is **stored, with a warning logged once per value**. In strict mode (`DBTX_DB_STRICT=1`, set in the test suite) it raises an error. | Meets the spec's intent ("a dbt upgrade adding a value cannot break inserts") while still showing new values in CI. |
| D10 | **Python 3.10 floor** (from `pyproject.toml`): enums are `class X(str, Enum)` because `StrEnum` needs 3.11. The dev venv runs **3.14**, so test on both ends of the range. | Matches `NodeTypes` in `docs/catalog.py`. |
| D11 | **Platform mode is the default and the only gate to the database.** `--standalone-mode` opts out: no database, no config file, no platform checks (6.5.1). | Every database write belongs to a platform project with a known folder identity, so the project guards (G1/G2) always apply when the store is touched. Standalone runs can never touch a shared database by accident. |

### Dependencies

```toml
dependencies = [
    ...,
    "sqlalchemy>=2.0.44,<3",   # floor chosen for Python 3.14 support; confirm on first `uv add`
    "alembic>=1.13,<2",
]

[project.optional-dependencies]
postgres = ["psycopg[binary]>=3.1,<4"]   # URL scheme: postgresql+psycopg://
```

SQLite uses the standard library driver (`sqlite+pysqlite`). It requires SQLite ≥ 3.35
for `RETURNING` and ≥ 3.24 for `ON CONFLICT`. Check the version at engine creation and
fail with a clear message if it is older. The venv's bundled SQLite (3.53.1) is well
above that floor, but distro Pythons can ship older builds.

---

## 2. Package layout

```
src/dbtx/config.py       # Phase 0: layered config loader, DbtxConfig (section 6)
src/dbtx/store/
  __init__.py            # public API: open_store(DbtxConfig), Store
  schema.yml             # the spec, moved here from docs/agentic/orm_to_implement.yml (Phase 1)
  types.py               # UTCDateTime, JSONType, in_check() helper
  enums.py               # db-enforced enums + app-enforced known-value sets
  validation.py          # app-enforced checking (D9)
  models/
    __init__.py          # re-exports every model; imports all modules so metadata is complete
    base.py              # Base, MetaData(naming_convention=...)
    platform.py          # Project (spec v2 registry)
    events.py            # Job, Invocation
    dbt_nodes.py         # Node, NodeVersion, NodeEdge, NodeResult
    dbtx_actions.py      # Report, ReportRender
  adapters/
    __init__.py          # registry: get_adapter(url) by dialect name; entry-point group
    base.py              # StoreAdapter ABC
    sqlite.py            # SqliteAdapter
    postgres.py          # PostgresAdapter
  repositories/
    jobs.py              # enqueue, claim_next, finish, cancel
    invocations.py       # start, finish
    nodes.py             # sync_manifest (node, node_version, node_edge)
    results.py           # load node_result rows
    reports.py           # sync_reports, render lifecycle
  loaders/
    manifest.py          # manifest (object or JSON dict) -> row dicts
    run_results.py       # run_results.json-shaped dict -> invocation + node_result rows
  recorder.py            # runner hook: ties a dbtx build/compile to the store (section 5)
  migrations/
    env.py
    script.py.mako
    versions/0001_initial.py
  cli.py                 # `dbtx db init|upgrade|status|load`
```

**Naming collision:** `dbtx.docs.catalog.NodeResult` already exists. Import ORM models
through the module (`from dbtx.store import models as m` → `m.NodeResult`) and never
star-import them next to the docs catalog.

---

## 3. Mapping the YAML to SQLAlchemy

### 3.1 Field types (`types.py`)

| YAML | SQLAlchemy | SQLite storage | Postgres storage |
|---|---|---|---|
| `string` | `sa.String()` (no length) | `VARCHAR` → TEXT affinity | `VARCHAR` (unbounded) |
| `integer` | `sa.Integer()` | `INTEGER` | `INTEGER` |
| `float` | `sa.Double()` | `REAL` (8-byte) | `DOUBLE PRECISION` |
| `timestamp` | `UTCDateTime` (wraps `sa.DateTime(timezone=True)`) | fixed-width ISO text, UTC | `TIMESTAMPTZ` |
| `json` | `JSONType = sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")` | TEXT | `JSONB` |

Notes:
- `UTCDateTime.process_bind_param` raises `ValueError` on naive input and calls
  `.astimezone(timezone.utc)` on aware input. `process_result_value` adds `tzinfo=UTC`
  when the value is naive (SQLite) and converts to UTC otherwise (Postgres). The
  timestamp indexes (`(status, requested_at)`, `(project, start_time)` and so on) need
  string ordering to match time ordering on SQLite. SQLAlchemy's SQLite `DATETIME`
  storage format is fixed width with microseconds, so this holds **as long as every
  value is UTC**, which the decorator guarantees. Add a test for it.
- The Postgres adapter sets `options='-c timezone=UTC'` on connect, so ad-hoc SQL
  sessions see UTC too.
- JSON uses `none_as_null=True` so Python `None` becomes SQL `NULL` on the nullable
  columns (`adapter_response`, `paths`), not the JSON literal `null`. JSONB is used
  over JSON on Postgres so the column can be indexed and queried with containment
  operators later.

### 3.2 Keys, constraints and indexes

- `MetaData(naming_convention=...)` with
  `pk_%(table_name)s`, `fk_%(table_name)s_%(column_0_N_name)s`,
  `ix_%(table_name)s_%(column_0_N_name)s`, `ck_%(table_name)s_%(constraint_name)s`,
  `uq_%(table_name)s_%(column_0_N_name)s`. Alembic batch mode on SQLite needs
  deterministic names.
- `primary_key` → `PrimaryKeyConstraint(*cols)` in `__table_args__`. Composite keys are
  listed in YAML order. Column order inside an index matters for the queries the YAML
  comments name.
- `foreign_keys` → one `ForeignKeyConstraint(columns, [f"{table}.{col}", ...])` per YAML
  entry, so composite keys stay composite. No `ON DELETE` cascade, because history is
  append-only. Removal is soft (`removed_at`).
- `indexes` → `sa.Index(None, *cols)` (name from the convention).
- `enforced_by: db` → `CheckConstraint(in_check(col, values), name=col)`. For nullable
  columns (`job.result`, `invocation.result`) the check is written as
  `col IS NULL OR col IN (...)`. SQL already lets `NULL` pass an `IN` check, but writing
  it out makes the intent clear.
- **SQLite enforces foreign keys only with `PRAGMA foreign_keys=ON`** on every
  connection. The SQLite adapter sets it in a `connect` event. A test checks that a
  dangling FK insert fails on both backends.

### 3.3 Enums (`enums.py`)

DB-enforced (CHECK constraint, and a Python `str, Enum` for type hints):
`JobStatus`, `RunResult` (shared by `job.result` and `invocation.result`),
`InvocationCommand`, `NodeResourceType`, `RenderStatus`.

App-enforced (frozen sets, D9): `NODE_RESULT_STATUSES`.

The CHECK lists are built **from the enums**, so a value is defined in one place only.
The parity test checks the enums against `schema.yml`.

### 3.4 Models

One class per entity, named in PascalCase: `Job`, `Invocation`, `Node`, `NodeVersion`,
`NodeEdge`, `NodeResult`, `Report`, `ReportRender`. Read-only `relationship()`s for
navigation (`viewonly=True`, so the ORM never cascades writes):

- `Job.invocations`, `Invocation.job`
- `Invocation.node_results`, `Invocation.report_renders`
- `Node.versions`, `Node.results`, `Node.parents` / `Node.children` (via `NodeEdge`
  with explicit `primaryjoin`)
- `Report.node` (the exposure), `Report.renders`

Use `lazy="raise"` by default so N+1 queries fail loudly in tests. Repositories load
explicitly with `selectinload`.

---

## 4. Adapter layer

```python
class StoreAdapter(ABC):
    dialect: ClassVar[str]                      # "sqlite" | "postgresql"

    def create_engine(self, url: URL, **opts) -> Engine: ...
    def on_connect(self, dbapi_conn, record) -> None: ...   # pragmas / session settings
    def insert(self, table) -> Insert: ...                  # dialect insert() supporting on_conflict
    def upsert(self, table, rows, *, keys, update, merge=None) -> Executable: ...
    def greatest(self, *cols) -> ColumnElement: ...
    def least(self, *cols) -> ColumnElement: ...
    def claim_next_job(self, session) -> Job | None: ...
    def check_server(self, conn) -> None: ...              # version floor checks
```

`upsert(..., merge=...)` takes per-column merge rules. Repositories use them to express
"keep the earliest `first_seen_at`, keep the latest `last_seen_at`" without writing any
dialect-specific SQL:

```python
adapter.upsert(Node.__table__, rows, keys=["project", "unique_id"],
               update=["resource_type", "package_name", "name", "checksum"],
               merge={"first_seen_at": "least", "last_seen_at": "greatest",
                      "removed_at": "clear"})
```

### SqliteAdapter
- URL default: `sqlite+pysqlite:///<project_dir>/.dbtx/dbtx.db` (section 6). The adapter
  creates the parent directory.
- `on_connect`: `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, `synchronous=NORMAL`,
  `busy_timeout=5000`.
- **Transactions:** pysqlite starts transactions lazily, so two writers can deadlock when
  upgrading a read lock. Follow the SQLAlchemy "serializable isolation / savepoints"
  recipe: set `isolation_level=None` at the DBAPI level and emit `BEGIN IMMEDIATE`
  from a `begin` event. Every write transaction then takes the write lock up front, and
  `busy_timeout` queues concurrent writers (dbt threads rendering reports at the same
  time) instead of raising `database is locked`.
- `:memory:` URLs (tests) use `StaticPool` with `check_same_thread=False`.
- `greatest`/`least` → SQLite's multi-argument scalar `max()` / `min()`.
- `claim_next_job`: one atomic statement, safe under `BEGIN IMMEDIATE`:
  `UPDATE job SET status='running', start_time=:now WHERE job_id = (SELECT job_id FROM job WHERE status='pending' ORDER BY requested_at LIMIT 1) RETURNING *`.

### PostgresAdapter
- Driver: psycopg 3 (`postgresql+psycopg://`). `pool_pre_ping=True`.
- `on_connect`: `SET TIME ZONE 'UTC'`. The search path comes from
  `schema_translate_map={None: config.db_schema}`. `dbtx db init` runs
  `CREATE SCHEMA IF NOT EXISTS` when it has permission to.
- `greatest`/`least` → `func.greatest` / `func.least`.
- `claim_next_job`: `SELECT ... WHERE status='pending' ORDER BY requested_at LIMIT 1 FOR UPDATE SKIP LOCKED`, then `UPDATE`. This lets several workers claim jobs without blocking each other. It is served by the `(status, requested_at)` index.
- `check_server`: Postgres ≥ 12.

### Registry
`get_adapter(url)` resolves `make_url(url).get_backend_name()` against a dict seeded with
the two built-in adapters and extended from the `dbtx.store_adapters` entry-point group.
An unknown dialect gets an error that lists the supported ones. DuckDB, which the
examples use for the warehouse, is a natural third adapter but is out of scope here.

---

## 5. Repositories and loaders: the write paths

All repository functions take `(session, adapter, ...)`. They contain no dialect
branching and do no commits (the caller owns the transaction).

### 5.1 Manifest sync: `nodes.sync_manifest(project, manifest, seen_at)`
Input is a dbt `Manifest` object (in-process, the one `prepare()` already parsed) or a
`manifest.json` dict (post-hoc load).
`loaders/manifest.py` turns either one into the same row dicts.

1. **Nodes:** iterate `nodes`, `sources`, `exposures` and `unit_tests`. Keep only
   `resource_type ∈ NodeResourceType` (the YAML says "the loader only stores these
   types"). Analyses, operations, macros, semantic models and similar are dropped.
   `checksum = node.checksum.checksum` when present, else `NULL` (sources, exposures).
   Upsert with the merge rules shown in section 4. `removed_at` is cleared because the
   node is present again.
2. **Node versions:** for each node with a checksum, upsert
   `(project, unique_id, checksum)`. `first_seen_at` takes `least`, `last_seen_at` takes
   `greatest`, so a node that reverts to an old checksum bumps that old version's row.
3. **Edges:** from `parent_map`, keeping only pairs where **both** ends were stored in
   step 1 (otherwise the FK would fail, for example a test that depends on a macro-only
   node). Same upsert and merge rules.
4. **Removals:** `UPDATE node SET removed_at=:seen_at WHERE project=:p AND removed_at IS NULL AND last_seen_at < :seen_at`, and the same for `node_edge`. This is correct
   because a manifest always describes the whole project, even for a selected run.
5. **No hash short-circuit.** `manifest.json` bytes include `metadata.invocation_id` and
   `generated_at`, so `manifest_hash` changes on every parse even when the project has
   not changed. Skipping the sync when the hash matches would never trigger. The sync
   stays as chunked, set-based upserts. Skipping unchanged projects would need a
   separate content hash, which the spec does not define, so it is out of scope.
6. `seen_at` is the manifest's `metadata.generated_at` (a naive UTC datetime in dbt
   1.12; add `tzinfo=UTC`). Out-of-order
   post-hoc loads are safe because the merges are `least`/`greatest`, and step 4 only
   touches rows last seen *before* this manifest.

Batch rows in chunks of 500 to stay under SQLite's bound-parameter limit.

### 5.2 Invocations: `invocations.start(...)` / `invocations.finish(...)`
- **Which invocation:** `dbtx build` calls `dbtRunner.invoke` three times (parse, ls,
  build). Only the **user's command** (the final `build`/`compile`, or the pass-through
  command) is recorded. The others are dbtx internals.
- **`invocation_id`:** dbt cannot be given an id in advance. `dbt.cli.requires.preflight`
  calls `dbt_common.invocation.reset_invocation_id()` on every `invoke`, and there is no
  flag or env var for it. The recorder therefore starts the invocation **lazily**, on
  the first event its callback receives. That event is always `MainReportVersion`,
  fired immediately after the reset and after `setup_event_logger` installs the
  callbacks. Take `invocation_id` from `event.info.invocation_id` and `start_time` from
  `dbt_common.invocation.get_invocation_started_at()` (naive UTC; add `tzinfo=UTC`).
  If `invoke` fails before preflight (a click usage error), no event fires and no
  invocation is recorded. That is the intended behaviour.
- `command`: the recorder already has the user's argv, so it maps from that:
  `args[0]`, or `args[0:2]` for the `docs`/`source` groups. The post-hoc loader only has
  run_results `args.which`, which is the **leaf** command name (`dbt.cli.flags`:
  `invoked_subcommand_name or ctx.info_name`), so it needs this table:

  | argv | `which` | stored `command` |
  |---|---|---|
  | `build` `compile` `test` `run` `seed` `snapshot` `parse` `run-operation` | same | same |
  | `source freshness` / `source snapshot-freshness` (deprecated alias) | `freshness` / `snapshot-freshness` | `source freshness` |
  | `docs generate` | `generate` | `docs generate` |

  The table lives in `enums.py` next to `InvocationCommand`, with a unit test per row.
  Commands outside the list (`ls`/`list`, `debug`, `deps`, `clean`, `init`, `show`,
  `retry`, `clone`, `docs serve`) are **not recorded** (decision Q3).
- `project`, `adapter_type`: `manifest.metadata`. `dbt_version`: in-process
  `dbt.version.__version__`; post-hoc, the artifact's `metadata.dbt_version`.
- `target`: from the **`ConcurrencyLine`** event (`event.data.target_name`). dbt fires it
  in `RunnableTask.execute_with_hooks` from `self.config.target_name`, so it is the
  resolved name even when `--target` was not given. That covers build, compile, run,
  test, seed, snapshot, source freshness and docs generate (whose `GenerateTask`
  extends `CompileTask`). **`parse` and `run-operation` never fire it.** For those, read
  the **registered adapter's** `config.target_name` (the adapter's `RuntimeConfig`)
  when `CommandCompleted` arrives.
  - Checked against dbt 1.12.5 with a probe: for both `parse` and `run-operation`, the
    adapter in `dbt.adapters.factory.FACTORY` is registered and carries the resolved
    target (`dev`) by `CommandCompleted`.
  - This is dbt's own answer after `--target`, `DBT_TARGET`, `--profile`,
    `DBT_PROFILE` and the profile default, so **dbtx never resolves profiles
    itself**. The adapter's `type()` gives `adapter_type` the same way when there is
    no manifest (5.5).
  - The column is non-nullable. If no adapter was registered (dbt failed before
    loading the profile), store `'unknown'` and log a warning.
- `selector`: `args.select` (joined with a space) or `args.selector`.
- `args`:
  - Commands that return a `RunExecutionResult` (build, compile, run, test, seed,
    snapshot, run-operation): take `args` from the serialised artifact (section 5.3).
    dbt has already **scrubbed `DBT_ENV_SECRET_*` vars** out of it.
  - `source freshness` returns a `FreshnessResult`, which has no `args`. Docs generate
    returns a `CatalogArtifact`, and parse returns a `Manifest`. For these, the recorder
    builds `args` itself with `dbt.utils.args_to_dict(get_flags())`, captured during
    the invocation (flags are set before `MainReportVersion` fires), and applies the
    same scrub (`dbt_common.events.functions.scrub_secrets` over `DBT_ENV_SECRET_*`
    values).
- `job_id`: `args.vars[job_id_var]`, reusing `dbtx.docs.catalog.read_job_id` so both
  features read the same var. If the job row does not exist (a `dbt run --vars job_id:x`
  outside the queue), insert one with `trigger='external'`, `status='finished'`, so the
  FK holds.
- `run_by`: `getpass.getuser()`. `host`: `socket.gethostname()`. Both are wrapped so a
  failure gives `NULL`.
- **`finish`:** `end_time`; `result` derived from the node results (any
  `error`/`fail`/`runtime error` → `failure`, else any `warn` → `warning`, else
  `success`). If dbt raised with no node results, `result='failure'` and
  `error_message = str(exception)`.
- **`manifest_hash`:** sha256 of `<target>/manifest.json` read **after the invocation
  returns**. Every runnable task calls `write_manifest()` at the end of `run()`, next to
  `run_results.json` (`dbt/task/runnable.py:790`, under `if self.args.write_json`).
  This happens even when dbtx hands in a pre-parsed manifest. `setup_manifest` skips
  parsing, not the final write.
  - The rewritten file differs from the one `prepare()`'s internal parse wrote: it
    gains `compiled_code`, `compiled_path`, `extra_ctes`, macro `depends_on` and so on.
    Its `metadata` still carries the *parse's* `invocation_id` and `generated_at`.
  - Hashing after the invocation is therefore the only choice that names a file which
    "stays on disk". It is also the file paired with that invocation's
    `run_results.json`.
  - `NULL` when the file is missing: `--no-write-json`, or `parse`/`docs generate`
    failures.

### 5.3 Node results: `results.load(invocation_id, project, run_results_dict)`
**One loader for both paths, two artifact shapes.** Both the in-process and post-hoc
paths go through `loaders/run_results.py`:

| Result | In-process serialisation (same code dbt uses to write the file) | Post-hoc file |
|---|---|---|
| `RunExecutionResult` | `RunResultsArtifact.from_execution_results(results=r.results, elapsed_time=r.elapsed_time, generated_at=r.generated_at, args=r.args).to_dict(omit_none=False)` (`dbt.artifacts.schemas.run`) | `run_results.json` |
| `FreshnessResult` | `FreshnessExecutionResultArtifact.from_result(r).to_dict(omit_none=False)` (`dbt.artifacts.schemas.freshness`) | `sources.json` (not `run_results.json`) |

Notes:
- Using dbt's serialiser rather than reading attributes directly means one mapping for
  both paths, with secrets already scrubbed.
- `from_execution_results` drops anything that is not a `RunResult`, exactly as the
  written file does.
- `docs generate` and `parse` produce no node results, so their invocation row has no
  children.

Mapping (run_results shape; freshness differences in the last column):

| Column | run_results source | sources.json difference |
|---|---|---|
| `status` | `result.status` (app-enforced, D9) | `pass`/`warn`/`error`/`runtime error` |
| `checksum` | node checksum from the manifest of the same run (`NULL` if unknown) | always `NULL` (sources have none) |
| `started_at` / `completed_at` | earliest `timing[].started_at` / latest `timing[].completed_at`. `NULL` when `timing` is empty (skipped nodes) | same. A `runtime error` entry has no `timing` |
| `execution_time` | `execution_time` | same. `0.0` for `runtime error` (the column is non-nullable) |
| `thread_id`, `message`, `failures` | same-named fields | `message` ← `error`, `failures` `NULL` |
| `rows_affected` | `adapter_response.rows_affected` | `NULL` |
| `adapter_response` | `adapter_response` (`NULL` if `{}`) | same |

Results whose `unique_id` is not in `node` are skipped and counted in a debug log. This
covers on-run-start/end hooks, analyses, and the `operation.*` result of
`run-operation`. All rows go in as one batched upsert keyed on the PK, so
reloading the same run_results is idempotent.

### 5.4 Reports: `reports.sync(project, reports, seen_at)` and the render lifecycle
- `sync` takes the `dict[str, Report]` from `discover_reports`. `config` =
  `report.config.model_dump(mode="json")`. `config_hash` = sha256 of
  `json.dumps(config, sort_keys=True, separators=(",", ":"))`. `relation`, `formats`,
  `template = config.template_path or config.template`. Same upsert/merge/removal
  pattern as nodes. Only reports present in the parsed manifest are touched.
- **Render lifecycle:** add an optional `on_change: Callable[[str, Outcome], None]`
  observer to `ReportScheduler`, called after every outcome transition (outside the
  lock). The scheduler keeps no knowledge of the store. The recorder:
  - inserts `pending` rows for every plan when the build invocation row is created
    (`config_hash` from the report)
  - `running` → sets `started_at`
  - `rendered` / `skipped` / `error` → sets `completed_at`, `detail`, and
    `paths = {fmt: str(path)}` (format → path, so a consumer can find "the csv" without
    parsing extensions)
- These writes happen on dbt node threads: each one is its own short transaction (D6),
  and on SQLite `BEGIN IMMEDIATE` plus `busy_timeout` queues them.

### 5.5 Clean, full-parse and failed-parse environments

Checked by running `dbtx build --no-partial-parse` on a fresh copy of
`examples/jaffle_shop` with no `target/`, `logs/` or database file.

**`--no-partial-parse` / full re-parse: no special handling needed.**
- `partial_parse` is accepted by every subcommand, so `explicit_params` keeps it and
  `argv_for` forwards it to dbtx's internal `parse` and `ls` (checked). The build itself
  is handed the parsed manifest and does not parse again.
- What the store reads from a manifest is the same whether it was parsed fully or
  partially: node set, `checksum.checksum`, `parent_map`. The probe confirmed checksums
  and `parent_map` are identical between the parse-written and build-written
  manifests. A full parse therefore causes no spurious `node_version` rows and no
  edge churn.
- `metadata.generated_at` (used as `seen_at`) is the parse time in both cases.

**Clean environment (no `target/`, first run): works, with one consequence.**

| Sequence (observed) | `target/manifest.json` |
|---|---|
| internal `parse` | none → written |
| internal `ls` (manifest handed in) | unchanged |
| `build` (manifest handed in) | **rewritten** (see `manifest_hash`, 5.2) |

- The first sync inserts every node with `first_seen_at = seen_at`. Nothing is marked
  removed, because no older rows exist.
- An existing store keeps its history through `dbt clean` as long as `DB_URI` points
  outside dbt's `clean-targets` (default `target`, `dbt_packages`). There is no
  built-in default URI (6.4); the documented example is
  `sqlite:///.dbtx/dbtx.db`. If a user points `DB_URI` under `target/`, or adds its
  directory to `clean-targets`, history is lost. `dbtx db status` warns when the SQLite file
  sits inside a configured clean target.
- A missing store is created on first open for SQLite: directory, file and
  `upgrade head`. Postgres is never auto-created: the start-up check (6.6 step 8)
  exits 2 with `run dbtx db init`, before dbt runs.
- `--no-write-json` writes neither `manifest.json` nor `run_results.json`. The
  in-process recorder is unaffected, because it serialises results in memory (5.3).
  Only `manifest_hash` is `NULL`, and `dbtx db load` has nothing to import.

**Parse fails (missing `dbt deps`, broken YAML, no `profiles.yml`): a gap in the
earlier plan.**
- `prepare()` handles a failed parse by re-running the user's command
  (`dbtRunner().invoke([command, *args])`) so dbt reports the error itself. The plan
  only attached the recorder to the final build invocation, so this run would go
  unrecorded. Yet it is exactly what `invocation.error_message` ("command-level
  failure, no node results") exists for.
- **Fix:** pass the recorder's callback to that fallback invoke as well. With no
  manifest, the non-nullable columns are resolved without one:

  | Column | Source when there is no manifest |
  |---|---|
  | `project` | `name:` from `dbt_project.yml` (what `metadata.project_name` is copied from) |
  | `adapter_type`, `target` | the registered adapter (5.2): `type()` and `config.target_name`, if dbt got as far as loading the profile |
  | `dbt_version` | `dbt.version.__version__` |
  | `result` / `error_message` | `failure` / `str(result.exception)` |

  - If `dbt_project.yml` itself cannot be read, nothing is recorded: there is no
    project to attribute the row to.
  - If dbt failed before registering an adapter (the common clean-machine case of no
    `profiles.yml`), store `'unknown'` for `adapter_type` and `target`. dbtx does not
    read `profiles.yml` to fill them in.
  - The manifest, node and report syncs are skipped. There is nothing to sync, and
    the previous rows must not be marked removed because of a failed parse.
- The same rule applies to pass-through commands whose parse fails. For any
  invocation that produced no manifest, the recorder must never run the removal
  step (5.1 step 4).

### 5.6 Jobs: `jobs.py`
`enqueue(trigger, job_id=None)` (generates a uuid4 when none is given),
`claim_next()` (delegates to the adapter), `finish(job_id, result)`, `cancel(job_id)`
(only from `pending`). Transitions are guarded by `WHERE status = :expected`, and a
`JobStateError` is raised if no row changed. **A job worker or scheduler process is out
of scope.** This plan delivers the queue primitives, tested for concurrent claims on
both backends.

### 5.7 Recorder (`recorder.py`): runner integration
A thin object that `runner.build`/`compile_project` (and the pass-through branch in
`cli.main`) create when a store is configured, and replace with a no-op otherwise:

```python
recorder = Recorder.for_invocation(config)   # NullRecorder in standalone mode or with DB_BACKEND=disabled
runner = dbtRunner(manifest=..., callbacks=[recorder.on_event, scheduler.on_event])
scheduler = ReportScheduler(plans, render=..., on_change=recorder.report_changed)
...
recorder.finish(result, prep.manifest, prep.reports)
```

`on_event` lazily starts the invocation on `MainReportVersion` (5.2) and picks up
`target_name` from `ConcurrencyLine`. `finish` runs the manifest
sync, report sync, node results and invocation finish in **one transaction** (D5:
failures are logged and swallowed). Report sync has to happen before the pending render
rows are inserted because of the FK. So in `build`, `report.sync` runs during the lazy
start, right after the invocation row is inserted. The manifest sync runs there too,
since `report` → `node` also has an FK.

---

## 6. Configuration (Phase 0)

### 6.1 Principle: configuration is checked first

Loading and validating configuration is the **first thing every `dbtx` command does**.
It runs at the top of `cli.main`, before dispatch, so it comes before:
- `--no-reports` handling
- `usage_error`
- template resolution
- `prepare()`'s internal `dbt parse`
- any connection to the warehouse or the database backend

A configuration problem exits with **code 2** and a message naming the offending
**layer** and **key**. This matches how dbtx already treats invalid report
definitions. dbt is never started on a bad configuration.

This changes D5. The rule "the store never fails a dbt run" now covers only failures
**after dbt has started**: writes from the recorder stay best-effort. Everything
checked at start-up fails fast with exit 2 (6.6):
- configuration
- the driver being installed
- connectivity
- schema version

### 6.2 URI, not individual login options

**Recommendation: one `DB_URI`, in SQLAlchemy URL form.** Do not add separate
host/port/user/password options.
- It is the form SQLAlchemy and Alembic take, so there is no translation layer.
- It covers SQLite, which has no host or user, with the same option:
  `sqlite:///path/to/dbtx.db`.
- Postgres connection parameters that would otherwise each need an option (`sslmode`,
  `application_name`, `connect_timeout`) go in the query string.
- **Passwords can be left out of the URI.** psycopg 3 is a libpq wrapper, so a URI
  without a password (`postgresql+psycopg://dbtx@db.internal:5432/dbtx`) is completed
  by libpq's standard mechanisms: `~/.pgpass` / `PGPASSFILE`, `PGPASSWORD` and
  `PGSERVICE`. This keeps secrets out of the config file without dbtx having to load
  `.env` (which it does not do, per 6.3). dbtx does not read those variables itself;
  libpq does.
- Every place dbtx prints a URI (errors, `dbtx config`, `dbtx db status`) masks the
  password with `make_url(uri).render_as_string(hide_password=True)`.

### 6.3 Three layers

| Precedence | Layer | Form |
|---|---|---|
| 1 (highest) | CLI options | `--db-backend enabled`, `--db-uri URI` (also the `--opt=value` form) |
| 2 | Config file | `DB_BACKEND=enabled`, `DB_URI=...` |
| 3 (lowest) | Environment variables | `DBTX_DB_BACKEND`, `DBTX_DB_URI` |
| — | Built-in default | only where an option has one (`DB_BACKEND=enabled`) |

Precedence is per option. A value from a higher layer wins even if a lower layer sets
the same option. Layers are never merged inside a single value.

One consequence of this order: an environment variable **cannot override** a value
in a committed config file. CI that needs a different URI than the file has must pass
`--db-uri`.

The three layers apply in **platform mode**. Standalone mode has no database options
and no config file (6.5.1).

**`.env` is not loaded.** dbtx reads only the real process environment (`os.environ`).
It does not depend on `python-dotenv` and does not search for `.env` files. The config
file format below looks like dotenv on purpose, but it is a separate file, parsed by
dbtx with stricter rules.

### 6.4 Options supported out of the box

| Option | CLI | Config file | Env var | Values | Default |
|---|---|---|---|---|---|
| Backend switch (opt-out) | `--db-backend` | `DB_BACKEND` | `DBTX_DB_BACKEND` | `enabled` \| `disabled` (case-insensitive) | `enabled` |
| Database URI | `--db-uri` | `DB_URI` | `DBTX_DB_URI` | SQLAlchemy URL | none; **required when the backend is enabled** |

- All `DB_*` options exist **only in platform mode**. Standalone mode rejects
  `--db-*` flags and ignores `DBTX_DB_*` env vars (6.5.1).
- Setting `enabled` explicitly behaves exactly like the default. It exists so a
  higher layer can turn the backend back on after a lower layer turned it off.
- `disabled` turns off all recording and every `dbtx db` subcommand (except
  `dbtx db status`, which reports that the backend is disabled). It skips the URI
  requirement entirely, so `DB_URI` may be absent or set. A *malformed* `DB_URI` is
  still an error even when disabled, because every configured value is validated.
- Options that later phases need go through the same loader, with the same three
  forms:

  | Option | CLI | Config file | Env var | Added in | Meaning |
  |---|---|---|---|---|---|
  | Postgres schema | `--db-schema` | `DB_SCHEMA` | `DBTX_DB_SCHEMA` | Phase 2 | default `dbtx`; setting it with a SQLite URI is an error |
  | Config file location | `--config-file` | — | `DBTX_CONFIG_FILE` | Phase 0 | (6.5.2) |
  | Platform root | `--platform-dir` | — | — | Phase 0 | CLI only; otherwise cwd holding `dbtx.conf` (6.5.1) |
  | Standalone mode | `--standalone-mode` (boolean flag, no value) | — | — | Phase 0 | CLI only. Opts out of platform mode and therefore of the database; conflicts with `--platform-dir`, `--db-*` and `--config-file` (6.5.1) |

- `DBTX_DB_STRICT` (D9) is a **test switch**, not a configuration option. It is read
  directly from the environment and is never set from the CLI or config file.

### 6.5 Deployment layout and the config file

#### 6.5.1 Two modes

dbtx is the **platform**. A platform holds several dbt projects, each deployed as a
**direct child folder of one common directory, the platform root**. Folder names are
unique by construction, because a directory cannot hold two entries with the same
name. The folder name is the project's **deployment identity**.

```
/srv/dbtx/                  <- platform root
  dbtx.conf                 <- the platform config file: shared DB_URI, etc.
  jaffle-shop/              <- project folder (deployment identity "jaffle-shop")
    dbt_project.yml         <- name: jaffle_shop
    profiles.yml            <- this project's dbt profiles (6.5.6)
  jaffle-shop-2/            <- project folder (deployment identity "jaffle-shop-2")
    dbt_project.yml         <- name: jaffle_shop   <- same dbt name: the guard (6.5.4) rejects it
    profiles.yml
```

**Platform mode is the default and is opt-out.** It is also the **gate to everything
platform-level**: the database backend, `dbtx db`, the config file and the project
guards. **Standalone mode** is the opt-out. It runs dbt with dbtx's non-database
features (reports, docs, templates) and assumes no database and no platform.

| | Platform mode (default) | Standalone mode (`--standalone-mode`) |
|---|---|---|
| Database backend | available; on unless `DB_BACKEND=disabled` | **none**: no recorder, no connection, no URI requirement |
| `dbtx db *` | available | exit 2: `database operations require platform mode` |
| Config file | `<platform_root>/dbtx.conf` (6.5.2) | **none read** |
| `DB_*` options | resolved through the three layers (6.3) | CLI `--db-*` → exit 2 (contradiction); `DBTX_DB_*` env → **ignored** (they may be set host-wide), shown by `dbtx config` as `ignored (standalone)` |
| Project guards | G1 filesystem scan + G2 database claim (6.5.4) | none |
| Project dir | relative to the platform root; must be a direct child (6.5.2) | dbt's default resolution |
| Profiles | scoped per project (6.5.6) | dbt's default lookup |

**How the mode is decided, first match wins:**

| # | Rule | Result | Example |
|---|---|---|---|
| 0 | `--standalone-mode` on the command line | standalone | `cd jaffle-shop && dbtx build --standalone-mode` |
| 1 | `--platform-dir DIR` on the command line | platform, root = `DIR` | `dbtx build --platform-dir /srv/dbtx --project-dir jaffle-shop` |
| 2 | The **current working directory** contains `dbtx.conf` and is **not itself a dbt project** (has no `dbt_project.yml`) | platform, root = cwd | `cd /srv/dbtx && dbtx build --project-dir jaffle-shop` |
| — | none of the above | **exit 2** | `cd jaffle-shop && dbtx build` |

The "none of the above" error says how to proceed:

`dbtx: no dbtx platform found (no --platform-dir, and /home/me/jaffle-shop has no dbtx.conf). Run from the platform root, pass --platform-dir <dir>, or add --standalone-mode to run without the platform.`

Contradictions also exit 2:
- `--standalone-mode` with `--platform-dir`
- `--standalone-mode` with any `--db-*` flag
- `--standalone-mode` with `--config-file`

Notes:
- **`--platform-dir` and `--standalone-mode` are CLI-only.** They have no config-file
  key, because the config file is located *from* the mode, and no env var, because
  that is what was asked for. They go through the same argv extraction as the other
  dbtx flags (6.6 step 1).
- Rule 2 excludes a directory that holds `dbt_project.yml`. A project folder is never
  a platform root, even if someone put a `dbtx.conf` in it.
- dbtx never searches parent directories for a platform. The platform is either named
  or is where you run from.
- **Rollout:** until Phase 5, the "none of the above" case is a **warning** and the
  run continues in standalone mode, under the same `ENFORCE_DB_REQUIREMENTS` constant
  as the other database-motivated failures (Phase 0). Existing `cd project && dbtx
  build` usage keeps working until the release that turns recording on. That
  release's changelog must tell single-project users to add `--standalone-mode` or
  set up a platform root.

#### 6.5.2 Locating the project and the config file

**Project dir.**
- **Standalone:** exactly as dbt resolves it: `--project-dir`, then `DBT_PROJECT_DIR`,
  then the nearest of cwd and its ancestors that contains `dbt_project.yml`. Use
  dbt's own `dbt.cli.resolvers.default_project_dir()`; do not re-implement the walk.
  dbtx adds nothing to the argv.
- **Platform:**
  - A relative `--project-dir` / `DBT_PROJECT_DIR` is resolved **against the platform
    root**, so `--project-dir jaffle-shop` means the same thing whatever the cwd is.
    dbtx passes the absolute path on to dbt, through the argv it already rebuilds
    (`argv_for`) and the pass-through argv.
  - If neither is given, the project is the nearest folder from cwd upward containing
    `dbt_project.yml` (dbt's walk), stopping at the platform root.
  - The resolved project dir **must be a direct child of the platform root**.
    Otherwise exit 2, listing the root's project folders. This includes running from
    the platform root with no `--project-dir`:

    `dbtx: /srv/dbtx is a dbtx platform; choose a project with --project-dir <folder> (jaffle-shop, jaffle-shop-2)`

**Config file (platform mode only).** Exactly **one** file is used, the first that
applies. Files are **never merged**, so the three layers in 6.3 stay three.

1. `--config-file PATH`
2. `DBTX_CONFIG_FILE`
3. `<platform_root>/dbtx.conf`

Rules:
- `--platform-dir DIR` must be an existing directory and not a dbt project. Its
  `dbtx.conf` must exist unless rule 1 or 2 names another file. A platform with no
  config is almost certainly a typo in the path, so it exits 2.
- **A `dbtx.conf` inside the project folder is an error** (exit 2, naming both files).
  A project overriding the platform's `DB_URI` would split one platform across two
  databases. If per-project settings are needed later, they get their own,
  explicitly allowed keys. Projects do not silently override the platform file.
- An explicitly named file that does not exist is an error.
- `DBTX_CONFIG_FILE` only *locates* the file. It is not an option that the file
  itself could set.
- **Standalone mode reads no config file.** `--config-file` with `--standalone-mode` is
  a contradiction (exit 2). `DBTX_CONFIG_FILE` is ignored, like the `DBTX_DB_*` vars,
  because it may be set host-wide. Revisit this when dbtx has options that are not
  platform or database options.
- `dbtx config` prints the mode and the rule that chose it, the platform root, the
  project dir, and the config file with the rule that chose it.

#### 6.5.3 File format

- `KEY=VALUE` per line. Blank lines and `#` comment lines are ignored. Surrounding
  whitespace is stripped, and a value may be wrapped in single or double quotes.
- **Not supported:** `export` prefixes, `${VAR}` interpolation and multi-line values.
  If they appear, the loader raises an error rather than doing something surprising.
- **Strict keys:** an unknown key (for example `DB_URL`) or a duplicate key is an error
  that names the file and line number. Typos fail loudly rather than silently falling
  through to a lower layer.
- The file may hold a URI with a password. Document that it must not be committed in
  that case, or that the password should come from libpq (6.2).
- A relative SQLite path resolves against the file's directory (6.6 step 7). In
  platform mode, `DB_URI=sqlite:///.dbtx/dbtx.db` is therefore one database at
  `<platform_root>/.dbtx/dbtx.db`, shared by every project. That suits one host only.
  A platform with several hosts, or several projects building at the same time,
  should use Postgres. `dbtx db status` warns when a SQLite store holds more than one
  project.

#### 6.5.4 Project-name guard

**Why it is needed.** The store keys everything on `project` =
`manifest.metadata.project_name`, which is the `name:` in `dbt_project.yml`. Two folders
declaring the same name would write into each other's rows. Worse, each sync would
mark the other's nodes as removed (5.1 step 4). The folder name is unique; the dbt
name is not. The guard enforces **one dbt project name ↔ one project folder**.

The guard runs at start-up, as part of configuration checking (6.6 step 8), under the
same gates as the URI requirement: the backend is enabled and the command uses the
database. In every other case a detected collision is only a warning, because it can
only corrupt the store.

**G1. Filesystem scan (platform mode; no database needed, so it is in Phase 0).**
- Scan the platform root's **direct child** folders that contain `dbt_project.yml`.
  Hidden folders are skipped. `dbt_packages/` is never reached, since only direct
  children are scanned.
- Read `name:` with `yaml.safe_load`. If the value contains Jinja (`{{`), render it
  with dbt's `DbtProjectYamlRenderer`, so a name taken from an `env_var()` resolves
  the way dbt would resolve it.
  - Rendering outside a dbt invocation needs
    `dbt_common.context.set_invocation_context(dict(os.environ))` first. Without it,
    `env_var()` raises `LookupError: DBT_INVOCATION_CONTEXT_VAR` (checked against
    1.12.5).
  - dbt's `preflight` resets that context on every `invoke`, so setting it here does
    not leak into the real run.
- If the **current** project's name is also declared by another folder, exit 2,
  listing every folder that declares it:

  `dbtx: project name 'jaffle_shop' is declared by 2 folders under /srv/dbtx: jaffle-shop, jaffle-shop-2. Each project on a platform needs a unique name: in dbt_project.yml.`

  The exception is G2's established owner (below).
- Collisions between *other* projects are reported as a warning only. A newly added
  folder must not stop unrelated projects from running.
- Cost: one small YAML read per project folder, before dbt starts.

**G2. Database claim (platform mode; needs the store, so Phases 1–2).**
G1 only sees one host's platform root. The database is the one place that sees every
host and every past deployment. So the store
records which folder owns each name, in a new `project` table (6.5.5).
- **First use:** the name is unclaimed. Insert `(project, folder)` in the start-up
  transaction. The insert is race-safe through the primary key: if two hosts claim at
  once, one insert fails and that run re-reads the claim.
- **Same folder:** bump `last_seen_at` and continue.
- **Different folder:** exit 2. For example:

  `dbtx: project name 'jaffle_shop' is owned by folder 'jaffle-shop' in this database (claimed 2026-10-01 by host build-01). Folder 'jaffle-shop-2' cannot use it.`

  This catches what G1 cannot: collisions across hosts, and a project renamed in
  place to a name another folder already owns.
- **Established owner wins.** When G1 finds a collision but the current folder
  already holds the G2 claim, the run continues with a warning that names the
  folder(s) that will be refused. Production keeps running when someone deploys a
  colliding folder, and the newcomer is the one that fails.
- **Legitimate moves and renames:** `dbtx db projects` lists the claims.
  `dbtx db projects rebind <name> --folder <new-folder>` moves a claim, for example
  after renaming a folder. It refuses while the old folder still exists under the
  platform root, unless `--force` is given.
- Folder names are compared exactly (case-sensitive), as stored. Different hosts
  writing to one database are expected to use the same folder name for the same
  project. Document this as a deployment rule.

**Not handled by the guard:** two folders that are *meant* to be the same project, for
example a blue/green redeploy that puts `jaffle-shop-new` next to `jaffle-shop`. Under
these rules they are different deployments: use the same folder name, or `rebind`.

#### 6.5.5 Schema: the `project` registry (spec version 2)

The spec (`orm_to_implement.yml`, now `src/dbtx/store/schema.yml`) is now **version 2** (agreed, Q10). Changes:
- **New `platform.project` entity:** `project` (PK, the dbt project name), `folder`,
  `claimed_at`, `claimed_by_host`, `last_seen_at`, with an index on `folder`.
- **FKs to `project(project)` from `invocation` and `node`.** Every other project-keyed
  table (`node_version`, `node_edge`, `node_result`, `report`, `report_render`)
  already reaches `project` through `node` or `invocation`, so the registry is
  enforced by the database and not only by dbtx.
- **`job.project`:** nullable, FK → `project`. The Phase 6 worker can tell which
  project a queued job is for. It is `NULL` for the stub job rows created from an
  external `--vars job_id` run (5.2).

Ordering consequence: the G2 claim (start-up) always inserts the `project` row before
any invocation or node row for it, so the FKs never fail in normal operation. `dbtx db
load` (post-hoc import) claims for the folder its `--project-dir` names, using the
same G2 rules.

`folder` is deliberately **not unique**. A folder whose dbt project is renamed keeps
its old claim (so its history stays attributed to it) and gains a new one. G2 warns
when this happens (`folder 'jaffle-shop' previously recorded project 'jaffle_shop_old'`).

#### 6.5.6 dbt profiles

**Rule (Q13):** in **platform mode**, profiles are scoped **per project**. In
**standalone mode**, dbt's default behaviour is untouched. dbtx **scopes where dbt
looks; it does not validate profiles**. A missing `profiles.yml`, an unknown profile or
a bad target is reported by dbt itself, in its own format.

| | Platform mode | Standalone mode |
|---|---|---|
| What dbtx passes to dbt | always `--profiles-dir <project_dir>` | nothing; dbt resolves `--profiles-dir` > `DBT_PROFILES_DIR` > cwd > `~/.dbt` |
| User's `--profiles-dir` / `DBT_PROFILES_DIR` | allowed only if it resolves to the project folder; anything else → exit 2 | used as dbt uses it |
| `--profile`, `DBT_PROFILE`, `--target`, `DBT_TARGET` | passed through untouched; dbt picks the profile and target as usual | same |

**Why dbtx has to pass it.** dbt looks for `profiles.yml` in **cwd**, then `~/.dbt`
(`dbt.cli.resolvers.default_profiles_dir`), and **never in the project folder**.
Platform runs start from the platform root (rule 2) or from anywhere (rule 1). Without
the flag, a project's `profiles.yml` would be ignored, and dbt could silently pick up
a same-named profile from `~/.dbt` that points at a different warehouse. With the flag,
dbt reads only the project's own file, so "declared per project" is enforced by dbt.

**Platform-mode details:**
- **Injection.** `--profiles-dir <abs project_dir>` is added to the argv at the top of
  `cli.main`, together with the absolute `--project-dir` (6.5.2). It is added as a
  real command-line option, so `explicit_params` sees it and `argv_for` forwards it
  to dbtx's internal `parse` and `ls` as well as the user's command. Every dbt
  command accepts `--profiles-dir` (checked against dbt 1.12.5), so injecting it can
  never cause a usage error.
- **A conflicting location is the one thing dbtx rejects.** A `--profiles-dir` or
  `DBT_PROFILES_DIR` that resolves anywhere other than the project folder exits 2.
  This is not profile validation: dbtx would otherwise have to silently override a
  setting the user made. The message names the setting:

  `dbtx: DBT_PROFILES_DIR=/home/ci/.dbt conflicts with platform mode: profiles are scoped per project (/srv/dbtx/jaffle-shop). Unset it.`

  Hosts that export `DBT_PROFILES_DIR` globally must unset it for platform runs.
  Document this in the deployment guide.

**Nothing in dbtx resolves profiles.** The recorder takes `target` and `adapter_type`
from dbt's own resolved state (5.2, 5.5), so there is no profile-name or target logic
to keep in step with dbt.

### 6.6 Validation order

All of this runs at the top of `cli.main`, in order. The first failure exits 2 with the
layer, key and reason. For example:

`dbtx: DB_URI (config file /proj/dbtx.conf:3): unsupported backend 'mysql'; supported: sqlite, postgresql`

1. **Extract dbtx options from argv:** `--db-backend`, `--db-uri`, `--db-schema`,
   `--config-file` and `--platform-dir`, in both `--opt value` and `--opt=value` forms,
   plus the boolean `--standalone-mode`. Remove them before
   anything else sees the argv, as `--no-reports` is removed today. A flag with no
   value is an error.
2. **Detect the mode, resolve the project dir, then (platform mode only) locate and
   parse the config file** (6.5.1, 6.5.2). Mode and layout errors exit 2 here:
   - no platform found and no `--standalone-mode` (warn-only until Phase 5). `--help`
     and `--version` are exempt, so help works anywhere.
   - `--standalone-mode` combined with `--platform-dir`, `--db-*` or `--config-file`
   - a bad `--platform-dir`
   - a platform with no `dbtx.conf`
   - a project that is not a direct child of the platform root, including no project
     chosen
   - a project-level `dbtx.conf` in platform mode
   - in platform mode, a `--profiles-dir` / `DBT_PROFILES_DIR` pointing outside the
     project (6.5.6). Profiles themselves are not validated.

   In platform mode, the absolute `--project-dir` and `--profiles-dir` are added to
   the argv here, before anything downstream reads it.

   **In standalone mode, steps 3–8 do not apply.** There are no database options to
   read or validate and no platform checks. `DbtxConfig` records `mode=standalone`
   and `db=None`, and the recorder is a no-op.
3. **Read env vars.** Only the `DBTX_*` names in 6.4.
4. **Resolve each option by precedence**, keeping the **source** of each value
   (`cli` / `file:<path>:<line>` / `env` / `default`) for error messages and
   `dbtx config`.
5. **Validate values:**
   - `DB_BACKEND` must be `enabled` or `disabled`.
   - `DB_URI` must parse with `sqlalchemy.engine.make_url`.
   - The backend name must be `sqlite` or `postgresql`. From Phase 2 this list comes
     from the adapter registry.
   - The driver must be importable. A `postgresql` URI without psycopg installed fails
     with `install dbtx[postgres]`.
6. **Requirement check, behind three gates.** In order:
   1. Is this platform mode? Standalone never reaches this step (step 2).
   2. Is the backend enabled? If `DB_BACKEND=disabled`, stop here: no requirement
      check, no start-up checks, and the recorder is a no-op.
   3. Does this command use the database (classes below)? If not, stop here.
   4. Is `DB_URI` set? If not, exit 2, and the error lists the ways out: set `DB_URI`
      through any layer, pass `--db-backend disabled`, or pass `--standalone-mode`.

   Command classes, in a single table in `config.py` (`command_uses_db(argv)`) with a
   test for every row:

   | Class | Commands | Steps 1–5 (validate) | Steps 6–8 (require + start-up checks) |
   |---|---|---|---|
   | **Uses the database** | `build`, `compile`, `run`, `test`, `seed`, `snapshot`, `parse`, `run-operation`, `source freshness`, `docs generate` (the recorded commands in 5.2), and `dbtx db init / upgrade / load` | yes | yes, in platform mode with the backend enabled |
   | **Reports on the database** | `dbtx db status`, `dbtx config` | yes | no. They report a missing URI, a disabled backend or standalone mode instead of failing on it. `dbtx config` still exits 2 on *invalid* values. `dbtx db status` in standalone mode exits 2 like every `dbtx db` command. |
   | **Does not touch the database** | `ls`/`list`, `debug`, `deps`, `clean`, `init`, `show`, `retry`, `clone`, `docs serve`, `docs patch / install / status`, `templates`, `--help`, `--version` | yes | no |

   The "uses the database" list is built from the same command table as the
   `InvocationCommand` enum, so recording a new command automatically makes it require
   a URI.
7. **Normalise:** a relative SQLite path is resolved against the directory of the layer
   that supplied it. For a config file that is the file's directory; for CLI and env it
   is the cwd. The resolved absolute path is what gets used and printed.
8. **Start-up checks, under the same gates as step 6 (so never in standalone mode):**
   - **Project-name guard G1** (platform mode, from Phase 0): the filesystem scan in
     6.5.4.
   - **From Phase 2 onward:** open the engine, connect, compare the Alembic revision
     with head, then run **guard G2** (the `project` claim, 6.5.4) in the same
     transaction.

   Each failure is an exit 2, before dbt runs:
   - Unreachable or authentication failure → exit 2.
   - SQLite behind head → auto-upgrade (a local file dbtx owns).
   - Postgres behind head → exit 2 with `run dbtx db upgrade`.
   - Ahead of head → exit 2.
   - Project name claimed by another folder (G2), or declared by another folder on
     this platform without an established claim (G1) → exit 2.

   `dbtx db init` and `dbtx db upgrade` skip only the revision check, since fixing it is
   their job.

The result is a frozen `DbtxConfig` dataclass, carrying `mode` (`platform` /
`standalone`), the platform root, the project dir and, in platform mode, the database
settings. `cli.main` passes it to `build`,
`compile_project`, the pass-through branch and `dbtx db`. **Nothing below `cli.main`
reads `os.environ` or the config file again.**

### 6.7 CLI surface

The CLI group is now `dbtx db` (formerly `dbtx store`), so it matches the `DB_*` option
names. The Python package stays `dbtx.store`.

| Command | Does |
|---|---|
| `dbtx config` | print the mode and every option's resolved value and source (URI masked). Exits 2 on invalid configuration, so it doubles as a config check in CI. Works in both modes. |
| `dbtx db init` | create schema (Postgres) and `alembic upgrade head` |
| `dbtx db upgrade` | `alembic upgrade head` |
| `dbtx db status` | backend enabled or disabled, masked URI and its source, server version, current vs head revision, row counts |
| `dbtx db load --run-loc target [--project-dir .]` | post-hoc import of `manifest.json` + `run_results.json` (and `sources.json` when present) for runs made with plain `dbt`. Idempotent. |

Every `dbtx db` command requires platform mode and exits 2 in standalone mode.

Help and version flags are in the "does not touch the database" class (6.6 step 6), so
help works on an unconfigured machine. Steps 1–5 still run, so a broken config file is
reported even then.

---

## 7. Phases

Each phase ends green on `pytest` for both backends (Postgres tests use a throwaway
pgserver instance, or the server given with `pytest --postgres-uri`) and is one
reviewable commit or PR.

### Phase 0: Configuration
- Add `sqlalchemy` as a dependency, for `make_url` only. No models or engines yet.
- `src/dbtx/config.py`, a top-level module rather than one inside `store/`, because
  later dbtx options will use the same loader:
  - an option registry (name, CLI flag, file key, env var, parser, default)
  - argv extraction
  - the config file parser
  - layered resolution with sources
  - validation
  - the frozen `DbtxConfig`
- `cli.main` calls the loader **before** any existing logic: `--no-reports` handling,
  `build`/`compile` dispatch, `templates`, `docs`, pass-through. It passes `DbtxConfig`
  down; `build` and `compile_project` take it as a parameter.
- `dbtx config` command.
- **Rollout flag.** The backend is enabled by default and no recording exists yet. So
  Phase 0 keeps **both** database-motivated failures warn-only until Phase 5:
  - the URI requirement (6.6 step 6)
  - G1's fatal path (6.5.4): a name collision is printed as a warning
  - "no platform found" without `--standalone-mode` (6.5.1): a warning, and the run
    continues in standalone mode

  Otherwise every existing `dbtx build` (single projects run from their own folder,
  including the live tests), and every platform project that happens to share
  a dbt name, would fail with exit 2 for a backend that does nothing yet. The
  flag is a module constant (`config.ENFORCE_DB_REQUIREMENTS = False`), patched to
  `True` in tests. It is deliberately **not** an env var or config option, so it does
  not become user-facing configuration that later has to be supported. Phase 5 flips
  the constant and then deletes it.
- **Settled details** (defaults chosen so Phase 0 is fully specified):
  - **Flag position:** dbtx flags are accepted anywhere in argv, before or after the
    command (`dbtx --platform-dir X build` and `dbtx build --platform-dir X`). The
    command is the first token left after extraction.
  - **What gets the injected `--project-dir` / `--profiles-dir`:** only argv handed to
    dbt (the user's command, the internal `parse` and `ls`, and pass-throughs).
    dbtx's own commands (`templates`, `docs patch/install/status/serve`, `config`,
    `db`) receive `DbtxConfig` instead. `dbtx templates --project-dir` uses the same
    project resolution (6.5.2), so in platform mode it resolves against the root.
  - **Unknown `DBTX_*` env vars** (for example `DBTX_DB_URL`) produce a warning, the
    env counterpart of the strict file keys. Known names: the options in 6.4,
    `DBTX_CONFIG_FILE` and `DBTX_DB_STRICT` (`DBTX_TEST_POSTGRES_URL` was dropped after
    Phase 1, when the tests began starting their own server). A warning
    rather than an error, because the environment is shared with other tools.
  - **`dbtx config` output:** human-readable, one line per item: mode, platform
    root, project dir, config file and the rule that chose it, then every option
    with value and source. A `--json` form can be added when something needs to
    parse it.
  - **Driver check without importing:** `importlib.util.find_spec("psycopg")`, so a
    validation run never imports a driver it does not use.
- **Docs and fixtures:**
  - a README "Configuration" section: modes, the three layers, options, the platform
    layout and the `DBT_PROFILES_DIR` deployment note
  - a commented `dbtx.conf.example`
  - a `platform_root` pytest fixture that copies `examples/jaffle_shop` into
    `<tmp>/jaffle-shop` and `<tmp>/jaffle-shop-2` with a root `dbtx.conf`, for the
    G1 and project-resolution tests
- **Tests (`tests/unit/test_config.py`):**
  - precedence for every pair of layers
  - the source recorded for each value
  - `--opt value` and `--opt=value` forms, and that they are stripped from the argv
    dbt sees
  - a flag with no value
  - config file: comments, quotes, unknown key, duplicate key, `export` / `${}`
    rejected, line numbers in errors
  - explicit missing file vs missing default file
  - `.env` in the project dir is **ignored**
  - invalid `DB_BACKEND`, malformed URI, unsupported backend, missing psycopg
    (simulated)
  - `disabled` skips the requirement but still rejects a malformed URI
  - gating: `disabled` + any command → no requirement; `enabled` + a non-database
    command (`deps`, `debug`, `templates`, `docs serve`) with no URI → runs;
    `enabled` + `build` with no URI → exit 2 (behind the Phase 0 test flag);
    `dbtx config` / `dbtx db status` with no URI → report it, exit 0
  - one test per row of the command-class table
  - mode detection: `--standalone-mode` first; then `--platform-dir`; then cwd with
    `dbtx.conf` and no `dbt_project.yml` → platform; a project folder with a
    `dbtx.conf` is never a platform root; no parent-directory search
  - no platform and no `--standalone-mode` → exit 2 with the guidance message
    (rollout on), or warning + standalone (rollout off); `--help` / `--version` work
    with no platform
  - project dir: a relative `--project-dir` is resolved against the platform root
    from any cwd, and the absolute path reaches dbt (assert on the argv passed to
    `dbtRunner`); not a direct child → exit 2; platform root with no project → exit 2
    listing folders; standalone uses dbt's `default_project_dir()` walk
  - config file selection (platform): `--config-file` > `DBTX_CONFIG_FILE` > root
    `dbtx.conf`; one file only, no merge
  - layout errors: a project `dbtx.conf` in platform mode; `--platform-dir` missing,
    a file, a dbt project, or with no `dbtx.conf`
  - `--standalone-mode` gates everything platform-level:
    - no config file is read (neither the root's nor the project's), no G1, no
      `--profiles-dir` injected, a relative `--project-dir` resolves against cwd
    - `DbtxConfig.db is None` and the recorder is the no-op (assert no engine is
      created and no store module is imported)
    - with `--platform-dir`, any `--db-*` flag, or `--config-file` → exit 2
    - `DBTX_DB_URI`, `DBTX_DB_BACKEND` and `DBTX_CONFIG_FILE` in the env → ignored,
      and `dbtx config` lists them as `ignored (standalone)`
    - every `dbtx db` subcommand → exit 2 (`requires platform mode`)
    - `dbtx config` works and reports `mode: standalone`
  - flag position: dbtx flags before and after the command are both extracted
  - unknown `DBTX_*` env var → warning; known ones are silent
  - rollout flag: with `ENFORCE_DB_REQUIREMENTS = False`, a missing URI and a G1
    collision only warn, and the exit code is dbt's
  - profiles (6.5.6), platform mode:
    - `--profiles-dir <project_dir>` reaches every `dbtRunner` call, including the
      internal `parse` and `ls`
    - an explicit `--profiles-dir` / `DBT_PROFILES_DIR` pointing elsewhere → exit 2;
      pointing at the project folder → accepted
    - `--profile` / `--target` / `DBT_PROFILE` / `DBT_TARGET` reach dbt unchanged
    - a project with no `profiles.yml` → no dbtx error; dbt's own error and exit
      code come through
    - a `~/.dbt/profiles.yml` with the same profile name is never used (fake `HOME`)
  - profiles, standalone mode: argv and env are passed through untouched (assert that
    no `--profiles-dir` is added)
  - **guard G1** on a temp platform root holding `jaffle-shop/` and `jaffle-shop-2/`
    with the same `name:` (`ENFORCE_DB_REQUIREMENTS` patched on): both exit 2 for a
    database command (backend enabled);
    an unrelated third project only warns; `--db-backend disabled` or a non-database
    command only warns; hidden folders and nested `dbt_packages/` are ignored; a
    Jinja `name:` is rendered
  - relative SQLite path resolution per layer
  - masked password in every message
  - **ordering:** a bad config exits 2 without calling `dbtRunner` (assert with a
    mock), even when the dbt args are invalid too
  - `--help` works with no configuration

**Done when:** `dbtx config` prints resolved values with sources, and every existing
test passes unchanged.

**Status: implemented (2026-10-10), uncommitted.** Progress and details are in
[phase0_state.md](phase0_state.md). Deviations from this section, all recorded there:
- config errors and warnings are printed to **stderr**, so they never corrupt output
  another tool parses (`dbtx ls --output json`)
- project-less commands (`config`, `init`, `docs serve/patch/install/status`,
  `db init/upgrade/status`) need no project dir in platform mode, so they work from
  the platform root
- platform mode rejects a command line that starts with an option (`dbtx --debug
  build`), because the project and profiles dir cannot be scoped without the command
- `DB_URI` accepts only the `pysqlite` and `psycopg` (3) drivers. SQLAlchemy resolved
  to 2.1.4, where a bare `postgresql://` already means psycopg 3
- `--db-schema` is deferred to Phase 2 with `DB_SCHEMA`
- `dbtx db` is a stub (exit 2) until its phases

### Phase 1: Foundations
- The promoted `schema.yml` is spec **version 2** (6.5.5), so `models/` gains a
  `platform.py` (`Project`). The parity test covers it like every other table.
- Add the remaining dependencies. Promote the YAML to `src/dbtx/store/schema.yml` and include it as
  package data in the hatch wheel config.
- `types.py`, `enums.py`, `models/*`, `models/base.py` naming convention.
- **Tests:** `test_store_schema_parity.py` (YAML ↔ metadata: tables, columns, types,
  nullability, PK, FKs, indexes, CHECK value sets ↔ enums), `test_store_types.py`
  (UTC round-trip, naive rejection, non-UTC conversion, SQLite ordering of stored
  timestamps, JSON `None` → SQL NULL).

**Done when:** `Base.metadata.create_all()` succeeds on SQLite and Postgres and the
parity test passes.

**Status: implemented (2026-10-10), uncommitted.** Progress and details are in
[phase1_state.md](phase1_state.md). Deviations from this section:
- the spec was **moved** (`git mv`), not copied, to `src/dbtx/store/schema.yml` (D3), so
  there is one copy; links in this plan point at the new path
- no hatch config change: the wheel already ships every file under `src/dbtx`
  (checked with `uv build --wheel`)
- `validation.py` and the autouse `DBTX_DB_STRICT=1` (D9, section 8) are deferred to
  Phase 4, where the loaders first write app-enforced values; Phase 1 has the value
  set (`enums.NODE_RESULT_STATUSES`) only
- the store tests use a plain-SQLAlchemy `engine` fixture (adapters come in Phase 2);
  on Postgres it creates a schema **per test**, not per session, so tests stay
  independent, and sets a non-UTC session time zone to prove UTC normalisation
- `Node.parents` / `Node.children` return `Node` objects through `node_edge` (joined on
  `project` as well), including edges marked removed

### Phase 2: Adapters
- `adapters/base.py`, `sqlite.py`, `postgres.py`, registry, `open_store(DbtxConfig)`.
- **Guard G2:** the claim in the start-up transaction, the
  "established owner wins" exception to G1, `dbtx db projects` and `rebind`. Tests:
  first claim; same folder; other folder → exit 2; two concurrent first claims (one
  wins, both backends); G1 collision while holding the claim → warn and run;
  `rebind` refused while the old folder exists.
- The supported-backend check in `config.py` switches to the registry. Add the
  `DB_SCHEMA` option and the start-up checks (6.6 step 8).
- **Tests:** FK enforcement and CHECK rejection on both backends; upsert merge rules
  (`least`/`greatest`/`clear`); concurrent writers on a file-backed SQLite store
  (8 threads, no `database is locked`); `claim_next_job` with 2 concurrent claimers
  never handing out the same job (both backends); version-floor errors.

**Done when:** a single parametrised `store` fixture (`sqlite-memory`, `sqlite-file`,
`postgres`) runs every adapter test.

**Status: implemented (2026-10-11), uncommitted.** Progress and details are in
[phase2_state.md](phase2_state.md). Deviations from this section:
- the database start-up checks (connect, version floor, tables, G2) are complete and
  tested, but for dbt commands they run only once `ENFORCE_DB_REQUIREMENTS` is on
  (Phase 5): nothing is recorded before then, and nothing creates the tables before
  Phase 3, so connecting on every `dbtx build` would only add failures. `dbtx db`
  commands always use the database
- until migrations (Phase 3) the "schema version" check is "the `project` table
  exists", failing with `run dbtx db init`
- G1 for the current project is decided at start-up together with G2 ("established
  owner wins"), not in `load_config`; `DbtxConfig` gained `project_name` and
  `colliding_folders`
- `rebind` also refuses a new folder that is not a project folder on the platform
  (unless `--force`), and the G2 refusal names the `rebind` command
- `upsert` refuses `least` / `greatest` on nullable columns (NULL handling differs per
  backend), duplicate keys within one call, and keys other than the primary key
- `DB_SCHEMA` names are limited to `[a-z_][a-z0-9_]{0,62}`; driver rules moved onto the
  adapter classes so plugins get the same checks
- the Phase 1 `engine` fixture stays for the type and model tests (they must hold on a
  bare connection); every adapter test uses `store`
- the registry is keyed by **adapter name**, not by backend: several adapters may serve
  one dialect and driver. An adapter declares its `dialect` and `driver`(s); the
  default for a URI's (backend, driver) is the built-in when it supports the pair, else
  the one plugin that does. A new option, `DB_ADAPTER` (`--db-adapter`, `DB_ADAPTER=`,
  `DBTX_DB_ADAPTER`), names another adapter explicitly. Each adapter chooses its JSON
  storage type (`json_type()`); `open_store` attaches the adapter to the engine's
  dialect for `JSONType`. See `docs/contributing/architecture/adapters.md`

### Phase 3: Migrations
- Alembic env (programmatic config, `render_as_batch` for SQLite, `include_schemas`
  and `version_table_schema` for Postgres), `0001_initial` autogenerated then
  hand-reviewed.
- `dbtx db init|upgrade|status`.
- **Tests:** `upgrade head` on an empty database followed by
  `alembic.autogenerate.compare_metadata` → no diff, on both backends. Downgrade to base
  works.

**Done when:** `create_all` is used only in unit-test fixtures; everything else goes
through migrations.

### Phase 4: Loaders and repositories
- `loaders/manifest.py`, `loaders/run_results.py`, `repositories/*`.
- `dbtx db load` (the post-hoc path), which exercises everything without touching the
  runner.
- **Tests** using `tests/fixtures/manifest.json`: node counts per resource type,
  excluded types absent, edge FK filtering, removal marking (sync, drop a node, sync →
  `removed_at` set; re-add → cleared), checksum revert bumping the old
  `node_version`, out-of-order loads, idempotent reloads of run_results, the
  sources.json shape (including a `runtime error` entry), secret vars absent from
  stored `args`, app-enforced status (warn in lenient mode, raise in strict mode),
  the `which` → `command` table, job state transitions.

**Done when:** loading the example project's artifacts twice produces identical row
counts and no errors.

### Phase 5: Runner integration
- `recorder.py`; `ReportScheduler(on_change=...)`; wiring in `runner.build`,
  `runner.compile_project` and the pass-through branch.
- Turn on the `DB_URI` requirement (6.6 step 6) that Phase 0 left behind a test flag.
  This is the release where `dbtx build` with no configuration starts exiting 2.
  Changelog and README must say so, and must point at `--db-backend disabled`.
- Flip `ENFORCE_DB_REQUIREMENTS` on (then delete it). The existing live tests
  run from inside the project folder, so `Project.dbtx` passes `--standalone-mode`;
  that also proves standalone runs never touch a database. The new store tests use
  the platform fixture.
- **Tests:** unit tests on the scheduler observer (fires for every transition, outside
  the lock); a recorder whose store raises leaves `build`'s exit code unchanged (D5);
  live test (`@pytest.mark.live`): `dbtx build` on a **platform fixture**
  (`examples/jaffle_shop` copied under a tmp platform root whose `dbtx.conf` sets
  `DB_URI=sqlite:///.dbtx/dbtx.db`) → one invocation (`result=success`), node
  results for every built node, three `report_render` rows `rendered` with `paths`, and
  `report` rows matching `_exposures.yml`. Also assert:
  - the stored `invocation_id` equals the one in `target/run_results.json` (not the
    internal parse's, which is in `manifest.json` metadata)
  - `target` is set without passing `--target`
  - `manifest_hash` matches `target/manifest.json` as it is after the build

  A second run with a failing test → skipped renders with `detail`.
- **Clean-environment tests** (5.5), each on a fresh copy of the example project:
  - `--no-partial-parse` twice → the second sync adds no `node_version` rows and
    sets no `removed_at`
  - first run with no `target/` → full node set, nothing removed
  - break a YAML file → one invocation with `result=failure`, `error_message` set,
    `project` taken from `dbt_project.yml`, no node or report rows touched
  - `--no-write-json` → full rows, `manifest_hash` `NULL`
- Update `README.md` with a "Run history store" section.

**Done when:** `dbtx build` with a store configured matches the existing console output
exactly, and the database holds the run.

### Phase 6 (follow-up, separate plan): Consumers
Not part of this delivery, listed so the schema choices are checked against them:
- `dbtx docs patch --from-store`: build the `dbtx_runtime.json` history from
  `node_result` (index `(project, unique_id, completed_at)`) instead of accumulating it
  in the sidecar.
- A job worker that drains the queue with `claim_next`, runs `dbtx build --vars job_id:…`
  and finishes the job from the invocation's result.
- Docs server endpoints for recent activity (`(project, start_time)`) and latest render
  per report (`(project, exposure_id, completed_at)`).

---

## 8. Test infrastructure

- `tests/unit/store/conftest.py` (see `docs/contributing/tests.md`): a `store` fixture parametrised over backends. Postgres is
  the server given with `pytest --postgres-uri URI`, or else a throwaway server from
  the `pixeltable-pgserver` dev dependency, started once per test session (added after
  Phase 1; the command-line option replaced the `DBTX_TEST_POSTGRES_URL` variable). Each test creates and drops a uniquely named schema, so runs can share one
  server. Marker `postgres` ("needs a real
  database") is added to `pyproject.toml` alongside `live` ("runs real dbt or a real
  server process") and `server`. They are independent: store tests are unit tests
  marked `postgres`, not `live`, and `-m "not live and not postgres"` is the fastest run.
- Strict mode (`DBTX_DB_STRICT=1`) is set autouse for the whole suite.
- Frozen `run_results.json` and `sources.json` fixtures next to
  `tests/fixtures/manifest.json`, produced by a small extension to
  `scripts/freeze_manifest.py`, so loader tests don't need dbt to run. The example
  project has no source freshness config, so the `sources.json` fixture is
  hand-trimmed to a passing entry and a `runtime error` entry.
- CI:
  - a job running `pytest -m "not live"` (the Postgres tests start their own server, or
    use a service container through `--postgres-uri`)
  - a Python matrix of 3.10 and 3.14 (the D10 floor and the dev venv)

---

## 9. Open questions (defaults assumed above; confirm before Phase 5)

| # | Question | Assumed default |
|---|---|---|
| Q1 | ~~On by default or opt-in?~~ | **Resolved:** opt-out (`DB_BACKEND=enabled` by default) with a required `DB_URI` and no built-in default URI (section 6). |
| Q2 | Should `dbtx compile` record an invocation? | Yes (`compile` is in the command list), but it has no node results except compiled nodes, and no renders. |
| Q3 | Should pass-through commands outside the CHECK list (`ls`, `debug`, `show`, `retry`, `clone`) be recorded? | No. Adding them is a migration that widens the CHECK. |
| Q4 | `job.trigger` is a free string. Should it be a closed set? | Free string. Conventional values: `manual`, `schedule`, `api`, `external`. |
| Q5 | `invocation.args` stores `vars`, and vars can hold secrets. | Store what dbt's artifact serialiser produces. It already scrubs values of `DBT_ENV_SECRET_*` vars, and the recorder applies the same scrub for commands without a `RunExecutionResult` (5.2). Any other secret passed as a plain var is stored, exactly as dbt writes it to `run_results.json`. Document this. |
| Q6 | Should `node_result.project` be the root project even for package nodes (`test.dbt_utils.*`)? | Yes. The FK is `(project, unique_id)` and `unique_id` already includes the package name. |
| Q7 | ~~Which commands need a URI?~~ | **Resolved:** only commands that use or change the database, and only when the backend is enabled. The two gates and the command classes are in 6.6 step 6. Every command still validates its configuration. |
| Q8 | ~~Config file name and format?~~ | **Resolved:** `dbtx.conf`, `KEY=VALUE`, at the platform root. Standalone mode reads no config file. One file at a time, never merged (6.5). |
| Q9 | ~~Project identity?~~ | **Resolved:** the dbt project name, kept unique per database by guards G1 + G2 (6.5.4). |
| Q10 | ~~Spec v2 `project` registry?~~ | **Resolved:** `orm_to_implement.yml` edited to version 2 (6.5.5). |
| Q11 | ~~Platform detection?~~ | **Resolved:** `--platform-dir`, or else the cwd if it holds `dbtx.conf` and is not a dbt project (6.5.1). |
| Q13 | ~~Profiles in platform mode?~~ | **Resolved:** platform mode always scopes profiles per project (`<project_dir>/profiles.yml`, required, injected as `--profiles-dir`, conflicting settings rejected). Standalone keeps dbt's default behaviour (6.5.6). |
| Q12 | dbt Mesh: should cross-project refs between projects on one platform be stored? | Not in v1. An edge whose parent belongs to another project fails the `(project, parent_id)` FK and is dropped (5.1 step 3). A `parent_project` column could be added later. |

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| SQLite write contention from dbt threads during report renders | WAL + `BEGIN IMMEDIATE` + `busy_timeout`. Writes are tiny. Covered by the Phase 2 concurrency test. |
| dbt internals used by the recorder change between minor versions: `MainReportVersion` being first, `ConcurrencyLine.target_name`, `get_invocation_started_at`, the artifact serialisers, and runnable tasks rewriting `manifest.json` at the end | All of them are isolated in `recorder.py` / `loaders/run_results.py`, checked against 1.12.5. The Phase 5 live test asserts each one, so a dbt bump that breaks one fails CI rather than silently storing wrong data. |
| Large manifests make every build slower | Chunked set-based upserts, run once at the end, not per event. The `manifest_hash` short-circuit was dropped (5.1 step 5). If profiling shows a problem, add a content hash that ignores `metadata`. |
| Schema drift between YAML, models and migrations | Parity test (YAML ↔ models) plus the migration test (migrations ↔ models). |

---

## 11. Change log (dbt 1.12.5 checks and review feedback)

| Topic | Before | After |
|---|---|---|
| Invocation id | "Verify whether dbt accepts a preset id" | It does not (reset in `preflight`). The id is captured lazily on `MainReportVersion`. Start time comes from `get_invocation_started_at()`. |
| Target name | "Verify the hook" | `ConcurrencyLine.target_name`. Profile fallback for `parse` / `run-operation`. |
| Result serialiser | "Verify `to_dict()` / `write()`" | `RunResultsArtifact.from_execution_results(...).to_dict(omit_none=False)`, which also scrubs secrets. |
| Source freshness | Assumed to be in `run_results.json` | Separate `FreshnessResult` / `sources.json` shape, with its own column mapping and args capture. |
| `command` mapping | Free-form mapping | Explicit argv / `which` table. `which` is the leaf name (`freshness`, `generate`). |
| `manifest_hash` | Hash of the file written by "this invocation"; used for a sync short-circuit | Hash taken **after** the invocation: runnable tasks rewrite `manifest.json` at the end even when handed a manifest (confirmed by probe). The short-circuit is dropped because the bytes change on every parse. |
| Failed parse | Not covered | The fallback invoke in `prepare()` is recorded as a failed invocation, with project, adapter type and target resolved without a manifest. No syncs and no removals run (5.5). |
| Q5 (secrets in args) | Store verbatim | dbt's scrub of `DBT_ENV_SECRET_*` is inherited. |
| Python / SQLite | 3.10 floor, SQLAlchemy ≥2.0.30 | Dev venv is 3.14, so SQLAlchemy ≥2.0.44 (confirm on install). CI matrix 3.10 + 3.14. Bundled SQLite 3.53. |
| Configuration | `--store-url` > `DBTX_STORE_URL` > `vars` in `dbt_project.yml`; opt-in | **Phase 0**, done first: CLI > `dbtx.conf` > env, no `.env` loading. `DB_BACKEND` is opt-out and `DB_URI` is required when enabled. Validated before anything else runs. CLI group renamed `dbtx db`. |
| D5 | The store never fails a run | Start-up problems exit 2 before dbt runs; only writes after dbt starts are best-effort. |
| Command gating | Requirement applied to "commands that use the backend" | Two gates (backend enabled, then command class) and an explicit three-class command table (6.6 step 6). |
| Multi-project | Not considered | **Platform layout:** projects are direct child folders of a platform root that holds the shared `dbtx.conf`. Platform vs standalone mode (6.5.1–6.5.2). **Project-name guard:** a filesystem scan (G1) plus a database claim (G2), so one dbt name maps to one folder, with the established owner winning (6.5.4). Proposed spec v2 `project` registry (6.5.5). |
| Platform root | Detected from a `dbtx.conf` in the project's parent | `--platform-dir`, or else a cwd holding `dbtx.conf` that is not a dbt project. A relative `--project-dir` is resolved against the root. Profiles are scoped per project in platform mode; standalone keeps dbt's default (Q13, 6.5.6). |
| Spec | Version 1 | Version 2: `platform.project` registry, FKs from `invocation`/`node`, `job.project`. |
| Project dir default | "then cwd" | dbt's own walk up from cwd (`default_project_dir`), bounded by the platform root in platform mode. |
| Profile validation | A start-up check on `profiles.yml` and the running profile, with a resolver re-implementing dbt's profile/target choice | **Removed.** dbtx only scopes `--profiles-dir` in platform mode and rejects a conflicting location; dbt reports profile problems itself. The `target` / `adapter_type` fallback reads the registered adapter's resolved config (probe-checked for `parse` and `run-operation`). |
| Modes | Standalone was the implicit fallback and still allowed a database (with G2) | **Platform mode is the default (opt-out) and the only gate to the database.** `--standalone-mode` turns off the database, config file, G1/G2 and `dbtx db`. CLI `--db-*` / `--config-file` with it → exit 2; `DBTX_DB_*` / `DBTX_CONFIG_FILE` env → ignored. No platform and no `--standalone-mode` → exit 2 (warn-only until Phase 5). D11. |
