# Refreshing the docs site

In platform mode, every run writes to its own folder under `LOG_ROOT_LOCATION`. The
docs site is served from one fixed place: the project's target folder
(`<dbt_project>/<target>`, `target/` by default), which runs no longer write to.

## After every `dbtx docs generate`, run `dbtx docs install`

```console
dbtx docs generate --project-dir jaffle-shop
dbtx docs install  --project-dir jaffle-shop
```

`dbtx docs generate` writes the docs site into its run folder, then copies the files
the site needs into the project's target folder: `index.html`, `manifest.json` and
`catalog.json` (the page loads the two JSON files from beside `index.html`).

The copied `index.html` is dbt's own, so it no longer contains dbtx's overlay (the
run data, history and embedded reports). **`dbtx docs install` puts the overlay back.**
Until it runs, the site shows plain dbt docs (the next run's automatic patch also puts
it back, but do not rely on a run coming soon).

## What keeps the site current

You do not need to patch the docs after a run. In platform mode, every command that
executes nodes (`build`, `run`, `test`, ...) finishes by:

1. copying the run's rendered `reports/` (when it rendered any) into
   `<dbt_project>/<target>/reports/`, and
2. patching the site with the run's results (`dbtx docs patch`, run internally).

`dbtx docs patch` stays a separate utility. Called from the command line it always
takes explicit `--docs-loc` and `--run-loc`.
