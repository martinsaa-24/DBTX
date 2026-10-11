# Store Migrations

dbtx's database schema is created and changed by **Alembic revisions that ship inside
the package**, in [`src/dbtx/store/migrations/versions/`](../../src/dbtx/store/migrations/versions/).
This page is for contributors who change the store's models.

## How they run

- dbtx runs Alembic **programmatically**, through
  [`src/dbtx/store/migrate.py`](../../src/dbtx/store/migrate.py). There is no
  `alembic.ini`, and the `alembic` command line is refused by
  [`migrations/env.py`](../../src/dbtx/store/migrations/env.py).
- `migrate.alembic_config(connection, schema)` hands Alembic an open connection from
  the store's engine. So migrations run with the store's adapter attached (JSON columns
  get the adapter's `json_type()`), and on Postgres every table lands in `DB_SCHEMA`
  through the engine's `schema_translate_map`. The version table, `alembic_version`,
  lives in `DB_SCHEMA` too (`version_table_schema`).
- Users run them with `dbtx db init` / `dbtx db upgrade`. The start-up checks migrate a
  SQLite store automatically (`StoreAdapter.upgrades_automatically`), and refuse a
  Postgres database that is behind.
- `migrate.revision_status()` compares the database with head:
  `uninitialised`, `current`, `behind`, or `unknown` (migrated by a newer dbtx).

## Adding a revision

1. **Change the models and the spec together:** `src/dbtx/store/models/` and
   `src/dbtx/store/schema.yml`. `test_store_schema_parity.py` fails until they agree.
2. **Generate the revision** against an empty SQLite database at head. Autogenerate
   compares that database with the models and writes what differs. Use the next
   number as `rev_id`:

   ```python
   from alembic import command

   from dbtx.store import migrate
   from dbtx.store.adapters import SqliteAdapter, attach_adapter

   # An empty SQLite database at head: autogenerate compares it with the models.
   adapter = SqliteAdapter()
   engine = adapter.create_engine("sqlite://", None)
   attach_adapter(engine, adapter)
   with engine.begin() as connection:
       config = migrate.alembic_config(connection, None)
       command.upgrade(config, "head")
       command.revision(config, message="describe the change", autogenerate=True, rev_id="0002")
   ```

   This writes `versions/0002_describe_the_change.py`. With no model change, its
   `upgrade()` and `downgrade()` are empty.
3. **Review it by hand.** Autogenerate is a draft:
   - **Imports:** it renders dbtx's column types as `dbtx.store.types.UTCDateTime(...)`
     but does not import them. Import them (`from dbtx.store.types import JSONType,
     UTCDateTime`) and write `UTCDateTime()` / `JSONType()`. Both set their own
     options, so the arguments autogenerate adds are redundant.
   - **CHECK constraints:** write the expression out as a string literal, as
     `0001_initial.py` does. Never build it from the enums: a revision must mean the
     same thing after the enums change. A new allowed value is a new revision.
   - **Names:** keep the `op.f("...")` names. They come from the naming convention in
     `models/base.py` and must match the models (`test_store_migrations.py` checks).
   - **New tables and indexes:** plain `op.create_table` / `op.create_index`.
   - **Changing an existing table:** SQLite cannot `ALTER` constraints, so use
     `with op.batch_alter_table("name") as batch_op:` (Alembic's batch mode copies
     the table). `render_as_batch` makes autogenerate write it that way. Note that
     `SqliteAdapter` turns `PRAGMA foreign_keys` on for every connection: a batch
     rebuild of a table that other tables reference can fail. Run the migration tests
     on SQLite.
   - **Data changes:** use lightweight `sa.table()` / `sa.column()` constructs, never
     the ORM models, which describe the newest schema, not the one at this revision.
   - **`downgrade()`** undoes `upgrade()`, in reverse order.
   - **Format** with black and isort (`-l 100`).
4. **Update the tests.** Several tests name the head revision literally (for example
   `test_head_is_the_initial_revision` and messages such as `(revision 0001)`), so a
   new head is a visible change to them. `test_store_migrations.py` then proves, on
   SQLite and Postgres, that upgrading an empty database gives exactly the models'
   schema and names, and that downgrading removes it. Add tests for any data the
   revision moves.

## Tests and fixtures

- `test_store_migrations.py`: the script directory, revision status, upgrade from
  empty (with `compare_metadata`, plus a check of constraint and index names, because
  `compare_metadata` ignores CHECK constraints), downgrade, Postgres `DB_SCHEMA`, and
  the adapter's JSON type.
- The `store` fixture builds its tables with `create_all` and records head
  (`create_tables_at_head`): much faster than migrating for every test, and
  `test_store_migrations.py` proves the two give the same schema. `create_all`
  appears nowhere outside test fixtures.
- `extra_revision` copies the migrations to `tmp_path`, adds an empty `0002` and
  points `migrate.SCRIPT_LOCATION` at the copy. With it, a database at the real head
  is "behind", so the upgrade paths are testable while only one revision exists.
