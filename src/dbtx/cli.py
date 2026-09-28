"""`dbtx` entry point.

    dbtx build [--no-reports] [dbt build options...]   build + render reports in one run
    dbtx compile [--no-reports] [dbt compile options...]  compile, validate reports, write their SQL
    dbtx templates [--project-dir DIR]                 list the templates a project can use
    dbtx <any other dbt command> [options...]           passed straight through to dbt
"""

from __future__ import annotations

import sys
from pathlib import Path

from dbt.cli.main import dbtRunner

from dbtx.runner import build, compile_project, exit_code
from dbtx.templates import TemplateError, TemplateRegistry


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
