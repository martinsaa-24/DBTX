# To do

Small, understood fixes the user has accepted for now and wants done later. Larger
deferred work is in [backlog.md](backlog.md).

## A refused first run leaves an empty `.dbtx/` folder

**From:** Phase 6, step 13 (2026-10-13): "the empty folder is okay for now".

On a platform whose SQLite database file does not exist yet, any command but `dbtx db
init` / `upgrade` is refused before connecting ("the database file … does not exist;
run `dbtx db init` to create it"), so no database file is created. The database's
**folder** still is: `SqliteAdapter.create_engine` (`src/dbtx/store/adapters/sqlite.py`)
creates the file's parent folder as soon as the store is opened, before the start-up
check runs. With the default `DB_URI=sqlite:///.dbtx/dbtx.db`, the refusal leaves an
empty `<platform root>/.dbtx/`.

**Fix:** create the parent folder only when a connection is actually made (for example
SQLAlchemy's `do_connect` event in the adapter), so a refused run creates nothing and
`dbtx db init` still works. Keep the existing error for a folder that cannot be created
("cannot open the database …: cannot create …", `test_a_sqlite_file_that_cannot_be_created_stops_the_run`).
Red test first: a refused first run leaves no `.dbtx/` at all.
