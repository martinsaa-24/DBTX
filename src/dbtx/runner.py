"""Runs dbt in-process: `build` renders reports as their exposures complete, and
`compile` validates reports and writes their SQL."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from click.core import ParameterSource
from dbt.cli.main import cli as dbt_cli
from dbt.cli.main import dbtRunner, dbtRunnerResult

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


def params_for(command: str, params: dict[str, Any]) -> dict[str, Any]:
    """Subset of `params` that dbt's `command` accepts (e.g. `--select` for ls, not for parse)."""
    accepted = {p.name for p in dbt_cli.commands[command].params}
    return {k: v for k, v in params.items() if k in accepted}


def selected_unique_ids(manifest: Any, params: dict[str, Any]) -> set[str]:
    """Resolve the user's selection exactly as dbt would, via `dbt ls`."""
    result = dbtRunner(manifest=manifest).invoke(
        ["ls", "--output", "json", "--output-keys", "unique_id", "--log-level", "none"],
        **params_for("list", params),
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
    try:
        templates = TemplateEngine(TemplateRegistry.for_project(project_root(params)))
    except TemplateError as exc:
        print(f"dbtx: {exc}")
        raise Stop(2) from None

    parsed = dbtRunner().invoke(["parse", "--quiet"], **params_for("parse", params))
    if not parsed.success:
        # Let dbt report the parse error in its usual format.
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
        max_workers=params.get("threads") or 4,
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
