"""The adapter interface: every difference between database backends lives here.

Repositories and the start-up checks never branch on the dialect. They ask the adapter
(plan section 4, decision D2). Adding a backend, or another adapter for an existing
one, means adding one `StoreAdapter` subclass and registering it under a name
(`dbtx.store.adapters`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Optional, Union

import sqlalchemy as sa
from sqlalchemy.orm import Session

from dbtx.store.errors import StoreError


class StoreAdapter(ABC):
    # The SQLAlchemy backend name this adapter serves (`make_url(uri).get_backend_name()`).
    dialect: ClassVar[str]
    # How the backend is named in messages ("SQLite", "Postgres").
    display_name: ClassVar[str]
    # The DBAPI driver(s) this adapter supports: one name, or a list of names. Every
    # other driver is refused.
    driver: ClassVar[Union[str, list[str]]]
    # The module each driver needs installed: one module for every driver, or a dict
    # with an entry per driver. None when the driver ships with Python.
    driver_package: ClassVar[Union[Optional[str], dict[str, Optional[str]]]]
    # What to write in DB_URI instead of another driver, and how to install the driver.
    driver_hint: ClassVar[str]
    install_hint: ClassVar[str]
    # Whether DB_SCHEMA applies (Postgres has schemas, SQLite does not).
    supports_schemas: ClassVar[bool]
    # The oldest server version dbtx works with, and why.
    minimum_server_version: ClassVar[tuple[int, ...]]
    minimum_server_reason: ClassVar[str]

    # ----------------------------------------------------------------------------------
    # Engines and connections
    # ----------------------------------------------------------------------------------

    @abstractmethod
    def create_engine(self, uri: str, schema: Optional[str]) -> sa.Engine:
        """An engine for `uri`, with `on_connect` hooked up. `schema` is DB_SCHEMA,
        or None for a backend without schemas."""

    @abstractmethod
    def on_connect(self, dbapi_connection, connection_record) -> None:
        """Settings applied to every new DBAPI connection (pragmas, time zone)."""

    @abstractmethod
    def server_version(self, connection: sa.Connection) -> tuple[int, ...]:
        """The server's version, as a tuple of integers."""

    def check_server(self, connection: sa.Connection) -> None:
        """Raise StoreError if the server is older than `minimum_server_version`."""
        found = self.server_version(connection)
        if tuple(found) >= tuple(self.minimum_server_version):
            return
        found_text = ".".join(str(part) for part in found)
        minimum_text = ".".join(str(part) for part in self.minimum_server_version)
        raise StoreError(
            f"{self.display_name} {found_text} is too old: dbtx needs {self.display_name} "
            f"{minimum_text} or newer ({self.minimum_server_reason})"
        )

    # ----------------------------------------------------------------------------------
    # Column types
    # ----------------------------------------------------------------------------------

    def json_type(self) -> sa.types.TypeEngine:
        """The storage type behind `JSONType` on this backend.

        Generic JSON by default. Override it for a backend with a better JSON type
        (Postgres: JSONB). Keep `none_as_null=True`: Python None must be SQL NULL.
        """
        return sa.JSON(none_as_null=True)

    # ----------------------------------------------------------------------------------
    # SQL constructs
    # ----------------------------------------------------------------------------------

    @abstractmethod
    def insert(self, table: sa.Table):
        """The dialect's `insert()`, which supports `on_conflict_do_update`."""

    @abstractmethod
    def greatest(self, *columns):
        """The largest of two or more values (not an aggregate)."""

    @abstractmethod
    def least(self, *columns):
        """The smallest of two or more values (not an aggregate)."""

    def upsert(self, table: sa.Table, rows: list[dict], *, keys, update, merge=None):
        """INSERT `rows`; on a key conflict, update the stored row instead.

        * `keys`: the primary key columns (the conflict target)
        * `update`: columns that take the new value
        * `merge`: {column: rule} for columns that combine the stored and the new value:
          `least` (keep the smaller), `greatest` (keep the larger), `clear` (set NULL)

        Every other column keeps its stored value. With no `update` and no `merge`, an
        existing row is left alone. Returns the statement; the caller executes it. Bad
        arguments raise ValueError before anything runs, so a mistake can never write
        different data on different backends.
        """
        if merge is None:
            merge = {}
        table_name = table.name

        if len(rows) == 0:
            raise ValueError(f"upsert into {table_name}: no rows")

        primary_key = []
        for column in table.primary_key.columns:
            primary_key.append(column.name)
        if sorted(keys) != sorted(primary_key):
            raise ValueError(
                f"upsert into {table_name}: keys must be the primary key "
                f"{primary_key}, got {list(keys)}"
            )

        for name in list(update) + list(merge):
            if name not in table.c:
                raise ValueError(f"upsert into {table_name}: {table_name} has no column {name!r}")
            if name in keys:
                raise ValueError(f"upsert into {table_name}: {name!r} is a key; keys never change")
        for name in update:
            if name in merge:
                raise ValueError(f"upsert into {table_name}: {name!r} is in both update and merge")

        for name, rule in merge.items():
            column = table.c[name]
            if rule == "least" or rule == "greatest":
                if column.nullable:
                    # least/greatest treat NULL differently on each backend.
                    raise ValueError(
                        f"upsert into {table_name}: {name!r} is nullable; {rule!r} needs "
                        f"a NOT NULL column"
                    )
            elif rule == "clear":
                if not column.nullable:
                    raise ValueError(
                        f"upsert into {table_name}: {name!r} is NOT NULL and cannot be cleared"
                    )
            else:
                raise ValueError(
                    f"upsert into {table_name}: unknown merge rule {rule!r} for {name!r}; "
                    f"use 'least', 'greatest' or 'clear'"
                )

        seen_keys = set()
        for row in rows:
            key = []
            for name in keys:
                key.append(row[name])
            key = tuple(key)
            if key in seen_keys:
                # Postgres refuses this; SQLite would apply both rows in turn.
                raise ValueError(f"upsert into {table_name}: the same key twice in rows: {key}")
            seen_keys.add(key)

        statement = self.insert(table).values(rows)
        new = statement.excluded

        set_ = {}
        for name in update:
            set_[name] = new[name]
        for name, rule in merge.items():
            stored = table.c[name]
            if rule == "least":
                set_[name] = self.least(stored, new[name])
            elif rule == "greatest":
                set_[name] = self.greatest(stored, new[name])
            elif rule == "clear":
                set_[name] = sa.null()

        if len(set_) == 0:
            return statement.on_conflict_do_nothing(index_elements=list(keys))
        return statement.on_conflict_do_update(index_elements=list(keys), set_=set_)

    # ----------------------------------------------------------------------------------
    # The job queue
    # ----------------------------------------------------------------------------------

    @abstractmethod
    def claim_next_job(self, session: Session):
        """Atomically move the oldest pending job to running and return it, or None."""
