"""Start a local Postgres (via pixeltable-pgserver), create an "orders" table, and print
the 5 most recent orders every 15 seconds.

On Ctrl+C (SIGINT) or SIGTERM the "orders" table is dropped, and pgserver
stops the Postgres server when this process exits.

Run:
    uv run --extra postgres python examples/pgserver_demo/orders_server.py

Then, in another terminal, run add_order.py to insert orders.
"""

import signal
import sys
import threading
from pathlib import Path

import pixeltable_pgserver
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
)

# Both scripts must use the same pgdata directory.
PGDATA = Path.home() / ".dbtx" / "pgserver_demo" / "pgdata"

QUERY_INTERVAL_SECONDS = 15

metadata = MetaData()

orders = Table(
    "orders",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("customer", String(200), nullable=False),
    Column("item", String(200), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

stop_requested = threading.Event()


def handle_stop_signal(signum, frame):
    print(f"\nReceived signal {signum}, stopping...")
    stop_requested.set()


def main():
    # Python's default SIGTERM handling exits without running atexit hooks,
    # which would skip both our table drop and pgserver's server shutdown.
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)

    PGDATA.mkdir(parents=True, exist_ok=True)

    # cleanup_mode="stop": when the last process holding a handle exits,
    # pgserver stops the server. The data directory is kept on disk.
    server = pixeltable_pgserver.get_server(PGDATA, cleanup_mode="stop")

    # driver="psycopg" gives a "postgresql+psycopg://" URI, so SQLAlchemy uses psycopg 3.
    database_uri = server.get_uri(driver="psycopg")
    print(f"Postgres is running. pgdata: {PGDATA}")
    print(f"Database URI: {database_uri}")

    engine = create_engine(database_uri)

    metadata.create_all(engine, tables=[orders])
    print('Table "orders" is ready.')

    try:
        while not stop_requested.is_set():
            query = select(orders).order_by(orders.c.id.desc()).limit(5)
            with engine.connect() as connection:
                rows = connection.execute(query).all()

            print(f"\nLast {len(rows)} orders (newest first):")
            if len(rows) == 0:
                print("  (no orders yet)")
            for row in rows:
                print(
                    f"  id={row.id} customer={row.customer} item={row.item} "
                    f"quantity={row.quantity} created_at={row.created_at}"
                )

            # Wakes up immediately if a stop signal arrives.
            stop_requested.wait(QUERY_INTERVAL_SECONDS)
    finally:
        print('Dropping table "orders"...')
        metadata.drop_all(engine, tables=[orders])
        engine.dispose()
        print('Table "orders" dropped. pgserver will stop Postgres on exit.')

    return 0


if __name__ == "__main__":
    sys.exit(main())
