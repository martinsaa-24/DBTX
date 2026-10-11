"""dbtx store: the database layer that records jobs, invocations, nodes and reports.

    store = open_store(config)          # a DbtxConfig in platform mode, backend enabled
    with store.begin() as session:      # one short-lived session per unit of work (D6)
        ...
    store.dispose()

`open_store` builds the engine through the adapter for DB_URI (DB_ADAPTER when set); it
does not connect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from dbtx.store.adapters import StoreAdapter, attach_adapter, get_adapter
from dbtx.store.errors import StoreError

if TYPE_CHECKING:
    from dbtx.config import DbtxConfig


class Store:
    """An open store: the adapter, its engine and a session factory."""

    def __init__(self, adapter: StoreAdapter, engine: sa.Engine, uri: str, schema: Optional[str]):
        self.adapter = adapter
        self.engine = engine
        self.uri = uri
        # DB_SCHEMA on a backend with schemas; None otherwise.
        self.schema = schema
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def session(self) -> Session:
        """A new session. The caller owns its transaction."""
        return self._sessions()

    def begin(self):
        """A new session in a transaction: committed on exit, rolled back on error."""
        return self._sessions.begin()

    def masked_uri(self) -> str:
        return sa.make_url(self.uri).render_as_string(hide_password=True)

    def dispose(self) -> None:
        self.engine.dispose()


def open_store(config: DbtxConfig) -> Store:
    """The store for a platform-mode configuration with the backend enabled.

    Builds the engine; does not connect. StoreError for a configuration without a
    database.
    """
    if not config.is_platform:
        raise StoreError("standalone mode has no database")
    if not config.backend_enabled:
        raise StoreError("the database backend is disabled (DB_BACKEND=disabled)")
    if config.db_uri is None or config.db_uri.value is None:
        raise StoreError("DB_URI is not set")

    uri = config.db_uri.value
    adapter_name = None
    if config.db_adapter is not None:
        adapter_name = config.db_adapter.value
    adapter = get_adapter(uri, adapter_name)
    schema = None
    if adapter.supports_schemas:
        assert config.db_schema is not None
        schema = config.db_schema.value
    engine = adapter.create_engine(uri, schema)
    # Before anything compiles: JSON columns ask the attached adapter for their type.
    attach_adapter(engine, adapter)
    return Store(adapter, engine, uri, schema)


__all__ = ["Store", "StoreError", "open_store"]
