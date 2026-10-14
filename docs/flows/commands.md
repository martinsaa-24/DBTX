# What each command does, and does not do

Command by command, in platform mode and standalone mode. Which options exist is in
[usage.md](usage.md); the order of the steps every run goes through is in
[execution.md](execution.md); the docs site in [docs_refresh.md](docs_refresh.md).

`examples/jaffle_shop` has something for every command here: models, seeds and tests,
a source with a freshness rule, a snapshot (`stores_snapshot`) and a macro
(`dbtx run-operation log_store_count`).

Terms used below:

- **Run folder:** `<LOG_ROOT_LOCATION>/<project>/<YYYY-MM-DD>/<HH>/<job id>/` (UTC). dbt
  writes the run's artifacts and its log there (dbtx hands it `--target-path` and
  `--log-path`), and the project's warm partial-parse cache is copied in first.
- **Recorded:** the run is stored in the run history database (the invocation, a result
  per node, and as noted the project's nodes and reports).
- **Docs site:** the project's own target folder (`<project>/target/` by default), which
  runs no longer write to. **Patched** means dbtx copies the run's rendered `reports/`
  there and merges the run into the site's run history (`dbtx docs patch`, run
  internally).

## Summary (platform mode)

| Command | Run folder | Recorded | Nodes / reports stored | Docs site afterwards |
|---|---|---|---|---|
| `dbtx build` | yes | yes | nodes and reports (renders too) | reports copied, **patched** |
| `dbtx run`, `test`, `seed`, `snapshot` | yes | yes | nodes (from the manifest this run wrote) | reports copied if any, **patched** |
| `dbtx compile` | yes | yes | nodes and reports (no renders) | not patched |
| `dbtx parse` | yes | yes | nodes | not patched |
| `dbtx run-operation` | yes | yes | nodes (the operation itself has no stored node) | not patched |
| `dbtx source freshness` | yes | yes | nodes; results as `sources.json` | not patched (no `run_results.json`) |
| `dbtx docs generate` | yes | yes (no node results) | nodes | its `index.html`, `manifest.json`, `catalog.json` copied in; not patched |
| `dbtx warm-cache` | no | no | — | writes only the cache, `partial_parse.msgpack` |
| `dbtx ls`, `debug`, `deps`, `show`, `clone`, `init` | no | no | — | untouched |
| `dbtx clean` | no | no | — | **deleted** with the rest of `target/` (see below) |
| `dbtx retry` | no | no | — | untouched; finds no previous run (see below) |
| `dbtx docs install`, `status`, `serve` | no | no | — | read (install: adds the overlay); `--docs-loc` defaults to it |
| `dbtx docs patch` | no | no | — | patched with the `--run-loc` you name (explicit options) |
| `dbtx db init`, `upgrade` | no | no | — | the only commands that change the database's schema |
| `dbtx db status`, `projects`, `load`, `config`, `templates` | no | no | `db load`: nodes, reports and runs from artifacts | untouched |

In **standalone mode** there are no run folders, no recording, no database and no
automatic patching: every dbt command behaves as plain dbt (outputs in the project's
`target/` and `logs/`), and `dbtx build` / `compile` add their reports. The docs
commands need `--docs-loc` (and `docs patch` `--run-loc`).

Every recorded command, in platform mode, first passes the configuration and start-up
checks (configuration, database connection, schema revision, project-name claim) and
only then gets its run folder: a failed check exits 2 before dbt starts.

## `dbtx build`

**Does:** parses the project once (internal `dbt parse`, using the copied cache),
validates every report, resolves the selection (internal `dbt ls`; neither internal
call is recorded), then runs `dbt build` with the parsed manifest. Reports render as
soon as their exposure and gate tests pass, into the run folder's `reports/`. Records
the invocation, a result per node (with its compiled SQL and relation), the project's
nodes, versions and edges, its reports, and each selected report's render (`rendered`
with files, or `skipped` / `error` with the reason). Afterwards copies the rendered
reports into the docs site and patches it.

**Does not:** write anything to the project's `target/` except, after the run, the
reports and the patched run history; change the exit code because of recording or
patching (problems are warnings); patch when there is no docs site yet (a note says to
run `dbtx docs generate` then `dbtx docs install`) or when the run wrote no
`run_results.json` (`--no-write-json`).

**Fails the parse (broken YAML, no profile):** dbtx runs your command so dbt reports
the error itself; that run is recorded as a `failure` with dbt's message, and no node
or report in the database changes. Exit code: dbt's.

## `dbtx run`, `test`, `seed`, `snapshot`

**Does:** passes the command to dbt as typed, with the run folder's paths and the job
id. dbt parses for itself (using the copied cache). Records the invocation and a
result per node; stores the project's nodes from the manifest this run wrote (none
when the parse failed). Afterwards copies any rendered reports (normally none: only
`build` renders) and patches the docs site, whatever dbt's exit code.

**Does not:** render reports (only `dbtx build` does); store reports (they need dbtx's
templates; `build` and `compile` store them); patch without a docs site or without
`run_results.json`.

## `dbtx compile`

**Does:** like `build` up to dbt's compile: parses, validates every report, compiles
the selected nodes, writes the selected reports' SQL under the run folder's
`compiled/`. Records the invocation, the nodes and the reports (no renders). With
`--inline` or `--no-reports` it is a plain recorded `dbt compile`.

**Does not:** run report queries, render reports, or patch the docs site.

## `dbtx parse`, `run-operation`, `source freshness`

**Do:** run in a run folder and are recorded. `parse` stores the nodes from the
manifest it returns; `run-operation` and `source freshness` store the nodes from the
manifest they wrote. `source freshness` stores one result per source (dbt's
`sources.json` shape).

**Do not:** patch the docs site (nothing they produce is a `run_results.json` of
executed nodes the overlay shows). `run-operation` stores no node result: an operation
is not a stored node, and dbt hands it back as an already-written artifact rather than
as results, so its invocation is `success`, or `failure` when it fails (with dbt's
error message when dbt raises one, for example an unknown macro).

## `dbtx docs generate`

**Does:** runs in a run folder and is recorded (no node results: dbt returns a
catalog). When dbt succeeds, copies `index.html`, `manifest.json` and `catalog.json`
into the docs site (each file whole, so a served site never sees half a file) and
prints a reminder.

**Does not:** keep dbtx's overlay: the new `index.html` is dbt's own. **Run `dbtx docs
install` afterwards** (the next patching run also restores it). Does not patch the run
history: the sidecar (`dbtx_runtime.json`) is never touched, so no history is lost.

## `dbtx warm-cache`

**Does:** runs `dbt parse --no-write-json` in the project's own target folder: writes
only `partial_parse.msgpack` (and dbt's `perf_info.json`). Every later run copies it
into its run folder. Run it after deploying or merging code, with the `--target` the
runs use.

**Does not:** run anything, record anything, need the database, create a run folder,
or touch the docs site's `manifest.json`. Platform mode only.

## `dbtx clean`

**Does:** plain `dbt clean`: deletes dbt's `clean-targets`, by default the target path,
which in platform mode is the **docs site** (with its run-history sidecar,
`dbtx_runtime.json`) and the **warm cache**.

**Does not:** touch run folders (they live under `LOG_ROOT_LOCATION`, outside the
project) or the run history database. After a clean: `dbtx warm-cache`, `dbtx docs
generate`, `dbtx docs install`; the docs history restarts from the next run.

## `dbtx retry`

**Does:** plain `dbt retry`, not recorded, no run folder. dbt looks for the previous
run's results in the target path, which in platform mode is the project's target
folder, where runs no longer write: dbt reports that it found no previous run.

**Does not:** find a platform run to retry. Finding a past run's folder is on the
backlog ([backlog.md](../agentic/backlog.md)). Pointing `--target-path` at a run folder
would make dbt read it and also write its own results over it, so it is not a
workaround.

## `dbtx ls`, `debug`, `deps`, `show`, `clone`, `init`

Plain dbt (in platform mode with the project and its own `profiles.yml`). Not recorded,
no run folder, no job id, no database connection; the docs site is untouched.

## `dbtx docs install`, `status`, `serve`, `patch`

- `install`: adds dbtx's overlay to the site's `index.html` (`--uninstall` removes it).
- `status`: coverage, staleness and the last patch, read from the site.
- `serve`: serves the site (needs the `serve` extra).
- `patch`: merges one run (`--run-loc`) into a site (`--docs-loc`); always explicit,
  in both modes. Platform runs call it for you.

In platform mode `install`, `status` and `serve` default `--docs-loc` to the project's
docs site (name the project with `--project-dir`, or run from its folder). None of
them record anything.

## `dbtx db ...`, `config`, `templates`

- `db init` / `db upgrade`: the only commands that create or migrate dbtx's tables
  (also for SQLite, whose file they also create). Any other command on a SQLite file
  that does not exist yet, a database without tables, or one behind this dbtx version
  exits 2 and names the one to run, without creating the file.
- `db status`: reports only; creates nothing.
- `db load`: imports a plain dbt run's artifacts (`manifest.json`, `run_results.json` /
  `sources.json`); the run folder is not recorded for these.
- `db projects`: lists or moves project-name claims.
- `config`, `templates`: report only.
