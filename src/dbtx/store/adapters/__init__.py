"""The adapter registry: which `StoreAdapter` serves a database URI (plan section 4).

Every adapter has a **name**. The built-ins are `sqlite` and `postgresql`. Other
packages add adapters through the `dbtx.store_adapters` entry-point group: the entry
point's name is the adapter's name, and its value is a `StoreAdapter` subclass, e.g.

    [project.entry-points."dbtx.store_adapters"]
    custom_sqlite = "dbtx_custom.adapter:CustomSqliteAdapter"

An adapter declares the SQLAlchemy `dialect` it serves and the `driver`(s) it supports.
Which adapter serves a URI's (backend, driver) pair:

* DB_ADAPTER, when set, names it. It must serve the URI's dialect and driver.
* Otherwise the built-in for the backend, when it supports the driver. Installing a
  plugin never changes this, and this path loads no plugin.
* Otherwise the one plugin that serves the pair. Several is an error asking for
  DB_ADAPTER; none is an "unsupported" error.

Plugin names are listed without loading. A plugin is loaded when it is named, or when
the built-ins cannot serve a pair: then every plugin is loaded, and a broken one is an
error. A plugin named like a built-in is ignored; two plugins with one name are an error.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Optional

import sqlalchemy as sa

from dbtx.store.adapters.base import StoreAdapter
from dbtx.store.adapters.postgres import PostgresAdapter
from dbtx.store.adapters.sqlite import SqliteAdapter
from dbtx.store.errors import StoreError

ENTRY_POINT_GROUP = "dbtx.store_adapters"

BUILTIN_ADAPTERS: dict[str, type[StoreAdapter]] = {
    "sqlite": SqliteAdapter,
    "postgresql": PostgresAdapter,
}

# The attribute `attach_adapter` sets on an engine's dialect.
DIALECT_ADAPTER_ATTRIBUTE = "dbtx_store_adapter"


# --------------------------------------------------------------------------------------
# Declarations: drivers and their packages
# --------------------------------------------------------------------------------------


def supported_drivers(adapter_class: type[StoreAdapter]) -> tuple[str, ...]:
    """The drivers an adapter supports. `driver` is one name, or a list of names."""
    if isinstance(adapter_class.driver, str):
        return (adapter_class.driver,)
    return tuple(adapter_class.driver)


def driver_package_for(adapter_class: type[StoreAdapter], driver: str) -> Optional[str]:
    """The package `driver` needs. `driver_package` is one package (or None) for every
    driver, or a dict with an entry per driver."""
    if isinstance(adapter_class.driver_package, dict):
        return adapter_class.driver_package[driver]
    return adapter_class.driver_package


# --------------------------------------------------------------------------------------
# Plugins
# --------------------------------------------------------------------------------------


def _entry_points():
    """The installed `dbtx.store_adapters` entry points (tests replace this)."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def _plugin_entry_points() -> list:
    """Entry points for adapters that are not built in, sorted by name.

    StoreError when two share a name: which one dbtx would use is not defined.
    """
    by_name = {}
    for entry_point in _entry_points():
        if entry_point.name in BUILTIN_ADAPTERS:
            continue
        if entry_point.name in by_name:
            first = by_name[entry_point.name]
            raise StoreError(
                f"two store adapter plugins are named {entry_point.name!r} "
                f"({first.value}, {entry_point.value}); uninstall one"
            )
        by_name[entry_point.name] = entry_point
    plugins = list(by_name.values())
    plugins.sort(key=lambda entry_point: entry_point.name)
    return plugins


def _load_plugin(entry_point) -> type[StoreAdapter]:
    """Load one plugin and check what it declares. StoreError if it is broken."""
    where = f"store adapter plugin {entry_point.name!r} ({entry_point.value})"
    try:
        adapter_class = entry_point.load()
    except Exception as exc:
        raise StoreError(f"{where} could not be loaded: {exc}") from exc
    if not isinstance(adapter_class, type) or not issubclass(adapter_class, StoreAdapter):
        raise StoreError(f"{where} is not a StoreAdapter subclass")
    if isinstance(adapter_class.driver_package, dict):
        for driver in supported_drivers(adapter_class):
            if driver not in adapter_class.driver_package:
                raise StoreError(f"{where}: driver_package has no entry for driver {driver!r}")
    return adapter_class


def _all_plugins() -> list[tuple[str, type[StoreAdapter]]]:
    """Every plugin, loaded, sorted by name. StoreError if any is broken."""
    plugins = []
    for entry_point in _plugin_entry_points():
        plugins.append((entry_point.name, _load_plugin(entry_point)))
    return plugins


# --------------------------------------------------------------------------------------
# Adapters by name
# --------------------------------------------------------------------------------------


def adapter_names() -> list[str]:
    """Every adapter name: the built-ins, then plugins by name. Loads nothing."""
    names = list(BUILTIN_ADAPTERS)
    for entry_point in _plugin_entry_points():
        names.append(entry_point.name)
    return names


def adapter_class_by_name(name: str) -> type[StoreAdapter]:
    """The adapter named `name` (DB_ADAPTER). Loads only that plugin."""
    if name in BUILTIN_ADAPTERS:
        return BUILTIN_ADAPTERS[name]
    for entry_point in _plugin_entry_points():
        if entry_point.name == name:
            return _load_plugin(entry_point)
    raise StoreError(
        f"unknown store adapter {name!r}; available: {', '.join(adapter_names())}"
    )


# --------------------------------------------------------------------------------------
# The adapter for a (backend, driver) pair
# --------------------------------------------------------------------------------------


def check_adapter_serves(
    name: str, adapter_class: type[StoreAdapter], backend: str, driver: str
) -> None:
    """StoreError unless the adapter serves `backend` through `driver`."""
    if adapter_class.dialect != backend:
        raise StoreError(f"store adapter {name!r} serves {adapter_class.dialect}, not {backend}")
    drivers = supported_drivers(adapter_class)
    if driver not in drivers:
        raise StoreError(
            f"store adapter {name!r} does not support driver {driver!r}; "
            f"it supports: {', '.join(drivers)}"
        )


def default_adapter(backend: str, driver: str) -> tuple[str, type[StoreAdapter]]:
    """(name, class) of the adapter that serves the pair when DB_ADAPTER is not set."""
    for name, adapter_class in BUILTIN_ADAPTERS.items():
        if adapter_class.dialect == backend and driver in supported_drivers(adapter_class):
            return name, adapter_class

    # The built-ins cannot serve the pair: look at every plugin.
    every_adapter = list(BUILTIN_ADAPTERS.items()) + _all_plugins()

    candidates = []
    for name, adapter_class in every_adapter:
        if adapter_class.dialect == backend and driver in supported_drivers(adapter_class):
            candidates.append((name, adapter_class))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = []
        for name, _ in candidates:
            names.append(name)
        raise StoreError(
            f"several store adapters serve {backend}+{driver}: {', '.join(names)}; "
            f"choose one with DB_ADAPTER"
        )

    for_backend = []
    for name, adapter_class in every_adapter:
        if adapter_class.dialect == backend:
            for_backend.append((name, adapter_class))
    if len(for_backend) == 1:
        adapter_class = for_backend[0][1]
        raise StoreError(
            f"unsupported {adapter_class.display_name} driver {driver!r}; "
            f"use {adapter_class.driver_hint}"
        )
    if len(for_backend) > 1:
        pairs = []
        for name, adapter_class in for_backend:
            for supported in supported_drivers(adapter_class):
                pairs.append(f"{backend}+{supported} ({name})")
        display_name = for_backend[0][1].display_name
        raise StoreError(
            f"unsupported {display_name} driver {driver!r}; supported: {', '.join(pairs)}"
        )

    backends = []
    for _, adapter_class in every_adapter:
        if adapter_class.dialect not in backends:
            backends.append(adapter_class.dialect)
    raise StoreError(f"unsupported backend {backend!r}; supported: {', '.join(backends)}")


def resolve_adapter(
    backend: str, driver: str, name: Optional[str] = None
) -> tuple[str, type[StoreAdapter]]:
    """(name, class) of the adapter for the pair: the one `name` (DB_ADAPTER) names,
    which must serve the pair, or else the default."""
    if name is None:
        return default_adapter(backend, driver)
    adapter_class = adapter_class_by_name(name)
    check_adapter_serves(name, adapter_class, backend, driver)
    return name, adapter_class


def adapters_for_backend(
    backend: str, name: Optional[str] = None
) -> list[tuple[str, type[StoreAdapter]]]:
    """(name, class) of the adapters that could serve `backend`, whatever the driver:
    the one `name` names; else the built-in for it; else every plugin for it. For hints
    when the driver is unknown."""
    if name is not None:
        return [(name, adapter_class_by_name(name))]
    found = []
    for builtin_name, adapter_class in BUILTIN_ADAPTERS.items():
        if adapter_class.dialect == backend:
            found.append((builtin_name, adapter_class))
    if found:
        return found
    for plugin_name, adapter_class in _all_plugins():
        if adapter_class.dialect == backend:
            found.append((plugin_name, adapter_class))
    return found


def get_adapter(uri: str, adapter_name: Optional[str] = None) -> StoreAdapter:
    """A new adapter for `uri`: the one `adapter_name` (DB_ADAPTER) names, or else the
    default for the URI's backend and driver."""
    url = sa.make_url(uri)
    _, adapter_class = resolve_adapter(url.get_backend_name(), url.get_driver_name(), adapter_name)
    return adapter_class()


# --------------------------------------------------------------------------------------
# The adapter behind an engine
# --------------------------------------------------------------------------------------


def attach_adapter(engine: sa.Engine, adapter: StoreAdapter) -> None:
    """Record on the engine's dialect which adapter built it (`open_store` does this).
    `JSONType` reads it: a column type sees only the dialect, never DB_ADAPTER."""
    setattr(engine.dialect, DIALECT_ADAPTER_ATTRIBUTE, adapter)


def attached_adapter(dialect) -> Optional[StoreAdapter]:
    """The adapter attached to `dialect`, or None for a dialect no store built."""
    return getattr(dialect, DIALECT_ADAPTER_ATTRIBUTE, None)


def adapter_for_dialect(dialect) -> StoreAdapter:
    """The adapter that decides column types for `dialect` (`JSONType`).

    * the adapter `open_store` attached to it (DB_ADAPTER's, or the default for DB_URI)
    * else, for a bare dialect: the built-in for its name, whatever its driver. No
      DB_ADAPTER applies to a bare dialect, and the driver rule is DB_URI's business:
      SQLAlchemy 2.0's generic Postgres dialect is psycopg2
    * else the default for its name and driver (a plugin backend)
    """
    adapter = attached_adapter(dialect)
    if adapter is not None:
        return adapter
    for adapter_class in BUILTIN_ADAPTERS.values():
        if adapter_class.dialect == dialect.name:
            return adapter_class()
    _, adapter_class = default_adapter(dialect.name, dialect.driver)
    return adapter_class()


__all__ = [
    "BUILTIN_ADAPTERS",
    "ENTRY_POINT_GROUP",
    "PostgresAdapter",
    "SqliteAdapter",
    "StoreAdapter",
    "adapter_class_by_name",
    "adapter_for_dialect",
    "adapter_names",
    "adapters_for_backend",
    "attach_adapter",
    "attached_adapter",
    "check_adapter_serves",
    "default_adapter",
    "driver_package_for",
    "get_adapter",
    "resolve_adapter",
    "supported_drivers",
]
