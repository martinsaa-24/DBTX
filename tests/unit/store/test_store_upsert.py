"""`adapter.upsert`, `adapter.greatest` / `adapter.least`: the same results on every backend.

Behaviour (plan section 4):

`upsert(table, rows, keys=..., update=..., merge=...)`
* a row whose key is new is inserted with every value given
* a row whose key exists updates the stored row instead:
  * `update` columns take the new value
  * `merge` columns follow their rule: `least` keeps the smaller of the stored and the
    new value, `greatest` the larger, `clear` sets NULL
  * every other column keeps its stored value
* with no `update` and no `merge`, an existing row is left as it is
* one statement can mix new and existing keys
* refused with ValueError, before anything runs: no rows; `keys` other than the primary
  key; a key in `update` or `merge`; a column in both; an unknown column or rule;
  `least` / `greatest` on a nullable column (NULL handling differs per backend);
  `clear` on a NOT NULL column; the same key twice in `rows` (Postgres rejects it,
  SQLite would apply both)

`greatest(*values)` / `least(*values)`
* the largest / smallest of two or more values in the same row, never an aggregate,
  typed like the first value (so timestamps come back as aware UTC datetimes)
* fewer than two values is refused with ValueError
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from dbtx.store import Store
from dbtx.store import models as m

T1 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
T2 = T1 + timedelta(hours=1)
T3 = T1 + timedelta(hours=2)

NODE_KEYS = ["project", "unique_id"]
NODE_UPDATE = ["resource_type", "package_name", "name", "checksum"]
NODE_MERGE = {"first_seen_at": "least", "last_seen_at": "greatest", "removed_at": "clear"}

ORDERS = "model.jaffle_shop.orders"
CUSTOMERS = "model.jaffle_shop.customers"


def node_row(
    unique_id: str = ORDERS,
    name: str = "orders",
    checksum: str | None = "c1",
    first_seen_at: datetime = T1,
    last_seen_at: datetime = T1,
    removed_at: datetime | None = None,
) -> dict:
    return {
        "project": "jaffle_shop",
        "unique_id": unique_id,
        "resource_type": "model",
        "package_name": "jaffle_shop",
        "name": name,
        "checksum": checksum,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "removed_at": removed_at,
    }


@pytest.fixture
def project_store(store: Store) -> Store:
    """`store` with the project row every node needs (foreign key)."""
    with store.begin() as session:
        session.add(m.Project(project="jaffle_shop", folder="jaffle-shop", claimed_at=T1, last_seen_at=T1))
    return store


def upsert_nodes(
    store: Store,
    rows: list[dict[str, object]],
    update: list[str] = NODE_UPDATE,
    merge: dict[str, str] = NODE_MERGE,
) -> None:
    statement = store.adapter.upsert(
        m.Node.__table__, rows, keys=NODE_KEYS, update=update, merge=merge
    )
    with store.begin() as session:
        session.execute(statement)


def stored_nodes(store: Store) -> dict[str, dict]:
    with store.session() as session:
        nodes = session.scalars(sa.select(m.Node)).all()
        stored = {}
        for node in nodes:
            stored[node.unique_id] = {
                "name": node.name,
                "checksum": node.checksum,
                "first_seen_at": node.first_seen_at,
                "last_seen_at": node.last_seen_at,
                "removed_at": node.removed_at,
            }
        return stored


# --------------------------------------------------------------------------------------
# upsert: inserting and updating
# --------------------------------------------------------------------------------------


def test_new_rows_are_inserted_with_every_value(project_store: Store) -> None:
    upsert_nodes(
        project_store,
        [node_row(), node_row(CUSTOMERS, name="customers", checksum=None, removed_at=T2)],
    )
    assert stored_nodes(project_store) == {
        ORDERS: {"name": "orders", "checksum": "c1", "first_seen_at": T1, "last_seen_at": T1, "removed_at": None},
        CUSTOMERS: {"name": "customers", "checksum": None, "first_seen_at": T1, "last_seen_at": T1, "removed_at": T2},
    }


def test_update_columns_take_the_new_value(project_store: Store) -> None:
    upsert_nodes(project_store, [node_row()])
    upsert_nodes(project_store, [node_row(name="orders_v2", checksum="c2")])
    stored = stored_nodes(project_store)[ORDERS]
    assert (stored["name"], stored["checksum"]) == ("orders_v2", "c2")


def test_update_can_set_null(project_store: Store) -> None:
    upsert_nodes(project_store, [node_row(checksum="c1")])
    upsert_nodes(project_store, [node_row(checksum=None)])
    assert stored_nodes(project_store)[ORDERS]["checksum"] is None


@pytest.mark.parametrize("stored, new", [(T1, T2), (T2, T1)], ids=["stored-earlier", "new-earlier"])
def test_least_keeps_the_earlier_value(
    project_store: Store,
    stored: datetime,
    new: datetime,
) -> None:
    upsert_nodes(project_store, [node_row(first_seen_at=stored, last_seen_at=T3)])
    upsert_nodes(project_store, [node_row(first_seen_at=new, last_seen_at=T3)])
    assert stored_nodes(project_store)[ORDERS]["first_seen_at"] == T1


@pytest.mark.parametrize("stored, new", [(T2, T3), (T3, T2)], ids=["stored-earlier", "new-earlier"])
def test_greatest_keeps_the_later_value(
    project_store: Store,
    stored: datetime,
    new: datetime,
) -> None:
    upsert_nodes(project_store, [node_row(first_seen_at=T1, last_seen_at=stored)])
    upsert_nodes(project_store, [node_row(first_seen_at=T1, last_seen_at=new)])
    assert stored_nodes(project_store)[ORDERS]["last_seen_at"] == T3


@pytest.mark.parametrize("new_removed_at", [None, T3], ids=["new-null", "new-set"])
def test_clear_sets_null_whatever_the_new_value(
    project_store: Store,
    new_removed_at: datetime | None,
) -> None:
    upsert_nodes(project_store, [node_row(removed_at=T2)])
    upsert_nodes(project_store, [node_row(removed_at=new_removed_at)])
    assert stored_nodes(project_store)[ORDERS]["removed_at"] is None


def test_other_columns_keep_the_stored_value(project_store: Store) -> None:
    upsert_nodes(project_store, [node_row(name="orders", checksum="c1", removed_at=T2)])
    upsert_nodes(
        project_store,
        [node_row(name="renamed", checksum="c2", first_seen_at=T3, last_seen_at=T3, removed_at=None)],
        update=["name"],
        merge={},
    )
    assert stored_nodes(project_store)[ORDERS] == {
        "name": "renamed", "checksum": "c1", "first_seen_at": T1, "last_seen_at": T1, "removed_at": T2,
    }


def test_no_update_and_no_merge_leaves_an_existing_row_alone(project_store: Store) -> None:
    upsert_nodes(project_store, [node_row(name="orders")])
    upsert_nodes(
        project_store,
        [node_row(name="renamed", last_seen_at=T3), node_row(CUSTOMERS, name="customers")],
        update=[],
        merge=None,
    )
    stored = stored_nodes(project_store)
    assert (stored[ORDERS]["name"], stored[ORDERS]["last_seen_at"]) == ("orders", T1)
    assert stored[CUSTOMERS]["name"] == "customers"


def test_one_statement_mixes_new_and_existing_keys(project_store: Store) -> None:
    upsert_nodes(project_store, [node_row(last_seen_at=T1)])
    upsert_nodes(
        project_store,
        [node_row(last_seen_at=T2), node_row(CUSTOMERS, name="customers", last_seen_at=T2)],
    )
    stored = stored_nodes(project_store)
    assert sorted(stored) == [CUSTOMERS, ORDERS]
    assert stored[ORDERS]["last_seen_at"] == T2


# --------------------------------------------------------------------------------------
# upsert: refused before anything runs
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rows, keys, update, merge, error",
    [
        ([], NODE_KEYS, NODE_UPDATE, NODE_MERGE, "no rows"),
        ([node_row()], ["unique_id"], NODE_UPDATE, NODE_MERGE, "keys must be the primary key"),
        ([node_row()], NODE_KEYS, ["unique_id", "name"], {}, "'unique_id' is a key"),
        ([node_row()], NODE_KEYS, [], {"project": "least"}, "'project' is a key"),
        ([node_row()], NODE_KEYS, ["name", "last_seen_at"], {"last_seen_at": "greatest"}, "'last_seen_at' is in both"),
        ([node_row()], NODE_KEYS, ["no_such_column"], {}, "node has no column 'no_such_column'"),
        ([node_row()], NODE_KEYS, [], {"no_such_column": "clear"}, "node has no column 'no_such_column'"),
        ([node_row()], NODE_KEYS, [], {"last_seen_at": "max"}, "unknown merge rule 'max'"),
        ([node_row()], NODE_KEYS, [], {"checksum": "least"}, "'checksum' is nullable"),
        ([node_row()], NODE_KEYS, [], {"name": "clear"}, "'name' is NOT NULL"),
        ([node_row(), node_row(name="again")], NODE_KEYS, NODE_UPDATE, NODE_MERGE, "the same key twice"),
    ],
    ids=[
        "no-rows", "keys-not-pk", "key-in-update", "key-in-merge", "update-and-merge",
        "unknown-update-column", "unknown-merge-column", "unknown-rule", "least-on-nullable",
        "clear-on-not-null", "duplicate-key",
    ],
)
def test_bad_upserts_are_refused(
    store: Store,
    rows: list[dict[str, object]],
    keys: list[str],
    update: list[str],
    merge: dict[str, str],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        store.adapter.upsert(m.Node.__table__, rows, keys=keys, update=update, merge=merge)


# --------------------------------------------------------------------------------------
# greatest / least
# --------------------------------------------------------------------------------------


def test_greatest_and_least_of_values(store: Store) -> None:
    values = [sa.literal(2), sa.literal(5), sa.literal(3)]
    with store.engine.connect() as conn:
        assert conn.execute(sa.select(store.adapter.greatest(*values))).scalar() == 5
        assert conn.execute(sa.select(store.adapter.least(*values))).scalar() == 2


def test_greatest_and_least_work_per_row_never_as_an_aggregate(project_store: Store) -> None:
    upsert_nodes(
        project_store,
        [
            node_row(ORDERS, first_seen_at=T1, last_seen_at=T2),
            node_row(CUSTOMERS, name="customers", first_seen_at=T3, last_seen_at=T2),
        ],
    )
    node = m.Node.__table__.c
    adapter = project_store.adapter
    statement = sa.select(
        node.unique_id,
        adapter.greatest(node.first_seen_at, node.last_seen_at).label("latest"),
        adapter.least(node.first_seen_at, node.last_seen_at).label("earliest"),
    ).order_by(node.unique_id)
    with project_store.engine.connect() as conn:
        rows = conn.execute(statement).all()
    # One result per row, typed like the columns: aware UTC datetimes on every backend.
    assert [tuple(row) for row in rows] == [(CUSTOMERS, T3, T2), (ORDERS, T2, T1)]


@pytest.mark.parametrize("count", [0, 1])
def test_greatest_and_least_need_two_values(store: Store, count: int) -> None:
    values = [sa.literal(1)] * count
    with pytest.raises(ValueError, match="two or more"):
        store.adapter.greatest(*values)
    with pytest.raises(ValueError, match="two or more"):
        store.adapter.least(*values)
