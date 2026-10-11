"""`dbtx` entry point.

    dbtx build [--no-reports] [dbt build options...]   build + render reports in one run
    dbtx compile [--no-reports] [dbt compile options...]  compile, validate reports, write their SQL
    dbtx templates [--project-dir DIR]                 list the templates a project can use
    dbtx docs patch|install|status [options...]        overlay run data onto generated dbt docs
    dbtx docs serve --docs-loc DIR [options...]        serve the generated docs over uvicorn
    dbtx config                                        show the resolved dbtx configuration
    dbtx db init | upgrade                             create or migrate dbtx's tables
    dbtx db status                                     report on the database
    dbtx db load --project-dir DIR [--run-loc DIR]     import a plain dbt run's artifacts
    dbtx db projects [rebind <name> --folder <folder> [--force]]
                                                       list or move project-name claims
    dbtx <any other dbt command> [options...]           passed straight through to dbt

Every command first loads and validates dbtx's configuration (dbtx.config); a problem
exits 2 before dbt starts. dbtx's own flags, accepted anywhere on the command line:

    --platform-dir DIR       run in platform mode with DIR as the platform root
    --standalone-mode        run without the platform (no config file, no database)
    --config-file PATH       use this config file instead of <platform root>/dbtx.conf
    --db-backend enabled|disabled
    --db-uri URI
    --db-schema NAME         the Postgres schema for dbtx's tables (default dbtx)
    --db-adapter NAME        the store adapter (default: the one for DB_URI)
"""

from __future__ import annotations

import sys
from pathlib import Path

from dbt.cli.main import dbtRunner

from dbtx.config import ConfigError, DbtxConfig, load_config
from dbtx.docs.command import run as docs_command
from dbtx.runner import build, compile_project, exit_code
from dbtx.templates import TemplateError, TemplateRegistry

# Subcommands of `dbtx docs` that are ours; anything else under `docs`
# (generate) stays dbt's and is passed through untouched. `serve` is ours:
# it replaces dbt's single-threaded, cwd-mutating static server.
DOCS_SUBCOMMANDS = {"patch", "install", "status", "serve"}

# Printed by `dbtx`, `dbtx -h` and `dbtx --help`, ahead of dbt's own help.
USAGE = """\
usage: dbtx <command> [options...]

dbtx commands:
  dbtx build [--no-reports] [dbt build options...]     build + render reports in one run
  dbtx compile [--no-reports] [dbt compile options...] compile, validate reports, write their SQL
  dbtx templates [--project-dir DIR]                   list the templates a project can use
  dbtx docs patch|install|status [options...]          overlay run data onto generated dbt docs
  dbtx docs serve --docs-loc DIR [options...]          serve the generated docs over uvicorn
  dbtx config                                          show the resolved dbtx configuration
  dbtx db init                                         create (Postgres: and its schema) or migrate dbtx's tables
  dbtx db upgrade                                      migrate dbtx's tables to this version
  dbtx db status                                       report on the database: revision, row counts
  dbtx db load --project-dir DIR [--run-loc DIR]       import a plain dbt run's artifacts
  dbtx db projects [rebind <name> --folder <folder> [--force]]
                                                       list or move project-name claims
  dbtx <any other dbt command> [options...]            passed straight through to dbt

dbtx options, accepted anywhere on the command line:
  --platform-dir DIR              run in platform mode with DIR as the platform root
  --standalone-mode               run without the platform (no config file, no database)
  --config-file PATH              use this config file instead of <platform root>/dbtx.conf
  --db-backend enabled|disabled   turn the database backend on or off
  --db-uri URI                    the database to use
  --db-schema NAME                the Postgres schema for dbtx's tables (default dbtx)
  --db-adapter NAME               the store adapter (default: the one for DB_URI)

`dbtx docs --help` lists the docs subcommands. dbt's help follows.

"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Configuration is checked before anything else, dbt included (plan 6.1).
    # Messages go to stderr so they never mix into output another tool parses,
    # such as `dbtx ls --output json`.
    try:
        config = load_config(args)
    except ConfigError as exc:
        print(f"dbtx: {exc}", file=sys.stderr)
        return 2
    for warning in config.warnings:
        print(f"dbtx: warning: {warning}", file=sys.stderr)

    # The database part of the start-up checks (plan 6.6 step 8): connect, server
    # version, tables, project-name guard. Also before dbt is ever started.
    from dbtx.store.errors import StoreError
    from dbtx.store.startup import run_startup_checks

    try:
        startup_warnings = run_startup_checks(config)
    except StoreError as exc:
        print(f"dbtx: {exc}", file=sys.stderr)
        return 2
    for warning in startup_warnings:
        print(f"dbtx: warning: {warning}", file=sys.stderr)
    args = list(config.argv)

    # Bare `dbtx`, or asking it for help, prints dbtx's own usage first; dbt's
    # help follows so the commands passed through to dbt are still listed.
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return exit_code(dbtRunner().invoke(["--help"]))

    commands = {"build": build, "compile": compile_project}
    if args and args[0] in commands and not {"-h", "--help"} & set(args):
        rest = args[1:]
        reports_enabled = "--no-reports" not in rest
        rest = [a for a in rest if a != "--no-reports"]
        return commands[args[0]](rest, reports_enabled=reports_enabled, config=config)

    if args and args[0] == "config":
        return show_config(config)

    if args and args[0] == "db":
        return db_command(config, args[1:])

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


def show_config(config: DbtxConfig) -> int:
    """`dbtx config`: the resolved configuration and where each value came from."""
    rows: list[tuple[str, str]] = []
    rows.append(("mode", f"{config.mode} ({config.mode_reason})"))
    rows.append(("platform root", str(config.platform_root) if config.platform_root else "none"))
    rows.append(("project dir", str(config.project_dir) if config.project_dir else "none"))
    if config.config_file is not None:
        rows.append(("config file", f"{config.config_file} ({config.config_file_reason})"))
    else:
        rows.append(("config file", config.config_file_reason))

    if config.is_platform:
        assert config.db_backend is not None and config.db_uri is not None
        rows.append(("DB_BACKEND", f"{config.db_backend.value}  [{config.db_backend.source}]"))
        if config.db_uri.value is None:
            rows.append(("DB_URI", "not set"))
        else:
            rows.append(("DB_URI", f"{config.masked_db_uri()}  [{config.db_uri.source}]"))
        assert config.db_schema is not None and config.db_adapter is not None
        resolved = config.resolved_adapter()
        if resolved is not None and not resolved[1].supports_schemas:
            rows.append(("DB_SCHEMA", f"n/a ({resolved[1].display_name} has no schemas)"))
        else:
            rows.append(("DB_SCHEMA", f"{config.db_schema.value}  [{config.db_schema.source}]"))
        if resolved is None and config.db_adapter.value is None:
            rows.append(("DB_ADAPTER", "not set (the default for DB_URI)"))
        elif resolved is None:
            rows.append(("DB_ADAPTER", f"{config.db_adapter.value}  [{config.db_adapter.source}]"))
        elif config.db_adapter.value is None:
            from sqlalchemy.engine import make_url

            url = make_url(config.db_uri.value)
            pair = f"{url.get_backend_name()}+{url.get_driver_name()}"
            rows.append(("DB_ADAPTER", f"{resolved[0]}  [default for {pair}]"))
        else:
            rows.append(("DB_ADAPTER", f"{resolved[0]}  [{config.db_adapter.source}]"))
    else:
        rows.append(("DB_BACKEND", "n/a (standalone mode has no database)"))
        rows.append(("DB_URI", "n/a (standalone mode has no database)"))
        rows.append(("DB_SCHEMA", "n/a (standalone mode has no database)"))
        rows.append(("DB_ADAPTER", "n/a (standalone mode has no database)"))

    for name in config.ignored_env_vars:
        rows.append(("ignored", f"{name} (standalone)"))

    width = max(len(label) for label, _ in rows)
    print("dbtx config")
    for label, value in rows:
        print(f"  {label:<{width}}  {value}")
    return 0


def db_command(config: DbtxConfig, args: list[str]) -> int:
    """`dbtx db ...`. Every subcommand needs platform mode."""
    if not config.is_platform:
        print("dbtx: database operations require platform mode", file=sys.stderr)
        return 2
    subcommand = args[0] if args else ""
    if subcommand == "init":
        from dbtx.store.cli import init_command

        return init_command(config, args[1:])
    if subcommand == "upgrade":
        from dbtx.store.cli import upgrade_command

        return upgrade_command(config, args[1:])
    if subcommand == "status":
        from dbtx.store.cli import status_command

        return status_command(config, args[1:])
    if subcommand == "projects":
        from dbtx.store.cli import projects_command

        return projects_command(config, args[1:])
    if subcommand == "load":
        from dbtx.store.cli import load_command

        return load_command(config, args[1:])
    from dbtx.store.cli import DB_USAGE

    if subcommand == "":
        print(f"dbtx: {DB_USAGE}", file=sys.stderr)
        return 2
    print(f"dbtx: unknown command `dbtx db {subcommand}`; {DB_USAGE}", file=sys.stderr)
    return 2


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
