"""Measure insert throughput into the "orders" table with a 1-connection pool
versus a 4-connection pool.

For each pool size N:
  - N writer threads each check out ONE connection from the pool and hold it
    for their whole life. They take orders off a shared queue and insert them,
    one Session and one commit per order (like one write per finished node).
  - 2*N producer threads only put orders on the queue. They never touch the
    connection pool, so they can never time out waiting for a connection.

Why not let every thread check out its own connection per insert? SQLAlchemy's
pool does not hand out connections in arrival order: a thread that just
returned a connection can take it straight back while a waiting thread is
still waking up. With more threads than connections, an unlucky thread can
lose that race until pool_timeout (30 s) expires and raises TimeoutError.

This script does NOT start Postgres. Start orders_server.py first; this script
connects to that running server and writes into its "orders" table. The rows it
inserts are deleted again after each run.

Run (while orders_server.py is running):
    uv run --extra postgres python examples/pgserver_demo/order_throughput.py
"""

import queue
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from pixeltable_pgserver.utils import PostmasterInfo
from sqlalchemy import DateTime, Integer, String, create_engine, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# Must match PGDATA in orders_server.py.
PGDATA = Path.home() / ".dbtx" / "pgserver_demo" / "pgdata"

ORDER_COUNT = 25000


class Base(DeclarativeBase):
    pass


# Maps onto the "orders" table that orders_server.py created.
class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer: Mapped[str] = mapped_column(String(200), nullable=False)
    item: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# Put on the queue once per writer thread, after all orders, to tell it to exit.
STOP = object()


def writer(engine, order_queue, errors):
    # Check out one connection and hold it until this thread exits, so writer
    # threads never compete with each other for pool connections.
    with engine.connect() as connection:
        while True:
            order = order_queue.get()
            if order is STOP:
                return
            try:
                # One Session per insert, bound to this thread's own connection.
                with Session(bind=connection) as session:
                    session.add(order)
                    session.commit()
            except Exception as error:
                # Keep going: if one bad insert killed this thread, the
                # remaining orders would never be written.
                errors.append(error)


def produce_order(order_queue, customer, order_number):
    # put() on an unbounded queue never blocks, so producers never wait.
    order_queue.put(Order(customer=customer, item=f"item-{order_number}", quantity=1))


def run_benchmark(database_uri, pool_size):
    # pool_size=N with max_overflow=0 means at most N connections, ever.
    engine = create_engine(database_uri, pool_size=pool_size, max_overflow=0)

    # Open all N connections before timing, so connection setup is not measured.
    warmup_connections = []
    for _ in range(pool_size):
        warmup_connections.append(engine.connect())
    for connection in warmup_connections:
        connection.close()

    # Tag this run's rows so we can count and delete exactly these rows.
    customer = f"throughput-pool{pool_size}-{uuid.uuid4().hex[:8]}"

    order_queue = queue.Queue()  # unbounded
    errors = []  # list.append is thread-safe

    start = time.perf_counter()

    # Exactly pool_size writers, so the pool is never asked for more connections than it has.
    writer_threads = []
    for _ in range(pool_size):
        thread = threading.Thread(target=writer, args=(engine, order_queue, errors))
        thread.start()
        writer_threads.append(thread)

    # More producer threads than connections, like dbt running more node threads than writers.
    with ThreadPoolExecutor(max_workers=pool_size * 2) as executor:
        futures = []
        for order_number in range(ORDER_COUNT):
            futures.append(executor.submit(produce_order, order_queue, customer, order_number))
        for future in futures:
            future.result()  # re-raises any producer error

    # All orders are queued. One STOP per writer, then wait for the queue to drain.
    for _ in writer_threads:
        order_queue.put(STOP)
    for thread in writer_threads:
        thread.join()

    elapsed_seconds = time.perf_counter() - start

    if len(errors) > 0:
        raise RuntimeError(f"pool_size={pool_size}: {len(errors)} inserts failed, first error: {errors[0]!r}")

    with Session(engine) as session:
        inserted_count = session.execute(
            select(func.count()).select_from(Order).where(Order.customer == customer)
        ).scalar_one()
        session.execute(delete(Order).where(Order.customer == customer))
        session.commit()

    engine.dispose()

    if inserted_count != ORDER_COUNT:
        raise RuntimeError(f"pool_size={pool_size}: expected {ORDER_COUNT} rows, found {inserted_count}")

    return elapsed_seconds


def print_result(pool_size, elapsed_seconds):
    orders_per_second = ORDER_COUNT / elapsed_seconds
    milliseconds_per_order = elapsed_seconds * 1000 / ORDER_COUNT
    print(
        f"pool_size={pool_size}: {ORDER_COUNT} orders in {elapsed_seconds:.2f} s "
        f"-> {orders_per_second:,.0f} orders/s, {milliseconds_per_order:.3f} ms/order"
    )


def main():
    postmaster_info = PostmasterInfo.read_from_pgdata(PGDATA)
    if postmaster_info is None or not postmaster_info.is_running():
        print(f"No running Postgres found for {PGDATA}. Start orders_server.py first.", file=sys.stderr)
        return 1

    database_uri = postmaster_info.get_uri(driver="psycopg")
    print(f"Connecting to: {database_uri}")
    print(f"Inserting {ORDER_COUNT} orders per run, one commit per order.\n")

    elapsed_pool_1 = run_benchmark(database_uri, pool_size=1)
    print_result(1, elapsed_pool_1)

    elapsed_pool_4 = run_benchmark(database_uri, pool_size=4)
    print_result(4, elapsed_pool_4)

    print(f"\nSpeedup of pool_size=4 over pool_size=1: {elapsed_pool_1 / elapsed_pool_4:.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
