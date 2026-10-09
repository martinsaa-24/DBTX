"""`dbtx` entry point.

    dbtx build [--no-reports] [dbt build options...]   build + render reports in one run
    dbtx compile [--no-reports] [dbt compile options...]  compile, validate reports, write their SQL
    dbtx templates [--project-dir DIR]                 list the templates a project can use
    dbtx docs patch|install|status [options...]        overlay run data onto generated dbt docs
    dbtx docs serve --docs-loc DIR [options...]        serve the generated docs over uvicorn
    dbtx <any other dbt command> [options...]           passed straight through to dbt
"""

from __future__ import annotations

import sys
from pathlib import Path

from dbt.cli.main import dbtRunner

from dbtx.docs.command import run as docs_command
from dbtx.runner import build, compile_project, exit_code
from dbtx.templates import TemplateError, TemplateRegistry

# Subcommands of `dbtx docs` that are ours; anything else under `docs`
# (generate) stays dbt's and is passed through untouched. `serve` is ours:
# it replaces dbt's single-threaded, cwd-mutating static server.
DOCS_SUBCOMMANDS = {"patch", "install", "status", "serve"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    commands = {"build": build, "compile": compile_project}
    if args and args[0] in commands and not {"-h", "--help"} & set(args):
        rest = args[1:]
        reports_enabled = "--no-reports" not in rest
        rest = [a for a in rest if a != "--no-reports"]
        return commands[args[0]](rest, reports_enabled=reports_enabled)

    if args and args[0] == "templates":
        return list_templates(args[1:])

    if args and args[0] == "docs":
        # Bare `dbtx docs`, or asking it for help, is a question about dbtx's
        # own docs commands -- so it is answered here rather than by dbt, whose
        # help lists only `generate` and `serve` and documents a `serve` that
        # is no longer the one this runs. A named subcommand dbtx does not own
        # still falls through untouched, help flags and all.
        if len(args) == 1 or args[1] in ("-h", "--help") or args[1] in DOCS_SUBCOMMANDS:
            return docs_command(args[1:])

    return exit_code(dbtRunner().invoke(args))


def list_templates(args: list[str]) -> int:
    project_dir = Path.cwd()
    if "--project-dir" in args:
        i = args.index("--project-dir")
        if i + 1 >= len(args):
            print("dbtx: --project-dir needs a value")
            return 2
        project_dir = Path(args[i + 1])
    try:
        registry = TemplateRegistry.for_project(project_dir)
    except (TemplateError, FileNotFoundError) as exc:
        print(f"dbtx: {exc}")
        return 2
    rows = [("NAME", "SOURCE", "URI"), *registry.rows()]
    widths = [max(len(r[i]) for r in rows) for i in range(2)]
    for name, source, uri in rows:
        print(f"{name:<{widths[0]}}  {source:<{widths[1]}}  {uri}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
