"""`adapter.server_version` and `adapter.check_server`: the version floors (plan section 4).

Behaviour:
* `server_version(connection)` is the version of the server actually connected to, as
  a tuple of integers: the SQLite library's version, or the Postgres server's
* `check_server(connection)` accepts the minimum and anything newer, and refuses an
  older server with StoreError naming the backend, both versions and the reason.
  Floors: SQLite 3.35 (RETURNING), Postgres 12

Old servers are simulated by replacing `server_version` on the adapter class (the one
place the adapters read the version), so no old server is needed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest

from dbtx.store import Store
from dbtx.store.errors import StoreError


def test_server_version_is_the_connected_servers(store: Store) -> None:
    with store.engine.connect() as conn:
        version = store.adapter.server_version(conn)
        if store.test_backend == "postgres":
            version_num = int(conn.exec_driver_sql("SHOW server_version_num").scalar())
            expected = (version_num // 10000, version_num % 10000)
            assert version[:2] == expected
        else:
            assert version == sqlite3.sqlite_version_info
    assert all(isinstance(part, int) for part in version)


def test_the_real_server_passes(store: Store) -> None:
    with store.engine.connect() as conn:
        store.adapter.check_server(conn)


@pytest.fixture
def server_reports(store: Store, monkeypatch: pytest.MonkeyPatch) -> Callable[[tuple[int, ...]], None]:
    """`server_reports((3, 34, 1))`: the adapter now sees a server of that version."""

    def report(version: tuple[int, ...]) -> None:
        monkeypatch.setattr(type(store.adapter), "server_version", lambda self, connection: version)

    return report


def test_the_minimum_version_passes(
    store: Store,
    server_reports: Callable[[tuple[int, ...]], None],
) -> None:
    server_reports(store.adapter.minimum_server_version)
    with store.engine.connect() as conn:
        store.adapter.check_server(conn)


@pytest.mark.parametrize(
    "sqlite_version, postgres_version, sqlite_text, postgres_text",
    [
        ((3, 34, 1), (11, 22), "3.34.1", "11.22"),
        ((2, 99, 99), (9, 6), "2.99.99", "9.6"),
    ],
)
def test_an_older_server_is_refused(
    store: Store,
    server_reports: Callable[[tuple[int, ...]], None],
    sqlite_version: tuple[int, ...],
    postgres_version: tuple[int, ...],
    sqlite_text: str,
    postgres_text: str,
) -> None:
    if store.test_backend == "postgres":
        server_reports(postgres_version)
        expected = (
            f"Postgres {postgres_text} is too old: dbtx needs Postgres 12 or newer "
            f"(the oldest supported PostgreSQL release dbtx is tested against)"
        )
    else:
        server_reports(sqlite_version)
        expected = (
            f"SQLite {sqlite_text} is too old: dbtx needs SQLite 3.35 or newer "
            f"(RETURNING needs 3.35)"
        )
    with store.engine.connect() as conn:
        with pytest.raises(StoreError) as info:
            store.adapter.check_server(conn)
    assert str(info.value) == expected
