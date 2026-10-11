"""PostgreSQL, through psycopg 3 (`postgresql+psycopg`). Needs `dbtx[postgres]`."""

from __future__ import annotations

from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from dbtx.store import clock
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.enums import JobStatus
from dbtx.store.errors import StoreError
from dbtx.store.models import Job


class PostgresAdapter(StoreAdapter):
    dialect = "postgresql"
    display_name = "Postgres"
    driver = "psycopg"
    driver_package = "psycopg"
    driver_hint = "postgresql+psycopg:// (psycopg 3)"
    install_hint = "install dbtx[postgres]"
    supports_schemas = True
    minimum_server_version = (12,)
    minimum_server_reason = "the oldest supported PostgreSQL release dbtx is tested against"

    def create_engine(self, uri: str, schema: Optional[str]) -> sa.Engine:
        if schema is None:
            raise StoreError("Postgres needs a schema (DB_SCHEMA) for dbtx's tables")
        # pool_pre_ping: a pooled connection the server dropped is replaced, not used.
        engine = sa.create_engine(uri, pool_pre_ping=True)
        sa.event.listen(engine, "connect", self.on_connect)
        # Every table lives in DB_SCHEMA: the models have no schema of their own.
        return engine.execution_options(schema_translate_map={None: schema})

    def on_connect(self, dbapi_connection, connection_record) -> None:
        # Committed, so a later rollback cannot undo it. Ad-hoc SQL in the same session
        # sees UTC too; the timestamp type itself does not rely on it.
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.close()
        dbapi_connection.commit()

    def server_version(self, connection: sa.Connection) -> tuple[int, ...]:
        # Read by SQLAlchemy from the server when the engine first connected.
        return tuple(connection.dialect.server_version_info)

    def json_type(self) -> sa.types.TypeEngine:
        return JSONB(none_as_null=True)

    def insert(self, table: sa.Table):
        return postgresql_insert(table)

    def greatest(self, *columns):
        if len(columns) < 2:
            raise ValueError("greatest() needs two or more values")
        return sa.func.greatest(*columns, type_=columns[0].type)

    def least(self, *columns):
        if len(columns) < 2:
            raise ValueError("least() needs two or more values")
        return sa.func.least(*columns, type_=columns[0].type)

    def claim_next_job(self, session: Session):
        # FOR UPDATE SKIP LOCKED: a job another worker is claiming right now is locked,
        # so this one skips to the next job instead of waiting or taking the same one.
        statement = (
            sa.select(Job)
            .where(Job.status == JobStatus.PENDING.value)
            .order_by(Job.requested_at, Job.job_id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        job = session.scalars(statement).first()
        if job is None:
            return None
        job.status = JobStatus.RUNNING.value
        job.start_time = clock.now()
        session.flush()
        return job
