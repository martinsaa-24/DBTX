"""`dbtx db ...` commands (plan 6.7). Each needs platform mode (checked by `cli.main`).

Unlike the start-up checks of dbt commands, a `dbtx db` command always uses the
database when it runs: that is its whole job.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from dbtx.config import DBT_PROJECT_FILE_NAME, ENV_DB_URI
from dbtx.store import Store, open_store
from dbtx.store.errors import StoreError
from dbtx.store.repositories.projects import list_claims, rebind_project
from dbtx.store.startup import check_database

if TYPE_CHECKING:
    from dbtx.config import DbtxConfig

PROJECTS_USAGE = "usage: dbtx db projects [rebind <name> --folder <folder> [--force]]"


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
        check_database(store, config.command)
    except StoreError:
        store.dispose()
        raise
    return store


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
