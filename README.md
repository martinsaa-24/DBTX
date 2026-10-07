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
- `dbtx docs generate` and `dbtx docs serve` are still dbt's and pass straight through.
  Only `patch`, `install` and `status` belong to dbtx.

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
each time you regenerate. Re-running `dbtx docs patch` reinstalls it; the sidecar itself is
untouched. `--static` docs are not supported, since that build inlines its data and leaves
no sibling JSON to read.

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

## Usage

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
Docs run data (`--docs-loc` holds the generated docs, `--run-loc` the run's `target/`):
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
```
#### DBTX Docs Example Sequence

End to end on `examples/jaffle_shop`, run from the repo root. Two shells: one serving the
docs, one running models and patching. dbt resolves `profiles.yml` from the project folder,
so each dbt-facing command needs both `--project-dir` and `--profiles-dir`.

Shell 1 — populate the warehouse, generate dbt's docs, attach the overlay:

```console
$ dbtx build --no-reports --project-dir examples/jaffle_shop --profiles-dir examples/jaffle_shop
...
20:11:46  Done. PASS=40 WARN=1 ERROR=0 SKIP=0 NO-OP=7 REUSED=0 TOTAL=48

$ dbtx docs generate --project-dir examples/jaffle_shop --profiles-dir examples/jaffle_shop
...
20:11:52  Catalog written to .../examples/jaffle_shop/target/catalog.json

$ dbtx docs install --docs-loc examples/jaffle_shop/target
dbtx docs: overlay installed
  examples/jaffle_shop/target/index.html
  examples/jaffle_shop/target/dbtx_docs.js
```

Shell 2 — serve, and leave it running. It serves `target/`, so the sidecar and the overlay
asset are picked up alongside dbt's own artifacts:

```console
$ dbtx docs serve --project-dir examples/jaffle_shop --profiles-dir examples/jaffle_shop
```

Shell 1 — the run/patch loop. Refresh the browser after each patch; the overlay re-fetches
the sidecar on load, so the server does not need restarting:

```console
$ dbtx run -s stg_customers stg_orders --project-dir examples/jaffle_shop --profiles-dir examples/jaffle_shop
$ dbtx docs patch --docs-loc examples/jaffle_shop/target --run-loc examples/jaffle_shop/target
dbtx docs: patched 2
  carried forward 0 node(s) from previous runs
  history limit 3, wrote examples/jaffle_shop/target/dbtx_runtime.json

$ dbtx run -s customers orders --project-dir examples/jaffle_shop --profiles-dir examples/jaffle_shop
$ dbtx docs patch --docs-loc examples/jaffle_shop/target --run-loc examples/jaffle_shop/target
dbtx docs: patched 2
  carried forward 2 node(s) from previous runs    # the staging pair kept its data

$ dbtx run -s customers --project-dir examples/jaffle_shop --profiles-dir examples/jaffle_shop
$ dbtx docs patch --docs-loc examples/jaffle_shop/target --run-loc examples/jaffle_shop/target
dbtx docs: patched 1
  carried forward 4 node(s) from previous runs    # customers now has 2 runs of history

$ dbtx docs status --docs-loc examples/jaffle_shop/target
dbtx docs: examples/jaffle_shop/target
  4 of 42 node(s) have run data
  history limit 3
  last patched 2026-10-06T20:12:29.939901Z
  overlay installed
```

Four nodes accumulated from three partial runs, with `customers` carrying two:

```console
customers        runs=2  ['success', 'success']
orders           runs=1  ['success']
stg_customers    runs=1  ['success']
stg_orders       runs=1  ['success']
```

Three things to know when working through this:

- Patch **after** a targeted `dbtx run`, not straight after `dbtx docs generate`. `generate`
  writes its own `run_results.json` covering every node, so patching there records all 41
  at once and there is no partial accumulation left to see.
- Re-running `dbtx docs generate` rewrites `index.html` and drops the overlay tag. The next
  `dbtx docs patch` puts it back, and the sidecar is never touched, so no history is lost.
- Because dbt resolves the DuckDB path in `profiles.yml` relative to the directory it runs
  from, running from the repo root creates `jaffle_shop.duckdb` **at the repo root** rather
  than inside the project. `.gitignore` only covers `examples/*/*.duckdb`, so that copy
  shows up as untracked.

## Development

```console
uv sync                                  # or: pip install -e . dbt-duckdb pytest
cd examples/jaffle_shop && uv run dbtx build
```

### Example project

`examples/jaffle_shop` is a jaffle-shop-style project on DuckDB (the database file is
`examples/jaffle_shop/jaffle_shop.duckdb`, outside `target/` so `dbt clean` keeps it). It has 5 seeds (1,000 customers,
8,000 orders, ~18,000 items over two years, 6 stores), staging views, an ephemeral
intermediate model, 5 marts, 26 data tests (including a warn-severity one that is expected
to warn), 6 dbtx reports and one plain exposure that dbtx ignores. The reports cover every
template source and style of extension:

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
pytest -m "not integration"   # ~1s:  unit tests against the frozen manifest
pytest                        # ~80s: also builds the example project in temp copies
```

- **Unit tests** (`tests/unit`) load `tests/fixtures/manifest.json`, a frozen, scrubbed
  `dbt parse` of the example project, as a real dbt `Manifest`. No warehouse and no parse
  are needed. They cover report discovery and validation, SQL generation, template
  resolution and precedence, rendering and the scheduler's gating rules.
  `test_docs_sidecar.py` drives the docs overlay on synthetic artifacts instead: partial
  runs accumulating across patches, the history cap, idempotent re-patching, staleness
  detection and the HTML injection.
- **Integration tests** (`tests/integration`) run `dbtx` end to end on throwaway copies of
  the example project. They cover selection, failing tests, failing models, report errors
  and passthrough.
- After changing the example project, refresh the fixture. A test fails while it is stale.

  ```console
  python scripts/freeze_manifest.py
  ```

## Requirements

dbt-core 1.12+ (exposures run as nodes in `dbt build`), Python 3.10+.
