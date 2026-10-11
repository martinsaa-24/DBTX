"""The adapter registry (`dbtx.store.adapters`): which adapter serves a database URI.

Behaviour (plan section 4, "Registry"; adapter names and DB_ADAPTER added in Phase 2):
* every adapter has a name: the built-ins are `sqlite` and `postgresql`, a plugin is
  named by its `dbtx.store_adapters` entry point
* an adapter declares its `dialect` and the driver(s) it supports (`driver`: a name or
  a list), and the package each driver needs (`driver_package`: one for all, or a dict)
* the default adapter for a (backend, driver) pair is the built-in when it supports the
  pair, whatever plugins are installed; otherwise the one plugin that does. Several
  plugins for the pair is an error asking for DB_ADAPTER; none is an "unsupported" error
* a named adapter (DB_ADAPTER) overrides the default, and must serve the URI's dialect
  and driver
* plugin names are listed without loading; a plugin is loaded when it is named, or when
  the built-ins cannot serve a pair (then every plugin is loaded, and a broken one is an
  error). The built-in default never loads a plugin
* a plugin named like a built-in is ignored; two plugins with one name are an error
* `open_store` attaches the adapter to the engine's dialect, for `JSONType`

Plugins are fake entry points installed by the `adapter_plugins` fixture
(tests/unit/conftest.py); an autouse fixture hides any plugin installed on the machine.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import sqlalchemy as sa

from dbtx.store import adapters
from dbtx.store.adapters import PostgresAdapter, SqliteAdapter, StoreAdapter
from dbtx.store.errors import StoreError
from tests.unit.fake_store_adapters import CustomSqliteAdapter, FakeDbAdapter

FAKE = "tests.unit.fake_store_adapters"
BROKEN = "tests.unit.no_such_module:Adapter"


# --------------------------------------------------------------------------------------
# Declarations: drivers and their packages
# --------------------------------------------------------------------------------------


def test_one_driver_as_a_name() -> None:
    assert adapters.supported_drivers(SqliteAdapter) == ("pysqlite",)
    assert adapters.supported_drivers(PostgresAdapter) == ("psycopg",)


def test_several_drivers_as_a_list() -> None:
    assert adapters.supported_drivers(CustomSqliteAdapter) == ("pysqlite", "pysqlcipher")


def test_one_driver_package_for_every_driver() -> None:
    assert adapters.driver_package_for(PostgresAdapter, "psycopg") == "psycopg"
    assert adapters.driver_package_for(SqliteAdapter, "pysqlite") is None


def test_a_driver_package_per_driver() -> None:
    assert adapters.driver_package_for(CustomSqliteAdapter, "pysqlite") is None
    assert adapters.driver_package_for(CustomSqliteAdapter, "pysqlcipher") == "sqlite3"


# --------------------------------------------------------------------------------------
# Built-in adapters
# --------------------------------------------------------------------------------------


def test_builtin_adapter_names() -> None:
    assert adapters.adapter_names() == ["sqlite", "postgresql"]


@pytest.mark.parametrize("name, adapter_class", [("sqlite", SqliteAdapter), ("postgresql", PostgresAdapter)])
def test_builtin_adapters_by_name(name: str, adapter_class: type[StoreAdapter]) -> None:
    assert adapters.adapter_class_by_name(name) is adapter_class


@pytest.mark.parametrize(
    "backend, driver, expected",
    [
        ("sqlite", "pysqlite", ("sqlite", SqliteAdapter)),
        ("postgresql", "psycopg", ("postgresql", PostgresAdapter)),
    ],
)
def test_builtin_default_adapters(
    backend: str,
    driver: str,
    expected: tuple[str, type[StoreAdapter]],
) -> None:
    assert adapters.default_adapter(backend, driver) == expected


@pytest.mark.parametrize(
    "uri, adapter_class",
    [
        ("sqlite://", SqliteAdapter),
        ("sqlite:///relative.db", SqliteAdapter),
        ("sqlite+pysqlite:////absolute.db", SqliteAdapter),
        # Not `postgresql://`: its default driver is psycopg on SQLAlchemy 2.1, psycopg2
        # (refused) on 2.0.
        ("postgresql+psycopg://dbtx:secret@db:5432/dbtx", PostgresAdapter),
    ],
)
def test_get_adapter_for_a_uri(uri: str, adapter_class: type[StoreAdapter]) -> None:
    assert type(adapters.get_adapter(uri)) is adapter_class


def test_every_call_gets_a_new_adapter() -> None:
    assert adapters.get_adapter("sqlite://") is not adapters.get_adapter("sqlite://")


@pytest.mark.parametrize("backend", ["mysql", "duckdb", "oracle"])
def test_unknown_backend_lists_the_supported_ones(backend: str) -> None:
    with pytest.raises(StoreError) as info:
        adapters.default_adapter(backend, "somedriver")
    assert str(info.value) == f"unsupported backend {backend!r}; supported: sqlite, postgresql"


def test_unknown_backend_in_a_uri() -> None:
    with pytest.raises(StoreError, match="unsupported backend 'mysql'"):
        adapters.get_adapter("mysql+pymysql://u@h/db")


def test_unsupported_driver_for_a_builtin_backend() -> None:
    with pytest.raises(StoreError) as info:
        adapters.default_adapter("sqlite", "pysqlcipher")
    assert str(info.value) == (
        "unsupported SQLite driver 'pysqlcipher'; use sqlite:/// (the standard library driver)"
    )


def test_unknown_adapter_name() -> None:
    with pytest.raises(StoreError) as info:
        adapters.adapter_class_by_name("nope")
    assert str(info.value) == "unknown store adapter 'nope'; available: sqlite, postgresql"


# --------------------------------------------------------------------------------------
# Plugins: names, loading
# --------------------------------------------------------------------------------------


def test_plugin_names_are_listed_after_the_builtins_without_loading(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"zzdb": BROKEN, "fakedb": f"{FAKE}:FakeDbAdapter"})
    assert adapters.adapter_names() == ["sqlite", "postgresql", "fakedb", "zzdb"]


def test_a_plugin_by_name(adapter_plugins: Callable[[dict[str, str]], None]) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter"})
    assert adapters.adapter_class_by_name("custom_sqlite") is CustomSqliteAdapter


def test_unknown_adapter_name_lists_the_plugins(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter"})
    with pytest.raises(StoreError, match=r"available: sqlite, postgresql, custom_sqlite$"):
        adapters.adapter_class_by_name("nope")


def test_a_plugin_named_like_a_builtin_is_ignored(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    # If the registry loaded this entry point, it would fail: the module does not exist.
    adapter_plugins({"sqlite": BROKEN})
    assert adapters.adapter_class_by_name("sqlite") is SqliteAdapter
    assert adapters.adapter_names() == ["sqlite", "postgresql"]


def test_two_plugins_with_one_name_are_an_error(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter"})
    adapter_plugins({"custom_sqlite": f"{FAKE}:OtherSqliteAdapter"})
    with pytest.raises(StoreError) as info:
        adapters.adapter_class_by_name("custom_sqlite")
    assert str(info.value) == (
        f"two store adapter plugins are named 'custom_sqlite' ({FAKE}:CustomSqliteAdapter, "
        f"{FAKE}:OtherSqliteAdapter); uninstall one"
    )


@pytest.mark.parametrize(
    "value, error",
    [
        (BROKEN, r"could not be loaded"),
        (f"{FAKE}:NotAnAdapter", r"is not a StoreAdapter subclass"),
        (
            f"{FAKE}:IncompleteDriverPackageAdapter",
            r"driver_package has no entry for driver 'otherdriver'",
        ),
    ],
)
def test_a_broken_plugin_is_an_error_when_named(
    adapter_plugins: Callable[[dict[str, str]], None],
    value: str,
    error: str,
) -> None:
    adapter_plugins({"fakedb": value})
    with pytest.raises(StoreError, match=r"store adapter plugin 'fakedb' .*" + error):
        adapters.adapter_class_by_name("fakedb")


# --------------------------------------------------------------------------------------
# Plugins: the default adapter for a (backend, driver) pair
# --------------------------------------------------------------------------------------


def test_a_plugin_is_the_default_for_its_own_backend(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": f"{FAKE}:FakeDbAdapter"})
    assert adapters.default_adapter("fakedb", "fakedriver") == ("fakedb", FakeDbAdapter)
    assert type(adapters.get_adapter("fakedb+fakedriver://host/db")) is FakeDbAdapter


def test_the_builtin_stays_the_default_when_a_plugin_serves_the_same_pair(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    # Installing a plugin never changes the default for a pair the built-in serves, and
    # that path loads no plugin (the broken one would fail).
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter", "broken": BROKEN})
    assert adapters.default_adapter("sqlite", "pysqlite") == ("sqlite", SqliteAdapter)


def test_a_plugin_is_the_default_for_a_driver_the_builtin_lacks(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter"})
    assert adapters.default_adapter("sqlite", "pysqlcipher") == (
        "custom_sqlite",
        CustomSqliteAdapter,
    )


def test_several_plugins_for_one_pair_need_a_choice(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins(
        {
            "custom_sqlite": f"{FAKE}:CustomSqliteAdapter",
            "other_sqlite": f"{FAKE}:OtherSqliteAdapter",
        }
    )
    with pytest.raises(StoreError) as info:
        adapters.default_adapter("sqlite", "pysqlcipher")
    assert str(info.value) == (
        "several store adapters serve sqlite+pysqlcipher: custom_sqlite, other_sqlite; "
        "choose one with DB_ADAPTER"
    )


def test_unsupported_driver_lists_every_adapter_for_the_backend(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter"})
    with pytest.raises(StoreError) as info:
        adapters.default_adapter("sqlite", "aiosqlite")
    assert str(info.value) == (
        "unsupported SQLite driver 'aiosqlite'; supported: sqlite+pysqlite (sqlite), "
        "sqlite+pysqlite (custom_sqlite), sqlite+pysqlcipher (custom_sqlite)"
    )


def test_plugin_backends_are_listed_in_the_unknown_backend_error(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins(
        {
            "zzdb": f"{FAKE}:FakeDbAdapter",
            "fakedb": f"{FAKE}:FakeDbAdapter",
            "custom_sqlite": f"{FAKE}:CustomSqliteAdapter",
        }
    )
    with pytest.raises(StoreError) as info:
        adapters.default_adapter("mysql", "pymysql")
    assert str(info.value) == "unsupported backend 'mysql'; supported: sqlite, postgresql, fakedb"


def test_a_broken_plugin_is_an_error_when_the_builtins_cannot_serve_a_pair(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter", "broken": BROKEN})
    with pytest.raises(StoreError, match=r"store adapter plugin 'broken' .*could not be loaded"):
        adapters.default_adapter("sqlite", "pysqlcipher")


def test_a_broken_plugin_never_affects_the_builtin_defaults(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"fakedb": BROKEN})
    assert type(adapters.get_adapter("sqlite://")) is SqliteAdapter
    assert type(adapters.get_adapter("postgresql+psycopg://h/db")) is PostgresAdapter
    # Listed by name, never loaded.
    assert adapters.adapter_names() == ["sqlite", "postgresql", "fakedb"]


# --------------------------------------------------------------------------------------
# A named adapter (DB_ADAPTER)
# --------------------------------------------------------------------------------------


def test_a_named_adapter_overrides_the_default(
    adapter_plugins: Callable[[dict[str, str]], None],
) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter"})
    assert adapters.resolve_adapter("sqlite", "pysqlite", "custom_sqlite") == (
        "custom_sqlite",
        CustomSqliteAdapter,
    )
    assert type(adapters.get_adapter("sqlite://", "custom_sqlite")) is CustomSqliteAdapter


def test_a_named_builtin(adapter_plugins: Callable[[dict[str, str]], None]) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter"})
    assert adapters.resolve_adapter("sqlite", "pysqlite", "sqlite") == ("sqlite", SqliteAdapter)


def test_no_name_is_the_default(adapter_plugins: Callable[[dict[str, str]], None]) -> None:
    adapter_plugins({"custom_sqlite": f"{FAKE}:CustomSqliteAdapter"})
    assert adapters.resolve_adapter("sqlite", "pysqlcipher") == ("custom_sqlite", CustomSqliteAdapter)
    assert adapters.resolve_adapter("sqlite", "pysqlite", None) == ("sqlite", SqliteAdapter)


def test_a_named_adapter_must_support_the_driver() -> None:
    with pytest.raises(StoreError) as info:
        adapters.resolve_adapter("sqlite", "pysqlcipher", "sqlite")
    assert str(info.value) == (
        "store adapter 'sqlite' does not support driver 'pysqlcipher'; it supports: pysqlite"
    )


def test_a_named_adapter_must_serve_the_dialect() -> None:
    with pytest.raises(StoreError) as info:
        adapters.resolve_adapter("sqlite", "pysqlite", "postgresql")
    assert str(info.value) == "store adapter 'postgresql' serves postgresql, not sqlite"


# --------------------------------------------------------------------------------------
# The adapter attached to an engine's dialect
# --------------------------------------------------------------------------------------


def test_an_attached_adapter_is_found_from_the_dialect() -> None:
    engine = sa.create_engine("sqlite://")
    adapter = CustomSqliteAdapter()
    adapters.attach_adapter(engine, adapter)
    assert adapters.attached_adapter(engine.dialect) is adapter


def test_a_dialect_without_an_attached_adapter() -> None:
    assert adapters.attached_adapter(sa.create_engine("sqlite://").dialect) is None
