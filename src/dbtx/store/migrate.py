"""Migrations: dbtx's Alembic revisions, run programmatically (plan D4, Phase 3).

The revisions ship in `dbtx/store/migrations/`. dbtx hands Alembic an open connection
(see `migrations/env.py`), so no `alembic.ini` is needed.

    with store.engine.connect() as connection:
        status = revision_status(connection, store.schema)   # current / behind / ...
    before, after = upgrade(store)                            # to head
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError

from dbtx.store.errors import StoreError

if TYPE_CHECKING:
    from dbtx.store import Store

# Where the revisions live: `package:directory`. Tests point it at a copy.
SCRIPT_LOCATION = "dbtx.store:migrations"

# RevisionStatus.state
UNINITIALISED = "uninitialised"  # no revision: dbtx's tables were never created
CURRENT = "current"  # at head
BEHIND = "behind"  # an older revision this dbtx knows: upgrade
UNKNOWN = "unknown"  # a revision this dbtx does not know: migrated by a newer dbtx


@dataclass(frozen=True)
class RevisionStatus:
    """The database's revision (None: no revision) against this dbtx's head."""

    current: Optional[str]
    head: str
    state: str


def alembic_config(connection: sa.Connection, schema: Optional[str]) -> Config:
    """An Alembic config that runs the migrations on `connection`. `schema` is
    DB_SCHEMA (where the version table lives), or None for a backend without schemas."""
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.set_main_option("path_separator", "os")
    config.attributes["connection"] = connection
    config.attributes["schema"] = schema
    return config


def script_directory() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    config.set_main_option("path_separator", "os")
    return ScriptDirectory.from_config(config)


def head_revision() -> str:
    """The newest revision this dbtx ships."""
    return script_directory().get_current_head()


def current_revision(connection: sa.Connection, schema: Optional[str]) -> Optional[str]:
    """The database's revision, or None when it has none (no version table, or empty)."""
    context = MigrationContext.configure(connection, opts={"version_table_schema": schema})
    return context.get_current_revision()


def revision_status(connection: sa.Connection, schema: Optional[str]) -> RevisionStatus:
    """The database's revision against head."""
    script = script_directory()
    head = script.get_current_head()
    current = current_revision(connection, schema)
    if current is None:
        return RevisionStatus(current=None, head=head, state=UNINITIALISED)
    if current == head:
        return RevisionStatus(current=current, head=head, state=CURRENT)
    try:
        script.get_revision(current)
    except CommandError:
        return RevisionStatus(current=current, head=head, state=UNKNOWN)
    return RevisionStatus(current=current, head=head, state=BEHIND)


def unknown_revision_message(store: Store, status: RevisionStatus) -> str:
    return (
        f"the database {store.masked_uri()} is at revision {status.current!r}, which this "
        f"dbtx does not know (head is {status.head!r}); it was migrated by a newer dbtx"
    )


def schema_exists(store: Store) -> bool:
    """Whether DB_SCHEMA exists. Only for an adapter that `supports_schemas`."""
    assert store.schema is not None
    with store.engine.connect() as connection:
        return sa.inspect(connection).has_schema(store.schema)


def create_schema(store: Store) -> None:
    """Create DB_SCHEMA. StoreError when the server refuses (no privilege)."""
    from dbtx.store.startup import error_reason

    assert store.schema is not None
    try:
        with store.engine.begin() as connection:
            connection.execute(sa.schema.CreateSchema(store.schema))
    except sa.exc.SQLAlchemyError as exc:
        raise StoreError(
            f"cannot create the schema {store.schema!r} in {store.masked_uri()}: "
            f"{error_reason(exc)}; create it (or ask an administrator to), then run "
            f"`dbtx db init` again"
        ) from None


def upgrade(store: Store) -> tuple[Optional[str], str]:
    """Migrate the store to head, in one transaction. Returns (revision before, revision
    after). StoreError if the database is at a revision this dbtx does not know."""
    with store.engine.begin() as connection:
        status = revision_status(connection, store.schema)
        if status.state == UNKNOWN:
            raise StoreError(unknown_revision_message(store, status))
        command.upgrade(alembic_config(connection, store.schema), "head")
        after = current_revision(connection, store.schema)
    assert after is not None
    return status.current, after
