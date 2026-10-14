"""SQLite, through the standard library driver (`sqlite+pysqlite`).

Concurrency: dbt's worker threads write at the same time (report renders), and pysqlite
starts transactions lazily, so two writers that both read first can deadlock when they
upgrade to a write lock. Following SQLAlchemy's recipe, pysqlite's own transaction
handling is switched off (`isolation_level = None`) and every transaction begins with
`BEGIN IMMEDIATE`, which takes the write lock up front; `busy_timeout` then queues
concurrent writers instead of failing with "database is locked".
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from dbtx.store import clock
from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.enums import JobStatus
from dbtx.store.errors import StoreError
from dbtx.store.models import Job

BUSY_TIMEOUT_MS = 5000


class SqliteAdapter(StoreAdapter):
    dialect = "sqlite"
    display_name = "SQLite"
    driver = "pysqlite"
    driver_package = None
    driver_hint = "sqlite:/// (the standard library driver)"
    install_hint = ""
    supports_schemas = False
    minimum_server_version = (3, 35)
    minimum_server_reason = "RETURNING needs 3.35"

    def create_engine(self, uri: str, schema: Optional[str]) -> sa.Engine:
        if schema is not None:
            raise StoreError(f"SQLite has no schemas; got schema {schema!r}")

        url = sa.make_url(uri)
        database = url.database
        if database is None or database == "" or database == ":memory:":
            # One in-memory database, shared by every connection (tests).
            engine = sa.create_engine(
                url,
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        else:
            if not database.startswith("file:"):
                parent = Path(database).parent
                try:
                    parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise StoreError(
                        f"cannot open the database {uri}: cannot create {parent}: "
                        f"{exc.strerror or exc}"
                    ) from None
            engine = sa.create_engine(url)

        sa.event.listen(engine, "connect", self.on_connect)
        sa.event.listen(engine, "begin", self.on_begin)
        return engine

    def database_file(self, uri: str) -> Optional[Path]:
        database = sa.make_url(uri).database
        if database is None or database == "" or database == ":memory:":
            return None
        if database.startswith("file:"):
            # A SQLite URI filename (`file:...?mode=memory&uri=true`): not a plain path.
            return None
        return Path(database)

    def on_connect(self, dbapi_connection, connection_record) -> None:
        # dbtx emits BEGIN itself (on_begin); pysqlite must not.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")  # stays "memory" for :memory:
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    def on_begin(self, connection: sa.Connection) -> None:
        """Every transaction takes the write lock when it begins."""
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    def server_version(self, connection: sa.Connection) -> tuple[int, ...]:
        # The SQLite library Python was built with, as this connection reports it.
        text = connection.exec_driver_sql("SELECT sqlite_version()").scalar()
        parts = []
        for part in text.split("."):
            parts.append(int(part))
        return tuple(parts)

    def insert(self, table: sa.Table):
        return sqlite_insert(table)

    def greatest(self, *columns):
        # SQLite's multi-argument max() is a scalar function; with one argument it
        # would be the aggregate, hence at least two.
        if len(columns) < 2:
            raise ValueError("greatest() needs two or more values")
        return sa.func.max(*columns, type_=columns[0].type)

    def least(self, *columns):
        if len(columns) < 2:
            raise ValueError("least() needs two or more values")
        return sa.func.min(*columns, type_=columns[0].type)

    def claim_next_job(self, session: Session):
        # One statement. Safe because every transaction began with BEGIN IMMEDIATE:
        # no other writer can run between choosing the job and claiming it.
        oldest_pending = (
            sa.select(Job.job_id)
            .where(Job.status == JobStatus.PENDING.value)
            .order_by(Job.requested_at, Job.job_id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            sa.update(Job)
            .where(Job.job_id == oldest_pending)
            .where(Job.status == JobStatus.PENDING.value)
            .values(status=JobStatus.RUNNING.value, start_time=clock.now())
            .returning(Job)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return session.scalars(statement).first()
