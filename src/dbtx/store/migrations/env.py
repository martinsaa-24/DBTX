"""Alembic environment for dbtx's store (plan D4).

dbtx runs its migrations programmatically (`dbtx.store.migrate`): it opens the store's
engine, starts a transaction and hands the connection over in `config.attributes`.
There is no `alembic.ini`, and the `alembic` command line is not supported.
"""

from __future__ import annotations

from alembic import context

from dbtx.store import models as m

config = context.config

if context.is_offline_mode():
    raise RuntimeError("dbtx's migrations run against a database (dbtx db init / upgrade)")

connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError(
        "dbtx's migrations are run by dbtx (dbtx db init / upgrade), not by the alembic "
        "command line"
    )

context.configure(
    connection=connection,
    target_metadata=m.Base.metadata,
    # Postgres: the version table lives in DB_SCHEMA with the store's tables (which the
    # engine's schema_translate_map puts there). None for a backend without schemas.
    version_table_schema=config.attributes.get("schema"),
    # SQLite cannot ALTER constraints: later migrations rebuild tables in batch mode.
    render_as_batch=True,
    compare_type=True,
)

with context.begin_transaction():
    context.run_migrations()
