"""Start-up checks: the database part of plan 6.6 step 8, run before dbt starts.

Gates, all required, else nothing is opened or connected to:
* platform mode, backend enabled, a command that uses the database, DB_URI set
* `ENFORCE_DB_REQUIREMENTS` (until plan Phase 5): while nothing is recorded, a dbt
  command makes no connection at all

Checks, in order, each a StoreError (exit 2) on failure:
1. connect
2. the server version floor (`adapter.check_server`)
3. the migration revision against head (plan Phase 3): unknown → StoreError; none or
   behind → upgraded when the adapter `upgrades_automatically` (SQLite), else a
   StoreError naming `dbtx db init` / `upgrade`. Those two commands skip this check:
   fixing the revision is their job
4. for a command with a project: guards G1 + G2 in one transaction. A name another
   folder on this platform also declares (G1) runs only from the folder that holds the
   claim ("established owner wins"); otherwise the folder claims the name or confirms
   its claim (G2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from dbtx import config as dbtx_config
from dbtx.store import Store, migrate, open_store
from dbtx.store.errors import StoreError
from dbtx.store.repositories.projects import claim_project, find_claim

if TYPE_CHECKING:
    from dbtx.config import DbtxConfig

# Commands whose job is to create or upgrade the tables, so they skip the tables check.
COMMANDS_THAT_CREATE_THE_TABLES = ("db init", "db upgrade")


def run_startup_checks(config: DbtxConfig) -> list[str]:
    """Connect, check the server and the tables, then run guards G1 + G2.

    Returns warnings to print. Raises StoreError, which `cli.main` turns into exit 2.
    """
    if not config.is_platform:
        return []
    if not config.backend_enabled:
        return []
    if not config.uses_database:
        return []
    if config.db_uri is None or config.db_uri.value is None:
        return []
    if not dbtx_config.ENFORCE_DB_REQUIREMENTS:
        return []

    store = open_store(config)
    try:
        warnings = check_database(store, config.command)
        try:
            warnings.extend(check_project_claim(store, config))
            return warnings
        except sa.exc.SQLAlchemyError as exc:
            raise StoreError(
                f"the database {store.masked_uri()} failed during the start-up checks: "
                f"{error_reason(exc)}"
            ) from None
    finally:
        store.dispose()


def check_database(store: Store, command: str) -> list[str]:
    """Steps 1-3: connect, the server version floor, the migration revision.

    A database with no revision, or behind head, is upgraded when the adapter
    `upgrades_automatically` (SQLite); otherwise it is a StoreError naming the command
    that fixes it. Returns warnings to print (an upgrade made).
    """
    try:
        connection = store.engine.connect()
    except sa.exc.SQLAlchemyError as exc:
        raise StoreError(
            f"cannot connect to the database {store.masked_uri()}: {error_reason(exc)}"
        ) from None

    with connection:
        store.adapter.check_server(connection)
        if command in COMMANDS_THAT_CREATE_THE_TABLES:
            return []
        status = migrate.revision_status(connection, store.schema)
    # The connection is closed before upgrading: on SQLite its transaction would hold
    # the write lock the upgrade needs.

    if status.state == migrate.CURRENT:
        return []
    if status.state == migrate.UNKNOWN:
        raise StoreError(migrate.unknown_revision_message(store, status))

    if not store.adapter.upgrades_automatically:
        if status.state == migrate.UNINITIALISED:
            raise StoreError(
                f"the database {store.masked_uri()} has no dbtx tables; run `dbtx db init`"
            )
        raise StoreError(
            f"the database {store.masked_uri()} is at revision {status.current}, behind "
            f"this dbtx's {status.head}; run `dbtx db upgrade`"
        )

    before, after = migrate.upgrade(store)
    if before is None:
        return [f"created dbtx's tables in {store.masked_uri()} (revision {after})"]
    return [f"upgraded {store.masked_uri()} from revision {before} to {after}"]


def check_project_claim(store: Store, config: DbtxConfig) -> list[str]:
    """Step 4: guards G1 + G2 for the project this command runs. Returns warnings."""
    if config.project_dir is None or config.project_name is None:
        # No project, or a dbt_project.yml without a usable name (dbt reports that).
        return []

    name = config.project_name
    folder = config.project_dir.name
    warnings = []

    with store.begin() as session:
        if config.colliding_folders:
            others = []
            for other in config.colliding_folders:
                if other != folder:
                    others.append(other)
            claim = find_claim(session, name)
            if claim is None:
                assert config.platform_root is not None
                raise StoreError(
                    dbtx_config.project_name_collision_message(
                        name, config.colliding_folders, config.platform_root
                    )
                )
            if claim.folder == folder:
                warnings.append(
                    f"project name {name!r} is also declared by {', '.join(others)} under "
                    f"{config.platform_root}; this folder ({folder}) owns the name in the "
                    f"database, so {', '.join(others)} will be refused"
                )
            # Otherwise another folder holds the claim: claim_project refuses (G2).

        result = claim_project(session, store.adapter, name, folder)
        warnings.extend(result.warnings)

    return warnings


def error_reason(exc: sa.exc.SQLAlchemyError) -> str:
    """The driver's message, first line only (no SQL, no parameters)."""
    original = getattr(exc, "orig", None)
    text = str(original) if original is not None else str(exc)
    lines = text.strip().splitlines()
    if not lines:
        return type(exc).__name__
    return lines[0]
