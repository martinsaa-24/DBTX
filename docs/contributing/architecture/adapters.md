# Store Adapter Architecture

How dbtx's store adapters are structured, how they are found and loaded, and why they
are built this way. This page is for contributors changing `dbtx.store`. If you are
writing a plugin, start with [the plugin guide](../plugins/adapters.md), which covers
the interface and installation.

## The rule

**Everything that differs between database backends lives in the adapter.** The
repositories, the start-up checks, the config validation and the column types never
branch on the dialect. They ask the adapter (plan section 4, decision D2). A new
backend, or a new way to talk to an existing one, is one new `StoreAdapter` subclass.
If you are about to write `if dialect == "postgresql"` anywhere else, add an adapter
member instead.

## Structure

```
src/dbtx/store/
  adapters/
    __init__.py    the registry: BUILTIN_ADAPTERS, ENTRY_POINT_GROUP, adapter names,
                   default_adapter / resolve_adapter / get_adapter, attach_adapter /
                   adapter_for_dialect
    base.py        StoreAdapter, the abstract interface, and its shared logic
    sqlite.py      SqliteAdapter
    postgres.py    PostgresAdapter
  types.py         JSONType and UTCDateTime; JSONType asks adapter_for_dialect for its type
  __init__.py      Store / open_store: resolves and attaches the adapter, builds the engine
  startup.py       start-up checks: adapter.check_server
  repositories/    write paths: take (session, adapter, ...)
src/dbtx/config.py DB_ADAPTER option; validate_db_adapter_name / validate_db_uri /
                   validate_db_schema, driven by adapter class attributes
src/dbtx/cli.py    `dbtx config` shows the resolved adapter and why
```

## Inheritance

```
                       StoreAdapter  (abc.ABC, adapters/base.py)
                       ├─ class attributes: what the adapter serves
                       ├─ abstract methods: what every backend does differently
                       └─ concrete methods: shared logic, overridable
                                  │
          ┌───────────────────────┼─────────────────────────┐
   SqliteAdapter           PostgresAdapter         <plugin>Adapter
   name "sqlite"           name "postgresql"       name = its entry point
          │                       │
   <plugin> subclass       <plugin> subclass       (a plugin may subclass a built-in
   e.g. extra driver       e.g. tuned for a        to change only what differs)
                           hosted service
```

The built-ins are siblings, each a direct subclass of `StoreAdapter`. A plugin
subclasses `StoreAdapter` for a new backend, or a built-in to reuse its behaviour for
the same backend. The base class has three kinds of member:

| Kind | Members | Why it is this kind |
|---|---|---|
| **Class attributes** | `dialect`, `display_name`, `driver` (one or a list), `driver_package` (one or a dict per driver), `driver_hint`, `install_hint`, `supports_schemas`, `minimum_server_version`, `minimum_server_reason` | Facts about the adapter. The registry and config validation read them from the **class**, without creating an instance or importing a driver |
| **Abstract methods** | `create_engine`, `on_connect`, `server_version`, `insert`, `greatest`, `least`, `claim_next_job` | Behaviour with no sensible shared default. Every backend has to decide |
| **Concrete methods** | `check_server`, `upsert`, `json_type` | One correct implementation that works for most backends (`check_server`, `upsert`), or a default that a backend may improve on (`json_type`) |

`upsert()` is concrete on purpose. It validates its arguments (the keys are the primary
key, merge rules fit the column's nullability, no duplicate keys) before building SQL.
Keeping that in the base class means every backend refuses the same mistakes. The only
backend-specific pieces it needs are `insert()`, `least()` and `greatest()`.

### Adding a member

- **Prefer a concrete method with a default.** Existing plugins keep working.
- **An abstract method breaks every installed plugin**: a subclass that doesn't
  implement it can no longer be instantiated. Add one only when no default is correct,
  and note it in the plan, because plugin authors pin dbtx versions against this
  interface.
- Use a class attribute for a fact that the registry or config validation needs, since
  neither may instantiate the adapter or import a driver.

## Identity: names, dialects and drivers

Three different things identify an adapter, and they are deliberately separate:

| | What it is | Where it comes from | Unique? |
|---|---|---|---|
| **name** | the adapter's identity, what `DB_ADAPTER` takes | the entry-point name; `sqlite` / `postgresql` for the built-ins | yes, one adapter per name |
| **`dialect`** | the backend it serves | class attribute; must equal the URI's backend name and SQLAlchemy's `dialect.name` | no, many adapters may serve `sqlite` |
| **`driver`** | the DBAPI driver(s) it supports | class attribute, one or a list | no, several adapters may support `pysqlite` |

A URI selects a **(backend, driver) pair**. More than one adapter may serve a pair, so
the registry needs a rule for the default and an override for the rest.

## Load patterns

### 1. From a URI and DB_ADAPTER (config, `open_store`)

```
DB_URI ──make_url──▶ (backend, driver)         DB_ADAPTER (a name, or unset)
"sqlite+pysqlcipher:///x.db" → ("sqlite", "pysqlcipher")
                                   │
                 resolve_adapter(backend, driver, name)
                                   │
       ┌───────────────────────────┴──────────────────────────────┐
  name set                                                    name unset
  adapter_class_by_name(name)                                 default_adapter(backend, driver)
    built-in? → that class                                     1. the built-in for backend, if
    else the plugin with that entry-point name                    it supports driver → done
      (only it is loaded)                                         (no plugin loaded)
    else "unknown store adapter … available: …"                2. else load every plugin; the one
  check_adapter_serves: dialect == backend,                       serving the pair → done
    driver in supported_drivers                                3. several → "choose one with
      else "serves X, not Y" /                                    DB_ADAPTER"; none → "unsupported
      "does not support driver …"                                 <Backend> driver …" or
                                                                  "unsupported backend …"
```

Who uses it, and what they need:

| Caller | Needs | Does |
|---|---|---|
| `config.validate_db_adapter_name` | the class | `DB_ADAPTER` names an installed adapter, even without `DB_URI` |
| `config.validate_db_uri` | the class | resolves (backend, driver, `DB_ADAPTER`); checks the driver's package with `find_spec`, never importing it. Errors are attributed to `DB_ADAPTER` or `DB_URI` |
| `config.validate_db_schema` | the class | `DB_SCHEMA` refused when the resolved adapter doesn't `supports_schemas` |
| `DbtxConfig.resolved_adapter()` (`dbtx config`) | name and class | shows `DB_ADAPTER  <name>  [default for <backend>+<driver>]` or `[<source>]` |
| `open_store` | an instance | `get_adapter(uri, DB_ADAPTER)`, `create_engine`, then `attach_adapter` |
| `startup.check_database` | the store's instance | `adapter.check_server(connection)` |
| repositories | the store's instance | `upsert`, `claim_next_job`, `insert`, `least` / `greatest` |

`get_adapter()` returns a **new instance** every time. Adapters hold no state, and
their constructor takes no arguments. A `Store` keeps the one it was opened with.

### 2. From a SQLAlchemy dialect (`JSONType`)

The models are declared when `dbtx.store.models` is imported, before anyone knows which
adapter will run. `JSONType` is a `TypeDecorator`, so the storage type is chosen later,
when SQLAlchemy first compiles or executes SQL for a dialect. A column type only ever
sees the **dialect**, never `DB_URI` or `DB_ADAPTER`:

```
SQLAlchemy compiles a JSON column for `dialect`
  └─ JSONType.load_dialect_impl(dialect)
       └─ adapter_for_dialect(dialect)
            1. the adapter open_store attached to the dialect     (DB_ADAPTER's, or the default)
            2. else the built-in for dialect.name, any driver      (a bare dialect)
            3. else default_adapter(dialect.name, dialect.driver)  (a plugin backend)
       └─ dialect.type_descriptor(adapter.json_type())  ─▶  the storage type
```

SQLAlchemy caches the result per type and dialect instance, so this runs once per JSON
column per engine, not once per row.

### 3. Plugins: names read eagerly, classes loaded on demand

- `_entry_points()` reads `importlib.metadata.entry_points(group="dbtx.store_adapters")`
  in dbtx's own environment. It is also the test seam: the autouse
  `no_adapter_plugins` fixture replaces it to hide plugins installed on the machine, and
  `adapter_plugins` replaces it to install fakes.
- `_plugin_entry_points()` drops entry points named like a built-in, refuses two with
  one name, and sorts the rest by name.
- `adapter_names()` lists names only. Nothing is imported.
- `adapter_class_by_name(name)` loads **only** that plugin.
- `default_adapter()` loads **every** plugin, but only when the built-in can't serve the
  pair (`_all_plugins()`).
- `_load_plugin()` checks what a plugin declares: a `StoreAdapter` subclass, and a
  `driver_package` dict with an entry for every driver.

### Import graph

```
dbtx.store.adapters ──▶ adapters.sqlite / adapters.postgres ──▶ dbtx.store.models ──▶ dbtx.store.types
        ▲                                                                                    │
        └───────────────── imported inside JSONType.load_dialect_impl ──────────────────────┘
```

The adapters import the models (`claim_next_job` needs `Job`), and the models import the
types. So `types.py` imports the registry **inside** `load_dialect_impl`, not at module
level. `config.py` likewise imports the registry inside its functions, which keeps
`import dbtx.config` free of SQLAlchemy dialects.

## Design decisions

### AD1: Every backend difference behind one interface

- **Decision:** dialect-specific code lives only in `StoreAdapter` subclasses.
  Repositories take `(session, adapter, ...)`.
- **Why:** one place to read and test per backend. A plugin can add or change a backend
  without touching dbtx.
- **Consequence:** `types.py` doesn't import `sqlalchemy.dialects.postgresql`. The
  Postgres JSON type lives in `PostgresAdapter.json_type()` (see AD8).

### AD2: An adapter's name is its identity; its `dialect` must match SQLAlchemy's

- **Decision:** an adapter is identified by its **name**: the entry-point name, or
  `sqlite` / `postgresql` for the built-ins. Its `dialect` attribute is separate and
  must equal both:
  1. `make_url(DB_URI).get_backend_name()`, which is how URIs are matched to adapters
  2. the SQLAlchemy dialect's `dialect.name`, which is how `JSONType` finds an adapter
     for a dialect no store built
- **Why:** several adapters may serve one dialect (AD10), so the dialect can't be the
  key. The name gives `DB_ADAPTER` something stable and unique to point at. Matching
  `dialect` to SQLAlchemy's own names means no mapping table is needed.
- **Enforced:** the URI path compares `dialect` with the backend name, and
  `check_adapter_serves` refuses an adapter for another dialect
  (`serves postgresql, not sqlite`). Rule 2 is checked only implicitly: a plugin whose
  `dialect` differs from SQLAlchemy's `dialect.name` still works for engines from
  `open_store` (the adapter is attached, AD12), but compiling DDL for a bare dialect
  can't find it. The plugin guide states it as a requirement.
- **History:** before `DB_ADAPTER`, the entry-point name also had to equal the dialect.
  That forced one adapter per backend.

### AD3: Built-ins have reserved names and are the default for their pairs

- **Decision:** `sqlite` and `postgresql` always name the built-ins. An entry point
  with either name is ignored. With `DB_ADAPTER` unset, the built-in serves every pair
  it supports, whatever plugins are installed. A plugin replaces a built-in only when a
  user names it in `DB_ADAPTER`.
- **Why:** installing a package must never silently change how dbtx talks to its two
  supported backends, and the built-ins are what the test suite covers. An explicit
  config line is a visible, reviewable choice.
- **History:** this used to be "a plugin cannot replace a built-in" at all.
  `DB_ADAPTER` relaxes it to "only explicitly".

### AD4: Names are read without loading; plugins are loaded only when needed

- **Decision:** listing names never imports a plugin. A plugin is imported when
  `DB_ADAPTER` names it, or when the built-ins can't serve a URI's pair and every plugin
  is loaded to find one. In that scan, **a broken plugin is an error**, even if another
  plugin would have served the URI.
- **Why:** the built-in default path, which SQLite and Postgres users take, never loads
  a plugin, so a broken plugin cannot affect them. Inside the scan, an error is
  simpler and louder than skipping. A typo in the plugin you meant to use would
  otherwise surface as a confusing "unsupported" after a warning.
- **Consequence:** load failures are `StoreError` (`could not be loaded`,
  `is not a StoreAdapter subclass`, `driver_package has no entry for driver …`), which
  config turns into `ConfigError` (exit 2).

### AD5: Driver rules are class attributes; an adapter may support several drivers

- **Decision:** `driver` is one name or a list. `driver_package` is one module (or
  `None`) for every driver, or a dict with an entry per driver, checked when the plugin
  is loaded. `driver_hint`, `install_hint` and `display_name` complete the messages.
  `validate_db_uri` has no per-backend branches.
- **Why:** plugins get the same checks and messages as the built-ins. Validation reads
  attributes from the class, so it never instantiates an adapter or imports a driver
  (`importlib.util.find_spec` only). The str-or-dict form keeps the built-ins'
  one-driver declarations unchanged, while a multi-driver plugin states each driver's
  package explicitly.
- **The built-ins still accept exactly one driver each:** `postgresql+psycopg` and
  `sqlite+pysqlite`. Transactions, locking and time zones are tested per driver. Other
  drivers come from plugins that test them.

### AD6: A driver-less URI whose dialect is missing is a config error, narrowly

- **Decision:** for a URI with no `+driver`, `url.get_driver_name()` loads the dialect
  to find its default driver. `config.uri_driver` catches **only**
  `sqlalchemy.exc.NoSuchModuleError`, and **only** around that call, for a URI with no
  `+`. The driver is unknown, so the hints come from the one adapter that could serve
  the backend (the one `DB_ADAPTER` names, else the built-in, else the only plugin for
  it):
  - if the backend is enabled and one of its driver packages is missing: the install
    hint
  - else: "names no driver … use `<driver_hint>`"
  - with several candidate adapters: "write `<backend>+<driver>://`"
- **Why:** this used to escape as a traceback. A plugin's dialect usually ships in its
  driver package, so "not installed" is the likely cause. The catch stays narrow so that
  real bugs in a dialect still surface
  (`test_other_errors_finding_the_driver_are_not_caught`).

### AD7: Stateless adapters, a new instance per lookup

- **Decision:** adapter constructors take no arguments, and adapters keep no state
  between calls. Configuration (URI, schema) is passed to `create_engine`.
- **Why:** the registry, `JSONType` and the tests can all create adapters freely, and
  no instance is shared between engines.

### AD8: Each adapter chooses its JSON storage type

- **Decision:** `StoreAdapter.json_type()` is a **concrete** method that returns
  `sa.JSON(none_as_null=True)`. `PostgresAdapter` overrides it with
  `JSONB(none_as_null=True)`. `types.JSONType` is a `TypeDecorator` whose
  `load_dialect_impl(dialect)` returns
  `dialect.type_descriptor(adapter_for_dialect(dialect).json_type())`.
- **Why not `with_variant`:** the old
  `sa.JSON(...).with_variant(JSONB(...), "postgresql")` fixed its dialect list when the
  models were imported. A plugin had no way to add itself to it.
- **Why concrete, not abstract:** generic JSON is correct for most backends, and an
  abstract method would break every existing plugin (see "Adding a member").
- **Which adapter decides:** the attached one (AD12), so a `DB_ADAPTER` plugin for a
  built-in dialect gets its own type. A **bare dialect** (DDL scripts, Alembic offline,
  tests) uses the built-in for its name, **whatever its driver**, and only a backend
  without a built-in falls back to `default_adapter(name, driver)`. The driver rule is a
  `DB_URI` concern and must not break compiling DDL: SQLAlchemy 2.0's generic
  `postgresql.dialect()` is psycopg2, which `PostgresAdapter` would refuse
  (`test_json_type_on_a_bare_postgres_dialect_with_another_driver`).
- **Why an error for an unknown dialect, not a silent fallback:** compiling for a
  dialect no adapter serves raises `StoreError`. A fallback to generic JSON would hide a
  plugin whose `dialect` doesn't match (AD2), because its override would silently stop
  applying.
- **`type_descriptor` and `colspecs`:** `type_descriptor` swaps generic types for the
  dialect's own implementations. An adapter must return the dialect's own type: one
  listed in the dialect's `colspecs` as itself, or a subclass of the dialect's own JSON
  implementation. `JSONB` subclasses Postgres's own `JSON`, so it is kept.
  `FakeDbDialect` in `tests/unit/fake_store_adapters.py` registers its type in
  `colspecs`, as real dialects do.
- **`none_as_null=True` is part of the contract.** Python `None` must be SQL `NULL` on
  every backend.
- **Scope:** only JSON. `UTCDateTime` could follow the same pattern, but no backend
  needs it yet.
- **Consequence for migrations (plan Phase 3):** Alembic will render JSON columns as
  `dbtx.store.types.JSONType()`, so migration scripts import it. Its import path and
  constructor are now effectively public API. Online migrations use the store's engine
  (attached adapter); offline ones compile for a bare dialect (built-in rule above).

### AD9: The interface grows by defaults

- **Decision:** new adapter behaviour is added as a concrete method with a default
  wherever one is correct. Abstract additions are a breaking change for plugins.
- **Why:** plugins live in other packages and can't move in lockstep with dbtx. The
  plugin guide tells authors to pin a dbtx version range, and this rule keeps that
  range from having to be narrow.

### AD10: Several adapters may serve one (backend, driver) pair

- **Decision:** the registry is keyed by adapter name, not by backend. The default for
  a pair is deterministic: the built-in if it supports the pair, else the single plugin
  that does. Several plugins for a pair is an error that asks for `DB_ADAPTER`. dbtx
  never picks one by name order or install order.
- **Why:** real cases need it: another driver for a built-in backend
  (`sqlite+pysqlcipher`), or different behaviour for the same backend and driver (a
  hosted Postgres variant). The rule means installing a plugin can make a previously
  unsupported URI work, but it can never change which adapter serves a URI that already
  worked, unless two plugins then compete for a pair that only plugins serve. In that
  case dbtx stops and asks.

### AD11: `DB_ADAPTER` names an adapter explicitly

- **Decision:** a new option with the usual precedence: `--db-adapter NAME` >
  `DB_ADAPTER=` in `dbtx.conf` > `DBTX_DB_ADAPTER`. Unset means the default for
  `DB_URI`. The value is an adapter **name** (entry-point name), not a class name or a
  `module:Class` path. It is validated even without `DB_URI` (typos surface early),
  and must serve the URI's dialect and driver. Errors about it are attributed to
  `DB_ADAPTER (<source>)`, and errors about the URI to `DB_URI (<source>)`. Like the
  other `--db-*` flags, it contradicts `--standalone-mode`.
- **Why entry-point names:** they are unique by construction (AD13), stable across
  refactors of the plugin's code, and they keep configuration from naming importable
  code directly. Class names can collide between packages, and paths would let any
  importable class be used without being registered.

### AD12: `open_store` attaches the adapter to the engine's dialect

- **Decision:** after `adapter.create_engine()`, `open_store` calls
  `attach_adapter(engine, adapter)`, which sets `engine.dialect.dbtx_store_adapter`.
  `adapter_for_dialect` reads it first.
- **Why:** with several adapters per dialect, `dialect.name` no longer identifies the
  adapter, and a column type sees only the dialect. The attribute carries the resolved
  choice, including `DB_ADAPTER`, to where it is needed.
- **Why in `open_store`, not in each adapter's `create_engine`:** one place, so no
  plugin can forget it. It runs before anything compiles: `create_engine` doesn't
  connect, and SQLAlchemy compiles types only on first use.

### AD13: Two plugins with one name are an error

- **Decision:** `_plugin_entry_points()` raises `StoreError` naming both entry points
  when two installed plugins share a name.
- **Why:** with names as identity, dbtx would otherwise use whichever the environment
  happened to list first. Refusing is the only predictable option.

## Testing adapters

- Registry behaviour: `tests/unit/store/test_store_adapter_registry.py`, with fakes
  from `tests/unit/fake_store_adapters.py` installed through the `adapter_plugins`
  fixture. `CustomSqliteAdapter` / `OtherSqliteAdapter` are plugins for the built-in
  `sqlite` dialect that also support `pysqlcipher`.
- Config checks driven by adapter attributes, and the `DB_ADAPTER` matrix: the plugin
  and `DB_ADAPTER` tests in `tests/unit/test_config.py`.
- The attached adapter and JSON types: `test_store_adapters.py` (`open_store` with and
  without `DB_ADAPTER`) and `test_store_types.py`.
- Behaviour on real backends: the `store` / `engine` fixtures run each test on
  `sqlite-memory`, `sqlite-file` and `postgres`. See [tests.md](../tests.md).
