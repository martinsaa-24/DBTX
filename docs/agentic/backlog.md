# Backlog

Work agreed to be worth doing, deferred by the user. Each item says where it came from.
Small, understood fixes are in [todo.md](todo.md).
The plan is [orm_implementation_plan.md](orm_implementation_plan.md); the user-facing
flows are in [docs/flows/](../flows/).

## Finding a past run's folder (`dbt retry`, `--state`, `--defer`)

**From:** Phase 6, open item 5 (2026-10-13): "We will worry about this later."

In platform mode every run writes to its own folder,
`<LOG_ROOT_LOCATION>/<project>/<YYYY-MM-DD>/<HH>/<job id>/`, and the project's `target/`
is the docs site. Tools that read a previous run from the target path therefore find
nothing:

- `dbtx retry`: dbt reads the previous `run_results.json` from the target path (its
  command line has no `--state` option) and reports "Could not find previous run in
  'target' target directory" (checked with dbt 1.12.5). Pointing `--target-path` at a
  run folder is not a workaround: dbt would read the old run there and write its own
  results over it.
- `--state` / `--defer` (state comparison, deferral): the user must name a run folder
  by hand.

The store already records each run's folder (`invocation.run_folder`, with its job id,
command, result and times), so the missing piece is a way to find it, for example:

- a command that prints the latest (or a given job's) run folder for a project, usable
  as `--state "$(dbtx ...)"`, or
- `dbtx retry` support: run dbt's retry with the previous run folder's results as its
  input, in a new run folder of its own (a new job id).

Open questions: which run counts as "the previous one" (latest finished, latest failed,
by job id); whether retry should be recorded as its own invocation linked to the
original job.
