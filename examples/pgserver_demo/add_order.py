"""Connect to the Postgres server started by orders_server.py, insert one
order with SQLAlchemy, then read it back from the database to validate it.

This script does NOT start Postgres. It reads the connection details from the
running server's postmaster.pid and exits with an error if no server is running.

Run (while orders_server.py is running):
    uv run --extra postgres python examples/pgserver_demo/add_order.py
    uv run --extra postgres python examples/pgserver_demo/add_order.py alice widget 3
"""

import sys
from datetime import datetime
from pathlib import Path

from pixeltable_pgserver.utils import PostmasterInfo
from sqlalchemy import DateTime, Integer, String, create_engine, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# Must match PGDATA in orders_server.py.
PGDATA = Path.home() / ".dbtx" / "pgserver_demo" / "pgdata"


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


def main():
    if len(sys.argv) == 4:
        customer = sys.argv[1]
        item = sys.argv[2]
        quantity = int(sys.argv[3])
    elif len(sys.argv) == 1:
        customer = "demo-customer"
        item = "demo-item"
        quantity = 1
    else:
        print("usage: add_order.py [CUSTOMER ITEM QUANTITY]", file=sys.stderr)
        return 2

    postmaster_info = PostmasterInfo.read_from_pgdata(PGDATA)
    if postmaster_info is None or not postmaster_info.is_running():
        print(f"No running Postgres found for {PGDATA}. Start orders_server.py first.", file=sys.stderr)
        return 1

    database_uri = postmaster_info.get_uri(driver="psycopg")
    print(f"Connecting to: {database_uri}")
    engine = create_engine(database_uri)

    # Insert the order.
    new_order = Order(customer=customer, item=item, quantity=quantity)
    with Session(engine) as session:
        session.add(new_order)
        session.commit()
        new_order_id = new_order.id
    print(f"Inserted order id={new_order_id}")

    # Read it back in a fresh session, so the values come from the database
    # and not from the session's identity map.
    with Session(engine) as session:
        stored_order = session.execute(select(Order).where(Order.id == new_order_id)).scalar_one_or_none()

    engine.dispose()

    if stored_order is None:
        print(f"VALIDATION FAILED: order id={new_order_id} not found in the database.", file=sys.stderr)
        return 1

    if stored_order.customer != customer or stored_order.item != item or stored_order.quantity != quantity:
        print(
            f"VALIDATION FAILED: stored order does not match. "
            f"expected=({customer}, {item}, {quantity}) "
            f"got=({stored_order.customer}, {stored_order.item}, {stored_order.quantity})",
            file=sys.stderr,
        )
        return 1

    print(
        f"Validated order from database: id={stored_order.id} customer={stored_order.customer} "
        f"item={stored_order.item} quantity={stored_order.quantity} created_at={stored_order.created_at}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
