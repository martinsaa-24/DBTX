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

## Usage

```console
dbtx build                               # everything, with reports
dbtx build -s +exposure:top_customers    # one report and exactly what it needs
dbtx build -s exposure:top_customers     # re-render from existing tables
dbtx build --no-reports                  # plain dbt build
dbtx compile                             # validate reports, write their SQL, no queries
dbtx compile -s +exposure:top_customers  # ...for one report
dbtx templates                           # templates this project can use, and where from
```

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
- **Integration tests** (`tests/integration`) run `dbtx` end to end on throwaway copies of
  the example project. They cover selection, failing tests, failing models, report errors
  and passthrough.
- After changing the example project, refresh the fixture. A test fails while it is stale.

  ```console
  python scripts/freeze_manifest.py
  ```

## Requirements

dbt-core 1.12+ (exposures run as nodes in `dbt build`), Python 3.10+.
