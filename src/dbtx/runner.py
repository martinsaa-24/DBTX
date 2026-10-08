"""Runs dbt in-process: `build` renders reports as their exposures complete, and
`compile` validates reports and writes their SQL."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import yaml
from click.core import Parameter, ParameterSource
from dbt.cli.exceptions import DbtUsageException
from dbt.cli.main import cli as dbt_cli
from dbt.cli.main import dbtRunner, dbtRunnerResult
from dbt.cli.option_types import YAML

from dbt_common.events.base_types import EventLevel
from dbt_common.events.functions import fire_event
from dbt_common.events.types import Note

from dbtx.reports import Report, ReportDefinitionError, discover_reports, render, write_sql
from dbtx.scheduler import ReportScheduler
from dbtx.templates import TemplateEngine, TemplateError, TemplateRegistry


def exit_code(result: dbtRunnerResult) -> int:
    """Mirror dbt's own exit codes: 0 ok, 1 failure, 2 unhandled error."""
    if result.success:
        return 0
    return 2 if result.exception is not None else 1


def usage_error(command: str, args: list[str]) -> str | None:
    """click's own complaint about `args`, or None when dbt's `command` accepts them.

    Checked before anything else: `explicit_params` parses resiliently, which turns an
    option click rejects into None and so drops it from the argv `argv_for` rebuilds.
    Without this the run would fail later, reporting the default of the option that went
    missing rather than what the user actually typed."""
    try:
        dbt_cli.commands[command].make_context(command, list(args))
    except click.UsageError as exc:
        return exc.format_message()
    return None


def explicit_params(command: str, args: list[str]) -> dict[str, Any]:
    """Parse `args` as dbt's `command` would, keeping only options given on the command line.

    Defaults and `DBT_*` environment variables are left out: every dbt invocation dbtx
    makes reads those by itself."""
    ctx = dbt_cli.commands[command].make_context(command, list(args), resilient_parsing=True)
    return {
        k: v
        for k, v in ctx.params.items()
        if v is not None and ctx.get_parameter_source(k) is ParameterSource.COMMANDLINE
    }


def _long_opt(param: Parameter) -> str:
    """The option's long form; `--select`'s own `opts[0]` is `-s`, which reads badly in logs."""
    return next((opt for opt in param.opts if opt.startswith("--")), param.opts[0])


def argv_for(command: str, params: dict[str, Any]) -> list[str]:
    """Render `params` as command-line tokens for dbt's `command`, dropping the options it
    does not accept (e.g. `--select` for ls, not for parse).

    Options are handed to dbt on the argv rather than through `dbtRunner.invoke(**kwargs)`:
    kwargs are applied only after click has built the command's context, so click still
    parses and validates every option's *default* -- and `--project-dir`/`--profiles-dir`
    are `click.Path(exists=True)` whose default is the cwd when it holds a `profiles.yml`
    and `~/.dbt` otherwise. Running from outside a dbt project with no `~/.dbt` therefore
    failed the invocation on a default the kwarg was about to replace.
    """
    accepted = {p.name: p for p in dbt_cli.commands[command].params}
    tokens: list[str] = []
    for name, value in params.items():
        param = accepted.get(name)
        if param is None:
            continue
        if getattr(param, "is_flag", False):
            # A `--flag/--no-flag` pair; a flag with no negation is emitted only when set.
            opts = param.opts if value else param.secondary_opts
            if opts:
                tokens.append(_long_opt(param) if value else opts[0])
        elif isinstance(param.type, YAML):
            # Flow style keeps it to the single argv token dbt's YAML option expects.
            tokens += [_long_opt(param), yaml.safe_dump(value, default_flow_style=True).strip()]
        elif getattr(param, "multiple", False):
            tokens += [tok for v in value for tok in (_long_opt(param), str(v))]
        else:
            tokens += [_long_opt(param), str(value)]
    return tokens


def selected_unique_ids(manifest: Any, params: dict[str, Any]) -> set[str]:
    """Resolve the user's selection exactly as dbt would, via `dbt ls`."""
    result = dbtRunner(manifest=manifest).invoke(
        ["ls", "--output", "json", "--output-keys", "unique_id", "--log-level", "none"]
        + argv_for("list", params)
    )
    if not result.success:
        raise RuntimeError(f"could not resolve selection: {result.exception}")
    return {json.loads(line)["unique_id"] for line in result.result or []}


def gate_tests(manifest: Any, report: Report, run_set: set[str]) -> set[str]:
    """Tests in this run that check one of the report's direct parents."""
    return {
        uid
        for uid in run_set
        if uid.startswith("test.")
        and uid in manifest.nodes
        and report.parents.intersection(manifest.nodes[uid].depends_on.nodes)
    }


def project_root(params: dict[str, Any]) -> Path:
    return Path(params.get("project_dir") or os.getcwd())


@dataclass(frozen=True)
class OutputPaths:
    """Where report files go, mirroring dbt's own layout:

    * `compiled`: report SQL, written by both `dbtx compile` and `dbtx build`
    * `run`:      the SQL as executed, written by `dbtx build` just before it runs
    * `reports`:  rendered deliverables (html, csv)
    """

    reports: Path
    compiled: Path
    run: Path

    @classmethod
    def for_project(cls, params: dict[str, Any], project_name: str) -> OutputPaths:
        project_dir = project_root(params)
        target_path = params.get("target_path")
        if not target_path:
            project_yml = project_dir / "dbt_project.yml"
            target_path = yaml.safe_load(project_yml.read_text(encoding="utf-8")).get(
                "target-path", "target"
            )
        target = project_dir / target_path
        return cls(
            reports=target / "reports",
            compiled=target / "compiled" / project_name / "reports",
            run=target / "run" / project_name / "reports",
        )


def query_and_render(
    adapter_type: str, report: Report, paths: OutputPaths, templates: TemplateEngine
) -> list[Path]:
    # Like a dbt node in `build`: compiled when it starts executing, and the run file
    # written immediately before the query is sent, so a failing query leaves both behind.
    sql = report.sql()
    write_sql(report, paths.compiled, sql)
    # Reuse the adapter dbt registered for this invocation, so reports share its
    # credentials and connection handling (and, for DuckDB, the same database handle).
    from dbt.adapters.factory import get_adapter_by_type

    adapter = get_adapter_by_type(adapter_type)
    with adapter.connection_named(f"dbtx.{report.name}"):
        write_sql(report, paths.run, sql)
        _, table = adapter.execute(sql, fetch=True)
    rows = [tuple(row) for row in table.rows]
    rendered = render(report, list(table.column_names), rows, paths.reports, templates)
    # Goes through dbt's logger, so it honours --quiet, --log-format json and the log file.
    fire_event(
        Note(msg=f"dbtx: rendered report {report.name} ({len(rows)} rows)"),
        level=EventLevel.INFO,
    )
    return rendered


class Stop(Exception):
    """Ends a dbtx command early with an exit code."""

    def __init__(self, code: int) -> None:
        self.code = code


@dataclass
class Prepared:
    params: dict[str, Any]
    manifest: Any
    templates: TemplateEngine
    reports: dict[str, Report]
    paths: OutputPaths


def prepare(command: str, args: list[str], params: dict[str, Any]) -> Prepared:
    """Start-up shared by `build` and `compile`: resolve templates, parse the project and
    validate every report definition, all before dbt does any real work."""
    problem = usage_error(command, args)
    if problem:
        print(f"dbtx: {problem}")
        raise Stop(2)

    try:
        templates = TemplateEngine(TemplateRegistry.for_project(project_root(params)))
    except TemplateError as exc:
        print(f"dbtx: {exc}")
        raise Stop(2) from None

    parsed = dbtRunner().invoke(["parse", "--quiet", *argv_for("parse", params)])
    if not parsed.success:
        if isinstance(parsed.exception, DbtUsageException):
            # dbt rejected the argv dbtx rebuilt, not the project. Falling back to the
            # user's own argv would succeed and silently render no reports at all, so
            # fail instead -- `usage_error` above has already ruled out their own options.
            print(f"dbtx: cannot run dbt parse: {parsed.exception}")
            raise Stop(2)
        # A real project error. Let dbt report it in its usual format.
        raise Stop(exit_code(dbtRunner().invoke([command, *args])))
    manifest = parsed.result

    try:
        reports = discover_reports(manifest, templates, project_root(params))
    except ReportDefinitionError as exc:
        print(f"dbtx: {exc}")
        raise Stop(2) from None

    paths = OutputPaths.for_project(params, manifest.metadata.project_name)
    return Prepared(params, manifest, templates, reports, paths)


def build(args: list[str], reports_enabled: bool = True) -> int:
    if not reports_enabled:
        return exit_code(dbtRunner().invoke(["build", *args]))
    params = explicit_params("build", args)
    try:
        prep = prepare("build", args, params)
    except Stop as stop:
        return stop.code

    run_set = selected_unique_ids(prep.manifest, params) if prep.reports else set()
    plans = [
        (report, gate_tests(prep.manifest, report, run_set))
        for uid, report in prep.reports.items()
        if uid in run_set
    ]
    if not plans:
        return exit_code(dbtRunner(manifest=prep.manifest).invoke(["build", *args]))

    adapter_type = prep.manifest.metadata.adapter_type
    scheduler = ReportScheduler(
        plans,
        render=lambda report: query_and_render(adapter_type, report, prep.paths, prep.templates),
    )
    result = dbtRunner(manifest=prep.manifest, callbacks=[scheduler.on_event]).invoke(
        ["build", *args]
    )
    scheduler.finish()

    print_summary(scheduler)
    report_errors = any(o.status == "error" for o in scheduler.outcomes.values())
    code = exit_code(result)
    return code if code or not report_errors else 1


def compile_project(args: list[str], reports_enabled: bool = True) -> int:
    """`dbt compile`, plus validation of every report and SQL for the selected ones."""
    params = explicit_params("compile", args)
    if not reports_enabled or "inline" in params:
        # `--inline` compiles one ad-hoc query; project reports do not apply.
        return exit_code(dbtRunner().invoke(["compile", *args]))
    try:
        prep = prepare("compile", args, params)
    except Stop as stop:
        return stop.code

    run_set = selected_unique_ids(prep.manifest, params) if prep.reports else set()
    selected = sorted(
        (r for uid, r in prep.reports.items() if uid in run_set), key=lambda r: r.name
    )
    result = dbtRunner(manifest=prep.manifest).invoke(["compile", *args])

    # Report SQL depends only on the manifest, so it is written even if dbt's own
    # compile fails; the exit code still reflects dbt's result.
    print(f"\ndbtx: {len(prep.reports)} report definition(s) valid, {len(selected)} compiled")
    for report in selected:
        path = write_sql(report, prep.paths.compiled)  # compile never writes target/run
        print(f"  {'COMPILED':<9} {report.name}  {_display_path(path)}")
    return exit_code(result)


def print_summary(scheduler: ReportScheduler) -> None:
    outcomes = scheduler.outcomes
    print(f"\ndbtx: {len(outcomes)} report(s)")
    for name, outcome in sorted(outcomes.items()):
        if outcome.status == "rendered":
            detail = ", ".join(_display_path(p) for p in outcome.paths)
        else:
            detail = outcome.detail
        print(f"  {outcome.status.upper():<9} {name}  {detail}")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
