# dbtx

A unified data-platform around dbt. `dbtx build` runs `dbt build` **and** provides a suite 
of other functionality leveraging DBT's parsed manifest data:
* **Report Rendering**:  
  configuration-driven reports in the same invocation. Each report is produced the moment
  its data is ready, not in a separate post-build step.
  ```console
  $ dbtx build
  ...
  19:45:42  34 of 42 NO-OP exposure top_customers ............................ [NO-OP in 0.00s]
  19:45:42  dbtx: rendered report top_customers (25 rows)
  ...
  dbtx: 3 report(s)
    RENDERED  jaffle_mix  target/reports/jaffle_mix.csv
    RENDERED  store_revenue_monthly  target/reports/store_revenue_monthly.html, target/reports/store_revenue_monthly.csv
    RENDERED  top_customers  target/reports/top_customers.html
  ```
* **Docs Run Data**:  
  run status, timings, job id and recent history overlaid onto the docs dbt already
  generates. Partial runs accumulate, so the docs show the latest known state of every node rather
  than only the last run's slice.
  ```console
  $ dbtx docs patch --docs-loc docs --run-loc target
  dbtx docs: patched 3
    job 123v1
    carried forward 3 node(s) from previous runs
    history limit 3, wrote docs/dbtx_runtime.json
  ```
* **Run History**:  
  every run of a platform project recorded in a database (SQLite or Postgres): each
  invocation, the result of every node, the project's nodes over time, and every report
  render. See [Run history store](#run-history-store).
* **Docs Server**:  
  the generated docs served over uvicorn, concurrently and without `chdir`-ing the process,
  with the cache validators dbt's single-threaded static server leaves out.
  ```console
  $ dbtx docs serve --docs-loc target
  dbtx docs: serving /home/you/proj/target
    http://127.0.0.1:8080  (ctrl-c to quit)
  ```

## How it works

### Report Rendering
- Reports are declared as dbt **exposures**, so dbt validates their `ref()`s, shows them in
  lineage and docs, and selects them with ordinary syntax (`-s +exposure:top_customers`).
- `dbtx` runs dbt in-process through `dbtRunner` and listens to its node events. In
  `dbt build`, exposures are DAG nodes. A report renders when:
  1. its exposure node finished (dbt only runs it after every upstream node succeeded), and
  2. every data test **in this run** on the exposure's direct parents passed or warned.
     dbt runs exposures concurrently with those tests, so dbtx waits for them itself.
- Queries go through the adapter dbt already opened for the run, so reports use the same
  profile, credentials and connection handling as the models.
- `dbtx compile` runs `dbt compile`, validates every report definition and template, and
  writes the SQL of the selected reports. It never queries or renders.
- Every other command (`dbtx run`, `dbtx test`, `dbtx docs generate`, ...) is passed
  straight through to dbt.

| Situation | Report outcome | `dbtx` exit code |
|---|---|---|
| Upstream builds and tests pass (or warn) | rendered | 0 |
| Upstream model fails, or a test on a parent fails | skipped | 1 (dbt failed) |
| Exposure not selected (e.g. `-s customers`) | not considered | dbt's |
| Report query fails (bad column, ...) | error | 1 |
| Invalid `dbtx.report` config, unknown template, template conflict or template syntax error | nothing runs (`build` and `compile`) | 2 |

### Declaring a report

```yaml
exposures:
  - name: top_customers
    label: Top 25 Customers            # report title
    type: analysis
    depends_on:
      - ref('customers')
    owner:
      name: Growth
    config:
      meta:
        dbtx:
          report:
            model: customers           # required only if depends_on has several models
            columns: [customer_name, order_count, lifetime_spend]   # default: *
            where: customer_type = 'returning'
            order_by: lifetime_spend desc
            limit: 25
            formats: [html, csv]       # default: [html]
            template: table            # required for html output; see "Templates"
            # template_path: report_templates/one_off.html   # exact file; wins over `template`
            template_params: {}        # free-form options, available to the template as `params`
```

Top-level `meta:` works too. Unknown keys are rejected. Heavy transformations belong in
dbt models, where they are tested and versioned. The report only selects, filters and
renders.

### Output files

Report SQL follows dbt's own `target/` layout, so it sits where dbt users already look:

| File | `dbtx compile` | `dbtx build` |
|---|---|---|
| `target/compiled/<project>/reports/<exposure>.sql` | written for each selected report | written when the report starts executing |
| `target/run/<project>/reports/<exposure>.sql` | never | written right before the query is sent |
| `target/reports/<exposure>.<format>` | never | rendered html / csv |

As with dbt nodes, a report that is skipped (upstream failure or failing test) gets no
SQL files, and a report whose query fails keeps its `run` file for debugging. SQL stays out
of `target/reports/`, so that folder can be published as-is.

In platform mode `target/` here means the run's own folder (see
[Run folders](#run-folders)); the rendered reports are also copied into the project's
docs site, `<project>/target/reports/`.

### Report Templates

HTML output always names its template. There is no default. A template is a Jinja file
called `<name>_template.html.j2` or `<name>_template.html`, and `template: <name>` selects
it. Templates come from:

| Source | Where | Precedence |
|---|---|---|
| exposure | `template_path:` on one report (relative to the dbt project) | highest; wins over `template:` |
| project | folders listed under `vars.dbtx.template_paths` in `dbt_project.yml` | replaces an integrated template of the same name |
| integrated | shipped with dbtx (`src/dbtx/templates/`): `table` | lowest |

```yaml
# dbt_project.yml. dbt reserves `vars` for arbitrary settings; custom top-level keys
# trigger deprecation warnings on several adapters.
vars:
  dbtx:
    template_paths: [report_templates, shared/templates]
```

Project and integrated templates are resolved once at start-up, before dbt runs, into a
`TemplateRegistry` of `(name, source, uri)`. The run stops with exit code 2 if:
- the same name appears in two project folders, or twice in one folder (`.html` and `.html.j2`)
- a listed folder does not exist
- a report names an unknown template, or its template does not compile

Only files directly inside each folder are considered. Subfolders are not searched.

```console
$ dbtx templates
NAME         SOURCE      URI
finance      project     file:///.../jaffle_shop/report_templates/finance_template.html.j2
jaffle_base  project     file:///.../jaffle_shop/report_templates/jaffle_base_template.html.j2
leaderboard  project     file:///.../jaffle_shop/report_templates/leaderboard_template.html.j2
scorecard    project     file:///.../jaffle_shop/report_templates/scorecard_template.html.j2
table        integrated  file:///.../dbtx/templates/table_template.html.j2
```

Templates receive:
- `report`: `title`, `description`, `owner`, `name` and `config`
- `columns`
- `rows`: tuples
- `records`: dicts keyed by column
- `params`: the report's `template_params`
- `generated_at`

Undefined variables raise an error, so typos fail the report instead of rendering blanks.

#### Extending templates

Templates extend each other **by template name**, with `{% extends "<name>" %}`. The name
goes through the same registry, so the parent can be an integrated or a project template.
The integrated `table` template defines these blocks: `title`, `style`, `header`,
`content`, `table_footer` and `footer`. The example project builds a small tree on it:

```
table (integrated)
  └── jaffle_base        house style: brand bar, palette, footer
        ├── finance      header + {{ super() }}, adds a banner and a totals row
        ├── leaderboard  replaces `content` with ranked bars
        └── scorecard    KPI tiles, then {{ super() }} for the inherited table
```

Call `{{ super() }}` in a block to keep what the parent renders. Children of
`jaffle_base` do this in `style` so the house style survives. `template_params` makes one
template reusable across reports: `customer_leaderboard` and `beverage_leaderboard` share
`leaderboard` but rank different columns with different number formats:

```yaml
template: leaderboard
template_params:
  label: customer_name       # entry name
  value: lifetime_spend      # ranks entries and sizes the bars
  detail: order_count        # optional sub-line
  prefix: "$"
```

Each example template documents its `template_params` in its header comment.

### Docs Run Data

dbt's docs site fetches `manifest.json` and `catalog.json` as siblings of `index.html` at
page load. The overlay follows that same shape rather than rewriting the 1.8 MB bundle:
run data goes in a sibling `dbtx_runtime.json`, and `index.html` gains one marker-delimited
`<script>` tag.

- Run data is **never** written into `manifest.json`. That file is dbt's, and every
  invocation overwrites it, which would discard the accumulated history. The sidecar is
  owned by dbtx alone and survives `dbt docs generate`.
- `dbtx docs patch` merges one run target into the docs location. The merge is an upsert
  keyed on `unique_id`, so nodes the run did not touch keep the data they already have.
  This is what lets a sequence of partial runs build up a complete picture.
- Each node keeps its **last 3 runs** by default (`--history N`). The cap is stored in the
  sidecar, so later patches hold the same depth without repeating the flag.
- Results carry the node checksum from the run that produced them. When that no longer
  matches the docs manifest the node's SQL has changed since it ran, and the result is
  flagged stale rather than presented as current.
- The overlay renders as a **Run Results** panel directly below a node's Description:
  status badge, duration, last ran / last compiled, the run's job and invocation ids, a
  stale banner where applicable, and a table of previous runs carrying the same two ids.
- Each result records the **job id** its invocation was launched with, read from the run's
  `--vars`. `run_results.json` is the only artifact this can come from: dbt copies the
  resolved command line into its top-level `args` verbatim, whereas `manifest.json` carries
  no args at all and so never mentions a var unless a model happens to interpolate it. The
  key defaults to `job_id` and is settable with `--job-id-var NAME`;
  `DBT_ENV_CUSTOM_ENV_<name>` is read as a fallback. Runs that passed no such var record no
  job id, and the panel omits the field rather than showing a blank.
- `dbtx docs generate` is still dbt's and passes straight through. `patch`, `install`,
  `status` and `serve` belong to dbtx; `serve` is covered in [Docs Server](#docs-server)
  below.

| Situation | Sidecar outcome |
|---|---|
| Node in both locations | run recorded, previous runs kept as history |
| Node in docs but not in this run | untouched; earlier run data retained |
| Node in the run but not in the docs | ignored and counted (see TODO below) |
| Node never run, or ephemeral | absent from the sidecar; panel reads "no run data" |
| Same run patched twice | no-op; the head is replaced, not appended |
| Run passed no job var | recorded with no job id; panel omits the field |
| Older artifacts patched after newer | skipped, unless `--force` |
| Node's SQL changed since its last run | result kept, flagged `stale` |

Because `index.html` is regenerated by `dbt docs generate`, the `<script>` tag is wiped
each time you regenerate -- and `dbt docs serve` wipes it too, for the same reason (see
[Docs Server](#docs-server)). Re-running `dbtx docs patch` reinstalls it; the sidecar itself
is untouched.

`dbt docs generate --static` is **not supported**. That build collapses the site into one
self-contained `index.html` with the artifacts inlined, so there is no sibling
`manifest.json` to read and no fetch for the overlay to attach run data to. The three dbtx
subcommands fail differently on one, so it is worth knowing which message means this:

| Command | On a `--static` bundle |
|---|---|
| `dbtx docs install` | refused: *"looks like `--static` output, which inlines its data"* |
| `dbtx docs patch` | refused: *"docs manifest not found: .../manifest.json"* |
| `dbtx docs serve` | serves it, but with no Run Results panel -- it is a valid HTML file, there is just nothing for the overlay to read |

Generate the default (non-static) docs when you want run data.

```console
$ dbtx docs status --docs-loc docs
dbtx docs: docs
  5 of 42 node(s) have run data
  1 stale (SQL changed since the recorded run)
  history limit 3
  last patched 2026-10-06T19:56:18.451423Z
  overlay installed
```

Nodes present in the run target but missing from the docs are currently ignored. Adding
them is a **TODO**: it needs the docs `manifest.json` extended too, or the node has nothing
to attach to.

### Docs Server

`dbtx docs serve` shadows dbt's `docs serve` with an ASGI app on uvicorn. It needs the
optional extra, which is why it is the one dbtx command that can be missing from a working
install:

```console
pip install 'dbtx[serve]'     # uvicorn + starlette
```

dbt's own server is a single-threaded `socketserver.TCPServer` that it points at the docs
by `chdir`-ing the whole process into `target/`. Both of those are a problem once the
overlay is in the picture, and two further differences follow from `serve` being dbtx's:

- **Requests are served concurrently.** The overlay adds a third artifact to every page
  load, so a page waiting behind one multi-megabyte `manifest.json` is immediately visible
  on dbt's one-at-a-time server.
- **The process never changes directory.** The docroot is resolved once, at construction,
  so a `chdir` by the caller cannot move it out from under a running server. dbt's
  process-global `chdir` is hostile to anything that embeds dbtx, and it never restores it.
- **The overlay asset is served from the package** when the docs directory has no installed
  copy, so `serve` works before `dbtx docs install` has ever run. An installed copy still
  wins, so `install` stays authoritative for what a reader gets.
- **No browser opens unless asked.** dbt's `--browser` defaults to on; this is opt-in,
  because `serve` is also meant to be startable from a script or a CI job.
- **A `--static` bundle is served as-is, without the overlay.** `serve` only validates that
  the directory holds an `index.html`, so unlike `install` and `patch` it does not refuse
  one -- it just has no sidecar to serve. See [the `--static` note](#docs-run-data) above.

dbt's server sends no cache validators at all, so every reload refetches the full manifest.
Three policies are needed instead, because the files have three different lifetimes:

| File | Policy | Why |
|---|---|---|
| `index.html`, `dbtx_runtime.json` | `no-store` | rewritten in place, by `dbt docs generate` and `dbtx docs patch`, under a running server |
| `dbtx_docs.js?v=<hash>` | immutable, 1 year | the URL carries a content hash, so its bytes never change |
| `manifest.json`, `catalog.json`, everything else | ETag + Last-Modified | unchanged since the last load is a 304, not a refetch |

`?v=` is honoured only on the overlay asset, the one file served under a content-hashed
URL. Treating it as a cache buster anywhere else would pin a regenerated `manifest.json`
in the browser for a year with no way to invalidate it.

The port is bound before uvicorn starts, which is what keeps a bad one on the same
`dbtx docs:` and exit-2 path as every other error here rather than surfacing as uvicorn's
bind failure:

```console
$ dbtx docs serve --docs-loc target --port 99999
dbtx docs: --port must be between 0 and 65535, got 99999
```

A port that is already serving is reported the same way, carrying the OS's message. Binding
first also means the socket is listening before `--browser` opens anything, so the browser's
first connection is queued by the kernel rather than refused mid-startup.

Responses are **not** compressed yet, which is the one part of the multi-megabyte
`manifest.json` problem this does not address: concurrency stops a page queueing behind it,
and ETags stop a *reload* refetching it, but a first load still transfers it in full.

#### Use `dbtx docs serve`, not `dbt docs serve`

They are one letter apart and the wrong one silently removes the overlay. dbt's `ServeTask`
starts by copying its own bundled `index.html` over the one in `target/`:

```python
os.chdir(self.config.project_target_path)
shutil.copyfile(DOCS_INDEX_FILE_PATH, "index.html")   # dbt/task/docs/serve.py
```

That bundled copy has no `<script>` tag, so the Run Results panel disappears from a docs
site that was patched moments earlier, with nothing printed to say why. `dbtx docs patch`
puts it back. `dbtx docs serve` never writes to the docs directory at all -- it only reads
it -- so serving is not something that can cost you the overlay.

## Usage

### Configuration

Every `dbtx` command first loads and checks its configuration. A problem exits with
code 2, and a message naming the setting and where it came from, before dbt starts.

**Platform mode (the default).** dbtx runs as a platform holding several dbt projects.
Each project is a folder directly under one platform root, and the root holds the
shared `dbtx.conf`:

```
/srv/dbtx/            <- platform root
  dbtx.conf
  jaffle-shop/        <- dbt_project.yml, profiles.yml, ...
  jaffle-shop-2/
```

```console
cd /srv/dbtx && dbtx build --project-dir jaffle-shop       # the cwd holds dbtx.conf
dbtx build --platform-dir /srv/dbtx --project-dir jaffle-shop
dbtx config                                                 # what was resolved, and from where
```

- The platform root is `--platform-dir`, or else the current directory if it holds
  `dbtx.conf` and is not itself a dbt project. dbtx never searches parent directories.
- A relative `--project-dir` is relative to the platform root, and the project must be
  a direct child of it.
- **Profiles are scoped per project:** dbtx always passes the project folder to dbt as
  `--profiles-dir`, so each project's own `profiles.yml` is used, never `~/.dbt`. A
  `--profiles-dir` or `DBT_PROFILES_DIR` pointing anywhere else is an error, so hosts
  that export `DBT_PROFILES_DIR` must unset it for platform runs. `--profile` and
  `--target` work as usual.
- Each project on a platform needs a unique `name:` in `dbt_project.yml`. When two
  folders declare the same one, a recorded command (below) from either stops (exit 2),
  unless that folder
  already owns the name in the database (below): then it runs, with a warning naming
  the folder that will be refused. A collision between two *other* projects is only a
  warning.
- **Project-name claims.** The database records which folder owns each dbt project
  name, so one name is never written by two folders, even on different hosts. The
  first run of a name claims it for its folder; a run from another folder is refused.
  Hosts sharing one database must use the same folder name for the same project.
  `dbtx db projects` lists the claims; after renaming or moving a project folder,
  `dbtx db projects rebind <name> --folder <new-folder>` moves its claim (refused while
  the old folder still exists, or when the new one is not a project folder on the
  platform, unless `--force`).

**Standalone mode.** `--standalone-mode` runs one project without the platform: no
config file, no database, no platform checks, and dbt's default project and profile
lookup. Database flags (`--db-uri`, `--db-backend`, `--db-schema`, `--config-file`) and
run-folder flags (`--log-root-location`, `--job-id`) contradict it and exit 2. `DBTX_*`
environment variables are ignored. Outputs go to the project's own `target/` and
`logs/`, as with plain dbt.

```console
cd jaffle-shop && dbtx build --standalone-mode
```

**Options (platform mode).** Highest precedence first: CLI flag, then the config file,
then the environment. `.env` files are never read.

| Option | CLI | `dbtx.conf` | Environment | Default |
|---|---|---|---|---|
| Database backend | `--db-backend enabled\|disabled` | `DB_BACKEND=` | `DBTX_DB_BACKEND` | `enabled` |
| Database URI | `--db-uri URI` | `DB_URI=` | `DBTX_DB_URI` | none |
| Postgres schema | `--db-schema NAME` | `DB_SCHEMA=` | `DBTX_DB_SCHEMA` | `dbtx` |
| Store adapter | `--db-adapter NAME` | `DB_ADAPTER=` | `DBTX_DB_ADAPTER` | the adapter for `DB_URI` |
| Config file | `--config-file PATH` | | `DBTX_CONFIG_FILE` | `<platform root>/dbtx.conf` |
| Run folder root | `--log-root-location DIR` | `LOG_ROOT_LOCATION=` | `DBTX_LOG_ROOT_LOCATION` | none; **required** |
| Job id | `--job-id ID` | | | a new one per run |

- `dbtx.conf` is strict `KEY=VALUE`: `#` comment lines and quoted values are allowed;
  unknown or duplicate keys, `export` and `${...}` are errors. See
  [examples/dbtx.conf.example](examples/dbtx.conf.example). A working copy,
  [examples/dbtx.conf](examples/dbtx.conf), makes `examples/` a platform root: run
  `dbtx build --platform-dir ..` from `examples/jaffle_shop` (from the project folder,
  so dbt finds the example's DuckDB file there).
- `DB_URI` is a SQLAlchemy URL: `sqlite:///path.db` or
  `postgresql+psycopg://user@host:5432/dbname` (needs `pip install 'dbtx[postgres]'`).
  A relative SQLite path is relative to the config file's directory. Leave the password
  out of the URI to have libpq read it from `~/.pgpass` or `PGPASSWORD`.
- `DB_SCHEMA` is the Postgres schema that holds dbtx's tables: lowercase letters,
  digits and `_`. SQLite has no schemas, so setting it with a SQLite URI is an error.
- `DB_ADAPTER` names the store adapter that talks to the database. Leave it unset to
  use the default for `DB_URI` (the built-in `sqlite` or `postgresql`). Set it only to
  use an adapter plugin instead (see below).
- `LOG_ROOT_LOCATION` is the root of the [run folders](#run-folders). Commands that
  run in a run folder (the recorded commands below) need it, whatever `DB_BACKEND`
  says; a relative path is relative to the config file's directory (or the current
  directory, from the CLI or the environment).
- With the backend enabled, a command that records runs (below) needs `DB_URI`;
  without one it exits 2 before dbt starts. Commands that do not touch the database
  (`ls`, `debug`, `deps`, `docs serve`, `templates`, ...) never need it, and
  `dbtx config` / `dbtx db status` report a missing URI instead of failing.

> **Upgrading from a version without run history.** dbtx now records every run, so
> two things that used to print a warning stop the command with exit 2:
> - running outside a platform (for example `cd jaffle-shop && dbtx build`): add
>   `--standalone-mode` to keep running a single project without the platform, or set
>   up a platform root with a `dbtx.conf`;
> - a platform with no `DB_URI`: set one, or pass `--db-backend disabled` (or
>   `DB_BACKEND=disabled` in `dbtx.conf`) to run without recording;
> - a database without dbtx's tables: run `dbtx db init` once (SQLite stores are no
>   longer created or migrated implicitly; after upgrading dbtx, run `dbtx db upgrade`).
>
> **Upgrading from a version without run folders.** Platform runs no longer write to
> the project's `target/`: set `LOG_ROOT_LOCATION` (exit 2 without it), look for a
> run's artifacts in its run folder, and drop `--target-path` / `--log-path` and
> `DBTX_JOB_ID` in `--vars` from platform command lines (exit 2; use `--job-id`).

#### Run folders

In platform mode every run of a recorded command (below) gets its own folder, so runs
never overwrite each other's artifacts and every run can be reviewed afterwards:

```
<LOG_ROOT_LOCATION>/<project>/<YYYY-MM-DD>/<HH>/<job id>/   # date and hour in UTC
  manifest.json  run_results.json  compiled/  run/  reports/  dbt.log  ...
```

- **Job id:** every run has one, retries included. Pass `--job-id ID` (a plain folder
  name), or dbtx generates one. dbt receives it as `DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID`, so
  it is recorded in `run_results.json` (`metadata.env.DBTX_JOB_ID`), in the run history
  and in the docs overlay. A run whose folder already exists (the same job id within
  the hour) is refused with exit 2.
- dbt is given `--target-path` and `--log-path` pointing at the run folder, for every
  invocation of the command (dbtx's internal parse and `ls` included). Passing your own
  is an error.
- **Partial parsing:** `dbtx warm-cache` refreshes the project's partial-parse cache in
  its own `target/` without running anything (`dbt parse --no-write-json`: only
  `partial_parse.msgpack` is written). Every run copies it into its folder, so dbt
  parses only what changed. Run it after deploying or merging new code, with the same
  `--target` the runs use: a different target, profile or CLI `--vars` makes dbt parse
  everything again. Keep `env_var('DBT_ENV_CUSTOM_ENV_DBTX_JOB_ID')` out of
  `dbt_project.yml` and `profiles.yml` for the same reason.
- **The docs site** is the project's own `target/`, which runs no longer write to.
  After `build`, `run`, `test`, `seed` and `snapshot`, dbtx copies the run's rendered
  `reports/` there and patches the site with the run. `dbtx docs generate` copies the
  new site (`index.html`, `manifest.json`, `catalog.json`) there; run `dbtx docs
  install` afterwards to put dbtx's overlay back. `dbtx docs install`, `status` and
  `serve` default `--docs-loc` to it. See
  [docs/flows/docs_refresh.md](docs/flows/docs_refresh.md).
- **Run folders are never deleted:** disk use under `LOG_ROOT_LOCATION` grows with every
  run, and `dbt clean` only removes the project's `target/` (the docs site and the warm
  cache). Prune old run folders yourself.
- How both modes behave: [docs/flows/usage.md](docs/flows/usage.md) (options and modes),
  [docs/flows/execution.md](docs/flows/execution.md) (step by step) and
  [docs/flows/commands.md](docs/flows/commands.md) (what each command does and does not
  do: run folder, recording, docs patching).

#### Run history store

In platform mode with the backend enabled, dbtx records each run of these commands:
`build`, `compile`, `run`, `test`, `seed`, `snapshot`, `parse`, `run-operation`,
`source freshness` and `docs generate`. Other commands (`ls`, `debug`, `show`, ...)
are not recorded. Standalone mode and `--db-backend disabled` record nothing and never
connect to a database.

What one run stores:

- **The invocation:** dbt's invocation id (the one in the run's `run_results.json`), the
  command, start and end time, result (`success`, `warning` or `failure`), dbt version,
  adapter type, target (dbt's resolved target, even without `--target`), selector,
  dbt's `args` (with `DBT_ENV_SECRET_*` values scrubbed, as dbt writes them to
  `run_results.json`; any other secret passed as a plain var is stored as dbt writes
  it), the job id, the run folder, user and host, and the sha256 of the
  `manifest.json` the run wrote.
- **A result for every node** dbt ran: status, timings, thread, message, failures,
  rows affected, adapter response, and the compiled SQL dbt ran with the relation it
  built or tested (for a failure, the exact SQL that failed; for models this is the
  `select`, without dbt's `create ... as` wrapper, which is in the run folder's `run/`).
- **The project's nodes over time:** every model, source, seed, snapshot, test, unit
  test and exposure, each checksum it has had, the edges between them, and when each
  was first and last seen and removed.
- **Reports and their renders** (`dbtx build` / `compile`): each report's definition,
  and in a build each selected report's render: `rendered` with its files, or
  `skipped` / `error` with the reason.

How it behaves:

- Everything is checked **before dbt starts**: the configuration, the connection, the
  schema revision (a database without dbtx's tables, or behind this dbtx version,
  stops with `run dbtx db init` / `run dbtx db upgrade`) and the project-name claim. A
  problem exits 2 and dbt never runs.
- Once dbt has started, **recording never changes the run**: the console output and
  the exit code are dbt's (and dbtx's reports'). If a write fails, dbtx prints one
  `dbtx: run history not recorded (invocation ...)` warning and the run carries on.
- A run whose project fails to parse (broken YAML, missing `profiles.yml`) is recorded
  as a `failure` with dbt's error message. Its nodes and reports are left as they were.
  When dbt failed before it loaded the profile, the target and adapter type are stored
  as `unknown`.
- `--no-write-json` still records everything; only the manifest hash is left empty.
- Keep a SQLite `DB_URI` outside dbt's clean targets (`target/` by default), or
  `dbt clean` deletes the history; `dbtx db status` warns about it.

#### Setting up the database: `dbtx db init`, `upgrade`, `status`

dbtx creates and migrates its own tables. The migrations ship inside the package, so
there is no `alembic.ini` to manage.

```console
dbtx db init       # Postgres: create DB_SCHEMA if missing; then create or migrate the tables
dbtx db upgrade    # migrate the tables to this dbtx version's schema
dbtx db status     # backend, URI, adapter, server version, schema revision, row counts
```

- **Schema changes are deliberate, on every backend.** Only these two commands create
  or migrate dbtx's tables: run `dbtx db init` once for a new platform (SQLite: it also
  creates the file), and `dbtx db upgrade` after upgrading dbtx. Any other command on a
  database without dbtx's tables, or behind this dbtx version, stops (exit 2) with
  `run dbtx db init` or `run dbtx db upgrade`. A SQLite file that does not exist yet is
  refused the same way, without being created (in-memory SQLite is unaffected).
- On Postgres, `dbtx db init` creates `DB_SCHEMA` when the database user is allowed
  to; otherwise an administrator creates it first.
- A database migrated by a newer dbtx is refused: upgrade dbtx instead.
- `dbtx db status` only reports. It creates nothing (not even a SQLite file), works
  with the backend disabled or no `DB_URI`, and warns when a SQLite file holds several
  projects (one file suits one host) or sits inside a project's dbt clean target
  (`dbt clean` would delete it).

#### Importing plain dbt runs: `dbtx db load`

Runs made with plain `dbt` (or anything that writes dbt's artifacts) can be imported
after the fact:

```console
dbtx db load --project-dir jaffle-shop                      # reads jaffle-shop/target/
dbtx db load --project-dir jaffle-shop --run-loc ci/artifacts
```

- It reads `manifest.json` and `run_results.json` and/or `sources.json` (source
  freshness), and stores the project's nodes, their versions and edges, its reports,
  and each run with its node results.
- The manifest must belong to the project folder, and the folder claims the project
  name as a run does.
- Loading the same artifacts again changes nothing, and older runs can be loaded after
  newer ones.
- An artifact from a command dbtx does not record (`dbt ls`, `debug`, ...) is skipped
  with a warning.
- The artifacts do not say which target ran or who ran it, so `target` is stored as
  `unknown` (unless `--target` was passed) and the user and host are left empty.

#### Database adapter plugins

SQLite and Postgres are built in. **Store adapter plugins** add other databases, or
other drivers or behaviour for SQLite and Postgres. A plugin is a separate Python
package that registers a named `StoreAdapter` under the `dbtx.store_adapters`
entry-point group. Which adapter serves `DB_URI`:

- `DB_ADAPTER`, when set, names it. It must support the URI's database and driver.
- Otherwise the built-in, when it supports the URI's database and driver. Installing a
  plugin never changes this.
- Otherwise the one installed plugin that does, e.g. for `duckdb+duckdb_engine:///...`
  or `sqlite+pysqlcipher:///...`. If several do, set `DB_ADAPTER`.

```ini
DB_URI=sqlite:///.dbtx/dbtx.db
# A plugin instead of the built-in sqlite adapter.
DB_ADAPTER=custom_sqlite
```

Install a plugin **into the same Python environment as dbtx** (`dbtx-duckdb` is an
example name, not a published package):

```console
pip install dbtx-duckdb                        # same virtualenv as dbtx
pipx inject dbtx dbtx-duckdb                   # dbtx installed with pipx
uv tool install dbtx --with dbtx-duckdb        # dbtx installed with uv tool
```

Putting a module on `PYTHONPATH` without installing it does not register it. An
unknown `DB_ADAPTER` lists every adapter dbtx can see, and `dbtx config` shows which
one serves `DB_URI`. See
[docs/contributing/plugins/adapters.md](docs/contributing/plugins/adapters.md) for the
adapter interface, writing a plugin, and the supported install methods.

### Using DBTX Exposures
```console
dbtx build                               # everything, with reports
dbtx build -s +exposure:top_customers    # one report and exactly what it needs
dbtx build -s exposure:top_customers     # re-render from existing tables
dbtx build --no-reports                  # plain dbt build
dbtx compile                             # validate reports, write their SQL, no queries
dbtx compile -s +exposure:top_customers  # ...for one report
dbtx templates                           # templates this project can use, and where from
```

### Using DBTX Docs
In platform mode the docs site is kept current by the runs themselves (see
[Run folders](#run-folders)); `dbtx docs install`, `status` and `serve` default
`--docs-loc` to the project's `target/`. The commands below take explicit locations, as
standalone mode always does and `dbtx docs patch` always does.

Docs run data (`--docs-loc` holds the generated docs, `--run-loc` the run's artifacts):
```console
dbtx docs patch --docs-loc target --run-loc target        # merge this run into the docs
dbtx docs patch --docs-loc docs/ --run-loc target         # ...docs published elsewhere
dbtx docs patch --docs-loc target --run-loc target --history 5   # keep 5 runs per node
dbtx docs patch --docs-loc target --run-loc target --job-id-var ci_run  # tag var not job_id
dbtx docs patch --docs-loc target --run-loc target --force       # re-apply older artifacts
dbtx docs patch --docs-loc target --run-loc target --no-overlay  # sidecar only, no HTML edit
dbtx docs status --docs-loc target                        # coverage, staleness, last patch
dbtx docs install --docs-loc target                       # (re)install the overlay
dbtx docs install --docs-loc target --uninstall           # remove it and the asset
dbtx docs serve --docs-loc target                         # serve them on 127.0.0.1:8080
dbtx docs serve --docs-loc target --port 8081 --browser   # ...and open a browser at it
dbtx docs serve --docs-loc target --host 0.0.0.0          # reachable off this machine
```
`serve` needs the optional extra: `pip install 'dbtx[serve]'` (uvicorn and starlette).

#### DBTX Docs Example Sequence

End to end on `examples/jaffle_shop`, a project on the `examples/` platform (its
`dbtx.conf` sets `DB_URI` and `LOG_ROOT_LOCATION=.dbtx/logs`). Run from the project
folder with `--platform-dir ..`, so dbt finds the example's DuckDB file there.

Shell 1 — create dbtx's tables (once), warm the cache, build, generate the docs site and
attach the overlay:

```console
$ cd examples/jaffle_shop
$ dbtx db init --platform-dir ..
created dbtx's tables in sqlite:///.../examples/.dbtx/dbtx.db (revision 0002)

$ dbtx warm-cache --platform-dir ..
dbtx: warmed the partial-parse cache .../examples/jaffle_shop/target/partial_parse.msgpack

$ dbtx build --platform-dir ..
...
dbtx docs: no docs site in .../examples/jaffle_shop/target to patch; run `dbtx docs generate`, then `dbtx docs install`

$ dbtx docs generate --platform-dir ..
...
dbtx docs: copied index.html, manifest.json, catalog.json to .../examples/jaffle_shop/target; run `dbtx docs install` to add dbtx's overlay

$ dbtx docs install --platform-dir ..
dbtx docs: overlay installed
  target/index.html
  target/dbtx_docs.js
```

Shell 2 — serve the site, and leave it running:

```console
$ cd examples/jaffle_shop && dbtx docs serve --platform-dir ..
dbtx docs: serving .../examples/jaffle_shop/target
  http://127.0.0.1:8080  (ctrl-c to quit)
```

Shell 1 — each run patches the site as it finishes; refresh the browser to see it (the
overlay re-fetches the sidecar on load, so the server does not need restarting):

```console
$ dbtx run -s stg_customers stg_orders --platform-dir ..
...
dbtx docs: patched the docs site .../examples/jaffle_shop/target with this run

$ dbtx run -s customers orders --platform-dir .. --job-id customers-1
...
dbtx docs: patched the docs site .../examples/jaffle_shop/target with this run
```

Each run's own artifacts and log stay in its folder under
`examples/.dbtx/logs/jaffle_shop/<date>/<hour>/<job id>/`.

**Standalone mode** runs the same loop by hand: every command takes `--standalone-mode`,
runs write to the project's `target/`, and you patch the docs yourself:

```console
$ cd examples/jaffle_shop
$ dbtx docs generate --standalone-mode
$ dbtx docs install --docs-loc target --standalone-mode
$ dbtx run -s customers --standalone-mode
$ dbtx docs patch --docs-loc target --run-loc target --standalone-mode
dbtx docs: patched 1
```

Things to know when working through this:

- In standalone mode, patch **after** a targeted `dbtx run`, not straight after
  `dbtx docs generate`: `generate` writes its own `run_results.json` covering every
  node, so patching there records all of them at once.
- Re-running `dbtx docs generate` replaces `index.html` without the overlay. `dbtx docs
  install` (or the next patch) puts it back; the sidecar is never touched, so no history
  is lost.
- dbt resolves the DuckDB path in `profiles.yml` relative to the directory it runs from,
  which is why the example runs from the project folder.

## Development

```console
uv sync                                  # or: pip install -e '.[serve]' dbt-duckdb pytest httpx
cd examples/jaffle_shop && uv run dbtx build --platform-dir ..    # history in examples/.dbtx/
cd examples/jaffle_shop && uv run dbtx build --standalone-mode   # or without the platform
```

### Example project

`examples/jaffle_shop` is a jaffle-shop-style project on DuckDB (the database file is
`examples/jaffle_shop/jaffle_shop.duckdb`, outside `target/` so `dbt clean` keeps it). It has 5 seeds (1,000 customers,
8,000 orders, ~18,000 items over two years, 6 stores), staging views, an ephemeral
intermediate model, 5 marts, 26 data tests (including a warn-severity one that is expected
to warn), 6 dbtx reports and one plain exposure that dbtx ignores. So that every command
dbtx records has something to run, it also has a source with a freshness rule (the
`raw_orders` seed; always stale, so `dbtx source freshness` warns), a snapshot of the
stores (`dbtx snapshot`) and a macro (`dbtx run-operation log_store_count`). The reports
cover every template source and style of extension:

| Report | Template | Source |
|---|---|---|
| `top_customers` | `table` | integrated |
| `store_revenue_monthly` | `finance` (table > jaffle_base > finance) | project, extended |
| `customer_leaderboard` | `leaderboard` (table > jaffle_base > leaderboard) | project, extended |
| `beverage_leaderboard` | `leaderboard`, with different `template_params` | project, extended |
| `store_scorecard` | `scorecard` (table > jaffle_base > scorecard) | project, extended |
| `jaffle_mix` | `exposure_templates/jaffle_mix.html` | exact `template_path` |

The seeds are generated deterministically, so tests can assert on exact figures:

```console
python scripts/generate_seeds.py
```

### Tests

```console
pytest -m "not live"          # ~1 min: unit tests (Postgres ones start a throwaway server)
pytest                        # ~2 min: also real dbt runs of the example project
ci/run.sh                     # the whole CI run, as GitHub runs it (into /mnt/cicd)
```

CI (GitHub Actions, Python 3.10 and 3.14) runs `ci/run.sh`: lint (ruff), unit and live
tests, and a build of the wheel that is installed and run. The same scripts run locally;
see [docs/contributing/ci.md](docs/contributing/ci.md).

- **Unit tests** (`tests/unit`) load `tests/fixtures/manifest.json`, a frozen, scrubbed
  `dbt parse` of the example project, as a real dbt `Manifest`. No warehouse and no parse
  are needed. They cover report discovery and validation, SQL generation, template
  resolution and precedence, rendering and the scheduler's gating rules.
  `test_docs_sidecar.py` drives the docs overlay on synthetic artifacts instead: partial
  runs accumulating across patches, the history cap, idempotent re-patching, staleness
  detection and the HTML injection. `test_docs_server.py` drives the server through
  Starlette's `TestClient`, so routing, cache policy and docroot escapes are covered
  in-process without binding a port; `test_docs_server_cli.py` covers its option parsing.
- **Live tests** (`tests/live`) run `dbtx` for real, end to end, on throwaway copies of
  the example project. They cover selection, failing tests, failing models, report errors
  and passthrough, in standalone mode. `test_store_recording.py` runs the project on a
  throwaway platform and checks what the run history store holds against dbt's own
  artifacts. `test_docs_server_boot.py` is also marked `server` and needs no
  warehouse -- it boots the docs server on a loopback socket (`pytest -m server`).
- After changing the example project, refresh the fixture. A test fails while it is stale.

  ```console
  python scripts/freeze_manifest.py
  ```

## Requirements

dbt-core 1.12+ (exposures run as nodes in `dbt build`), Python 3.10+.

A Postgres `DB_URI` needs the `postgres` extra -- psycopg 3 (`pip install 'dbtx[postgres]'`).

`dbtx docs serve` additionally needs the `serve` extra -- uvicorn and starlette
(`pip install 'dbtx[serve]'`). Everything else, including `dbtx docs patch`, `install` and
`status`, works without it: the server is imported at the point of use, not at startup, so
a plain install is not carrying a web stack it never runs.
