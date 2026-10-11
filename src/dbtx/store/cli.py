"""`dbtx db ...` commands (plan 6.7): `init`, `upgrade`, `status`, `load`, `projects`. Each
needs platform mode (checked by `cli.main`).

Unlike the start-up checks of dbt commands, a `dbtx db` command always uses the
database when it runs: that is its whole job.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
import yaml
from sqlalchemy.orm import Session

from dbtx.config import DBT_PROJECT_FILE_NAME, ENV_DB_URI, project_folders
from dbtx.reports import ReportDefinitionError, discover_reports
from dbtx.store import Store, migrate
from dbtx.store import models as m
from dbtx.store import open_store
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.errors import StoreError
from dbtx.store.loaders.manifest import ManifestRows, manifest_rows, read_manifest
from dbtx.store.loaders.run_results import (
    InvocationRecord,
    freshness_result_rows,
    invocation_from_run_results,
    invocation_from_sources,
    run_result_rows,
)
from dbtx.store.repositories import invocations
from dbtx.store.repositories import reports as report_repo
from dbtx.store.repositories import results
from dbtx.store.repositories.nodes import sync_manifest
from dbtx.store.repositories.projects import list_claims, rebind_project
from dbtx.store.startup import check_database, check_project_claim, error_reason
from dbtx.templates import TemplateEngine, TemplateError, TemplateRegistry

if TYPE_CHECKING:
    from dbtx.config import DbtxConfig

PROJECTS_USAGE = "usage: dbtx db projects [rebind <name> --folder <folder> [--force]]"
DB_USAGE = (
    "usage: dbtx db init | upgrade | status | load | projects [rebind <name> --folder "
    "<folder> [--force]]"
)
LOAD_USAGE = "usage: dbtx db load --project-dir DIR [--run-loc DIR]"


def open_checked_store(config: DbtxConfig) -> Store:
    """The store for a `dbtx db` command: configured, reachable, with dbtx's tables."""
    command = f"dbtx {config.command}"
    assert config.db_backend is not None and config.db_uri is not None
    if not config.backend_enabled:
        raise StoreError(
            f"`{command}` needs the database backend, which is disabled "
            f"(DB_BACKEND=disabled from {config.db_backend.source})"
        )
    if config.db_uri.value is None:
        raise StoreError(
            f"`{command}` needs a database: set DB_URI (with --db-uri, DB_URI= in "
            f"{config.config_file}, or {ENV_DB_URI})"
        )
    store = open_store(config)
    try:
        warnings = check_database(store, config.command)
    except StoreError:
        store.dispose()
        raise
    for warning in warnings:
        print(f"dbtx: warning: {warning}", file=sys.stderr)
    return store


# --------------------------------------------------------------------------------------
# dbtx db init / upgrade
# --------------------------------------------------------------------------------------


def init_command(config: DbtxConfig, args: list[str]) -> int:
    """`dbtx db init`: create DB_SCHEMA when missing (Postgres), then migrate to head."""
    if len(args) != 0:
        print(f"dbtx: `dbtx db init` takes no arguments; {DB_USAGE}", file=sys.stderr)
        return 2
    return run_and_report(init, config)


def init(config: DbtxConfig) -> int:
    store = open_checked_store(config)
    try:
        if store.adapter.supports_schemas and not migrate.schema_exists(store):
            migrate.create_schema(store)
            print(f"created schema {store.schema!r}")
        before, after = migrate.upgrade(store)
        print_upgrade(store, before, after)
    finally:
        store.dispose()
    return 0


def upgrade_command(config: DbtxConfig, args: list[str]) -> int:
    """`dbtx db upgrade`: migrate to head."""
    if len(args) != 0:
        print(f"dbtx: `dbtx db upgrade` takes no arguments; {DB_USAGE}", file=sys.stderr)
        return 2
    return run_and_report(upgrade, config)


def upgrade(config: DbtxConfig) -> int:
    store = open_checked_store(config)
    try:
        if store.adapter.supports_schemas and not migrate.schema_exists(store):
            raise StoreError(
                f"the schema {store.schema!r} does not exist in {store.masked_uri()}; "
                f"run `dbtx db init`"
            )
        before, after = migrate.upgrade(store)
        print_upgrade(store, before, after)
    finally:
        store.dispose()
    return 0


def print_upgrade(store: Store, before: Optional[str], after: str) -> None:
    if before is None:
        print(f"created dbtx's tables in {store.masked_uri()} (revision {after})")
    elif before == after:
        print(f"{store.masked_uri()} is already at revision {after} (head)")
    else:
        print(f"upgraded {store.masked_uri()} from revision {before} to {after}")


# --------------------------------------------------------------------------------------
# dbtx db status
# --------------------------------------------------------------------------------------


def status_command(config: DbtxConfig, args: list[str]) -> int:
    """`dbtx db status`: report on the database; never change it."""
    if len(args) != 0:
        print(f"dbtx: `dbtx db status` takes no arguments; {DB_USAGE}", file=sys.stderr)
        return 2
    return run_and_report(status, config)


def status(config: DbtxConfig) -> int:
    assert config.db_backend is not None and config.db_uri is not None
    assert config.db_schema is not None and config.db_adapter is not None
    rows: list[tuple[str, str]] = []
    rows.append(("DB_BACKEND", f"{config.db_backend.value}  [{config.db_backend.source}]"))
    if config.db_uri.value is None:
        rows.append(("DB_URI", "not set"))
        print_rows(rows)
        return 0
    rows.append(("DB_URI", f"{config.masked_db_uri()}  [{config.db_uri.source}]"))
    if not config.backend_enabled:
        print_rows(rows)
        return 0

    resolved = config.resolved_adapter()
    assert resolved is not None
    adapter_name, adapter_class = resolved
    adapter = adapter_class()
    if adapter.supports_schemas:
        rows.append(("DB_SCHEMA", f"{config.db_schema.value}  [{config.db_schema.source}]"))
    if config.db_adapter.value is None:
        url = sa.make_url(config.db_uri.value)
        pair = f"{url.get_backend_name()}+{url.get_driver_name()}"
        rows.append(("adapter", f"{adapter_name}  [default for {pair}]"))
    else:
        rows.append(("adapter", f"{adapter_name}  [{config.db_adapter.source}]"))

    warnings: list[str] = []
    database_file = adapter.database_file(config.db_uri.value)
    if database_file is not None:
        warnings.extend(clean_target_warnings(config, adapter.display_name, database_file))
        if not database_file.exists():
            # Opening the store would create the file (and its directory).
            rows.append(
                (
                    "database",
                    f"{database_file} does not exist yet; the first run or `dbtx db init` "
                    f"creates it",
                )
            )
            print_rows(rows)
            print_warnings(warnings)
            return 0

    projects: list[str] = []
    store = open_store(config)
    try:
        try:
            connection = store.engine.connect()
        except sa.exc.SQLAlchemyError as exc:
            print_rows(rows)
            raise StoreError(
                f"cannot connect to the database {store.masked_uri()}: {error_reason(exc)}"
            ) from None
        with connection:
            version = store.adapter.server_version(connection)
            version_text = ".".join(str(part) for part in version)
            rows.append(("server", f"{store.adapter.display_name} {version_text}"))
            revision = migrate.revision_status(connection, store.schema)
            rows.append(("revision", revision_text(revision)))
            if revision.state == migrate.CURRENT:
                counts = []
                for table in m.Base.metadata.sorted_tables:
                    count = connection.execute(
                        sa.select(sa.func.count()).select_from(table)
                    ).scalar_one()
                    counts.append(f"{table.name} {count}")
                rows.append(("tables", ", ".join(counts)))
                projects = list(
                    connection.execute(
                        sa.select(m.Project.project).order_by(m.Project.project)
                    ).scalars()
                )
    finally:
        store.dispose()

    if database_file is not None and len(projects) > 1:
        warnings.append(
            f"this {adapter.display_name} store holds {len(projects)} projects "
            f"({', '.join(projects)}); one database file suits one host: projects that "
            f"build at the same time, or on several hosts, should share a Postgres database"
        )
    print_rows(rows)
    print_warnings(warnings)
    return 0


def revision_text(revision: migrate.RevisionStatus) -> str:
    if revision.state == migrate.CURRENT:
        return f"{revision.current} (head)"
    if revision.state == migrate.UNINITIALISED:
        return "none; run `dbtx db init`"
    if revision.state == migrate.BEHIND:
        return f"{revision.current}; head is {revision.head}, run `dbtx db upgrade`"
    return (
        f"{revision.current!r}, which this dbtx does not know (head is {revision.head!r}); "
        f"it was migrated by a newer dbtx"
    )


def clean_target_warnings(config: DbtxConfig, display_name: str, database_file: Path) -> list[str]:
    """A warning for each platform project whose dbt clean target holds the database
    file: `dbt clean` would delete it. dbt's default clean target is its target path."""
    assert config.platform_root is not None
    warnings = []
    database = database_file.resolve()
    for folder in project_folders(config.platform_root):
        project = config.platform_root / folder
        try:
            data = yaml.safe_load((project / DBT_PROJECT_FILE_NAME).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        targets = data.get("clean-targets")
        if targets is None:
            targets = [data.get("target-path") or "target"]
        if not isinstance(targets, list):
            continue
        for target in targets:
            if not isinstance(target, str):
                continue
            clean_dir = (project / target).resolve()
            if database.is_relative_to(clean_dir):
                warnings.append(
                    f"the {display_name} store {database_file} is inside {clean_dir}, a clean "
                    f"target of the project in {folder}: `dbt clean` deletes it"
                )
    return warnings


def print_rows(rows: list[tuple[str, str]]) -> None:
    width = max(len(label) for label, _ in rows)
    print("dbtx db status")
    for label, value in rows:
        print(f"  {label:<{width}}  {value}")


def print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        print(f"dbtx: warning: {warning}", file=sys.stderr)


# --------------------------------------------------------------------------------------
# dbtx db load
# --------------------------------------------------------------------------------------


def load_command(config: DbtxConfig, args: list[str]) -> int:
    """`dbtx db load --project-dir DIR [--run-loc DIR]`: import a run's artifacts."""
    valid, run_loc = parse_load_args(args)
    if not valid:
        print(f"dbtx: {LOAD_USAGE}", file=sys.stderr)
        return 2
    return run_and_report(load_artifacts, config, run_loc)


def parse_load_args(args: list[str]) -> tuple[bool, Optional[str]]:
    """(valid, --run-loc or None). `--project-dir` is accepted and skipped: the
    configuration has already resolved it."""
    run_loc = None
    i = 0
    while i < len(args):
        token = args[i]
        if token in ("--project-dir", "--run-loc"):
            if i + 1 >= len(args) or args[i + 1].startswith("-"):
                return False, None
            if token == "--run-loc":
                run_loc = args[i + 1]
            i += 2
            continue
        if token.startswith("--project-dir="):
            i += 1
            continue
        if token.startswith("--run-loc="):
            run_loc = token[len("--run-loc=") :]
            if run_loc == "":
                return False, None
            i += 1
            continue
        return False, None
    return True, run_loc


def load_artifacts(config: DbtxConfig, run_loc_text: Optional[str]) -> int:
    assert config.project_dir is not None
    project_dir = config.project_dir
    if run_loc_text is None:
        run_loc = project_dir / "target"
    else:
        run_loc = Path(run_loc_text)
        if not run_loc.is_absolute():
            run_loc = Path.cwd() / run_loc

    manifest_path = run_loc / "manifest.json"
    if not manifest_path.is_file():
        raise StoreError(f"{run_loc} has no manifest.json")
    run_results_path = run_loc / "run_results.json"
    sources_path = run_loc / "sources.json"
    if not run_results_path.is_file() and not sources_path.is_file():
        raise StoreError(f"{run_loc} has no run_results.json or sources.json; nothing to load")

    try:
        manifest = read_manifest(manifest_path)
    except Exception as exc:  # dbt's schema-version and parse errors
        raise StoreError(f"cannot read {manifest_path}: {exc}") from None
    rows = manifest_rows(manifest)
    if rows.project != config.project_name:
        raise StoreError(
            f"{manifest_path} belongs to project {rows.project!r}, but {project_dir.name} is "
            f"project {config.project_name!r}"
        )
    run_results = read_json(run_results_path)
    sources = read_json(sources_path)

    warnings: list[str] = []
    try:
        reports = discover_reports(manifest, TemplateEngine(TemplateRegistry.for_project(project_dir)), project_dir)
    except (ReportDefinitionError, TemplateError) as exc:
        reports = None
        warnings.append(f"reports not loaded: {exc}")

    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    lines = []
    store = open_checked_store(config)
    try:
        warnings.extend(check_project_claim(store, config))
        with store.begin() as session:
            counts = sync_manifest(session, store.adapter, rows)
            lines.append(
                f"loaded the manifest of {rows.project} from {run_loc}: {counts.nodes} nodes, "
                f"{counts.versions} versions, {counts.edges} edges, {counts.removed_nodes} newly removed"
            )
            if reports is not None:
                stored, removed = report_repo.sync(session, store.adapter, rows.project, reports, rows.seen_at)
                line = f"loaded {stored} reports"
                if removed > 0:
                    line += f", {removed} newly removed"
                lines.append(line)
            checksums = {}
            for node in rows.nodes:
                checksums[node["unique_id"]] = node["checksum"]
            if run_results is not None:
                line = load_invocation(
                    session,
                    store.adapter,
                    rows,
                    "run_results.json",
                    invocation_from_run_results(run_results),
                    run_result_rows(run_results, rows.project, checksums),
                    manifest_hash,
                    manifest.metadata.invocation_id,
                    warnings,
                )
                if line is not None:
                    lines.append(line)
            if sources is not None:
                line = load_invocation(
                    session,
                    store.adapter,
                    rows,
                    "sources.json",
                    invocation_from_sources(sources),
                    freshness_result_rows(sources, rows.project),
                    manifest_hash,
                    manifest.metadata.invocation_id,
                    warnings,
                )
                if line is not None:
                    lines.append(line)
    finally:
        store.dispose()

    for line in lines:
        print(line)
    print_warnings(warnings)
    return 0


def read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StoreError(f"cannot read {path}: {exc}") from None


def load_invocation(
    session: Session,
    adapter: StoreAdapter,
    rows: ManifestRows,
    artifact: str,
    record: InvocationRecord,
    result_rows: list[dict],
    manifest_hash: str,
    manifest_invocation_id: Optional[str],
    warnings: list[str],
) -> Optional[str]:
    """Store one artifact's invocation and node results. Returns the line to print, or
    None (with a warning) for a command dbtx does not record."""
    if record.command is None:
        which = record.args.get("which", "?")
        warnings.append(f"{artifact} is from `dbt {which}`, which dbtx does not record")
        return None
    invocations.start(
        session, adapter, project=rows.project, adapter_type=rows.adapter_type, record=record, run_by=None, host=None
    )
    stored, skipped = results.load(session, adapter, rows.project, result_rows)
    result = invocations.result_from_statuses(row["status"] for row in result_rows)
    # The manifest was written by the invocation named in its metadata (plain dbt).
    this_manifest = manifest_hash if record.invocation_id == manifest_invocation_id else None
    invocations.finish(
        session,
        record.invocation_id,
        end_time=record.end_time,
        result=result,
        error_message=None,
        manifest_hash=this_manifest,
    )
    line = f"loaded invocation {record.invocation_id} ({record.command}, {result}): {stored} node results"
    if skipped > 0:
        line += f", {skipped} skipped (no stored node)"
    return line


# --------------------------------------------------------------------------------------
# dbtx db projects
# --------------------------------------------------------------------------------------


def projects_command(config: DbtxConfig, args: list[str]) -> int:
    """`dbtx db projects [rebind <name> --folder <folder> [--force]]`."""
    if len(args) == 0:
        return run_and_report(list_projects, config)
    if args[0] == "rebind":
        parsed = parse_rebind_args(args[1:])
        if parsed is None:
            print(f"dbtx: {PROJECTS_USAGE}", file=sys.stderr)
            return 2
        name, folder, force = parsed
        if not is_a_folder_name(folder):
            print(
                f"dbtx: --folder {folder!r} is not a folder name; give the project folder's "
                f"name under the platform root",
                file=sys.stderr,
            )
            return 2
        return run_and_report(rebind, config, name, folder, force)
    print(f"dbtx: {PROJECTS_USAGE}", file=sys.stderr)
    return 2


def run_and_report(function, *args) -> int:
    """Run a command body; a StoreError is printed and becomes exit 2."""
    try:
        return function(*args)
    except StoreError as exc:
        print(f"dbtx: {exc}", file=sys.stderr)
        return 2


def list_projects(config: DbtxConfig) -> int:
    store = open_checked_store(config)
    try:
        with store.session() as session:
            claims = list_claims(session)
    finally:
        store.dispose()

    if len(claims) == 0:
        print("no project names are claimed in this database")
        return 0

    rows = [("PROJECT", "FOLDER", "CLAIMED AT", "BY HOST", "LAST SEEN AT")]
    for claim in claims:
        rows.append(
            (
                claim.project,
                claim.folder,
                format_time(claim.claimed_at),
                claim.claimed_by_host or "-",
                format_time(claim.last_seen_at),
            )
        )
    widths = []
    for column in range(4):
        widths.append(max(len(row[column]) for row in rows))
    for row in rows:
        print(
            f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  "
            f"{row[3]:<{widths[3]}}  {row[4]}"
        )
    return 0


def rebind(config: DbtxConfig, name: str, folder: str, force: bool) -> int:
    assert config.platform_root is not None
    store = open_checked_store(config)
    try:
        with store.begin() as session:
            before, after = rebind_project(session, name, folder)
            if before.folder == after.folder:
                print(f"project name {name!r} is already owned by folder {folder!r}")
                return 0
            if not force:
                old_folder = config.platform_root / before.folder
                if old_folder.exists():
                    raise StoreError(
                        f"folder {before.folder!r} still exists under "
                        f"{config.platform_root}; move or remove it first, or pass --force"
                    )
                new_folder = config.platform_root / folder
                if not (new_folder / DBT_PROJECT_FILE_NAME).is_file():
                    raise StoreError(
                        f"{new_folder} is not a project folder (no {DBT_PROJECT_FILE_NAME}); "
                        f"check the name, or pass --force"
                    )
    finally:
        store.dispose()
    print(f"moved project name {name!r} from folder {before.folder!r} to {after.folder!r}")
    return 0


def parse_rebind_args(args: list[str]) -> Optional[tuple[str, str, bool]]:
    """(name, folder, force) from `<name> --folder <folder> [--force]`, or None."""
    name = None
    folder = None
    force = False
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--force":
            force = True
            i += 1
            continue
        if token == "--folder" or token.startswith("--folder="):
            if folder is not None:
                return None
            if token == "--folder":
                if i + 1 >= len(args) or args[i + 1].startswith("-"):
                    return None
                folder = args[i + 1]
                i += 2
            else:
                folder = token[len("--folder=") :]
                i += 1
            if folder == "":
                return None
            continue
        if token.startswith("-"):
            return None
        if name is not None:
            return None
        name = token
        i += 1
    if name is None or folder is None:
        return None
    return name, folder, force


def is_a_folder_name(folder: str) -> bool:
    """A plain folder name: one path component, not `.` or `..`."""
    if folder in (".", ".."):
        return False
    if "/" in folder or "\\" in folder:
        return False
    return Path(folder).name == folder


def format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")
