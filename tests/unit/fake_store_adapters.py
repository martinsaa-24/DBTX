"""Adapter classes for the fake `dbtx.store_adapters` entry points the tests install
(`adapter_plugins` in tests/unit/conftest.py), and a `fakedb` SQLAlchemy dialect to
compile DDL for them. None of them talks to a database."""

from __future__ import annotations

from typing import Any, NoReturn, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.sql.compiler import TypeCompiler

from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.adapters.sqlite import SqliteAdapter


class FakeDbAdapter(StoreAdapter):
    """A valid plugin for the made-up backend `fakedb`."""

    dialect = "fakedb"
    display_name = "FakeDB"
    driver = "fakedriver"
    driver_package = None
    driver_hint = "fakedb+fakedriver://"
    install_hint = ""
    supports_schemas = False
    minimum_server_version = (1,)
    minimum_server_reason = "test"

    def create_engine(self, uri: str, schema: Optional[str]) -> NoReturn:
        raise NotImplementedError

    def on_connect(self, dbapi_connection: Any, connection_record: Any) -> NoReturn:
        raise NotImplementedError

    def server_version(self, connection: sa.Connection) -> NoReturn:
        raise NotImplementedError

    def insert(self, table: sa.Table) -> NoReturn:
        raise NotImplementedError

    def greatest(self, *columns: sa.ColumnElement) -> NoReturn:
        raise NotImplementedError

    def least(self, *columns: sa.ColumnElement) -> NoReturn:
        raise NotImplementedError

    def claim_next_job(self, session: Session) -> NoReturn:
        raise NotImplementedError


class FakeDbWithPackageAdapter(FakeDbAdapter):
    """`fakedb` again, with a driver package that is never installed."""

    driver_package = "fakedb_driver_that_is_not_installed"
    install_hint = "install fakedb-driver"


class IncompleteDriverPackageAdapter(FakeDbAdapter):
    """Two drivers, but `driver_package` names a package for only one of them."""

    driver = ["fakedriver", "otherdriver"]
    driver_package = {"fakedriver": None}


class NotAnAdapter:
    """Registered as `fakedb`, but not a StoreAdapter at all."""

    dialect = "fakedb"


class FakeDbJson(sa.JSON):
    """The JSON type `FakeDbJsonAdapter` chooses. Rendered as FAKEJSON on `fakedb`."""


@compiles(FakeDbJson, "fakedb")
def compile_fake_db_json(type_: FakeDbJson, compiler: TypeCompiler, **kw: Any) -> str:
    return "FAKEJSON"


class FakeDbJsonAdapter(FakeDbAdapter):
    """`fakedb` with its own JSON type."""

    def json_type(self) -> sa.types.TypeEngine:
        return FakeDbJson(none_as_null=True)


class FakeDbDialect(SQLiteDialect_pysqlite):
    """A SQLAlchemy dialect named `fakedb`, for compiling DDL without a database."""

    name = "fakedb"
    driver = "fakedriver"
    # Like a real dialect's native types (SQLite's JSONB, for example): listed in
    # colspecs as itself, so `type_descriptor` keeps it instead of swapping in the
    # dialect's generic JSON.
    colspecs = {**SQLiteDialect_pysqlite.colspecs, FakeDbJson: FakeDbJson}


# --------------------------------------------------------------------------------------
# Custom adapters for a built-in dialect (`sqlite`), selected by DB_ADAPTER or by
# serving a driver the built-in does not
# --------------------------------------------------------------------------------------


class CustomSqliteJson(sa.types.UserDefinedType):
    """CustomSqliteAdapter's JSON type, rendered as CUSTOMJSON. Not in SQLite's
    colspecs, so `type_descriptor` keeps it: DDL shows which adapter chose a column's
    type. Only compiled, never stored."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "CUSTOMJSON"


class CustomSqliteAdapter(SqliteAdapter):
    """SQLite through pysqlite (like the built-in) or pysqlcipher (which it lacks).

    `sqlite3` stands in for the pysqlcipher package (sqlcipher3) so that the package
    check passes; the tests never connect through pysqlcipher. Its JSON type renders as
    CUSTOMJSON, so tests can tell which adapter an engine's JSON columns came from.
    """

    display_name = "SQLite (custom)"
    driver = ["pysqlite", "pysqlcipher"]
    driver_package = {"pysqlite": None, "pysqlcipher": "sqlite3"}
    install_hint = "install custom-sqlite[cipher]"

    def json_type(self) -> sa.types.TypeEngine:
        return CustomSqliteJson()


class OtherSqliteAdapter(CustomSqliteAdapter):
    """A second plugin serving sqlite+pysqlcipher: with both installed, the default for
    that pair is ambiguous."""


class CustomSqliteMissingPackageAdapter(CustomSqliteAdapter):
    """CustomSqliteAdapter whose pysqlcipher package is never installed."""

    driver_package = {"pysqlite": None, "pysqlcipher": "sqlcipher3_that_is_not_installed"}
