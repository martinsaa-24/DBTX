"""The manifest sync (`dbtx.store.repositories.nodes.sync_manifest`, plan 5.1).

Behaviour, for one project (the caller owns the transaction; the project's claim row
exists):
* nodes, node versions and edges are upserted. `first_seen_at` keeps the earliest
  sighting and `last_seen_at` the latest, whatever order manifests are loaded in
* a node's attributes (`resource_type`, `package_name`, `name`, `checksum`) and a
  cleared `removed_at` follow the **newest** manifest loaded: an older manifest loaded
  later never overwrites them
* a node version is one row per (node, checksum): a node that reverts to an old
  checksum bumps that version's `last_seen_at`
* removals: a node or edge not in the newest manifest loaded gets `removed_at` = that
  manifest's time; re-adding it clears `removed_at`. A node only in an older manifest
  loaded later is marked removed at the newest manifest's time
* loading the same manifest again changes nothing; another project's rows are never
  touched; more rows than one batch (500) are all stored
* returns the counts stored, and the nodes and edges newly marked removed
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from dbt.artifacts.resources import FileHash
from dbt.contracts.graph.manifest import Manifest

from dbtx.store import Store
from dbtx.store import models as m
from dbtx.store.loaders.manifest import ManifestRows, manifest_rows
from dbtx.store.repositories.nodes import SyncCounts, sync_manifest

T1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T2 = T1 + timedelta(days=1)
T3 = T1 + timedelta(days=2)

CUSTOMERS = "model.jaffle_shop.customers"
ORDERS = "model.jaffle_shop.orders"


def add_project(store: Store, project: str = "jaffle_shop") -> None:
    with store.begin() as session:
        session.add(m.Project(project=project, folder=project, claimed_at=T1, last_seen_at=T1))


def rows_at(manifest: Manifest, at: datetime) -> ManifestRows:
    manifest.metadata.generated_at = at.replace(tzinfo=None)
    return manifest_rows(manifest)


def sync(store: Store, rows: ManifestRows) -> SyncCounts:
    with store.begin() as session:
        return sync_manifest(session, store.adapter, rows)


def node(store: Store, unique_id: str, project: str = "jaffle_shop") -> m.Node:
    with store.session() as session:
        return session.get(m.Node, (project, unique_id))


def versions(store: Store, unique_id: str) -> list[tuple[str, datetime, datetime]]:
    with store.session() as session:
        rows = session.scalars(
            sa.select(m.NodeVersion)
            .where(m.NodeVersion.unique_id == unique_id)
            .order_by(m.NodeVersion.first_seen_at)
        ).all()
        return [(row.checksum, row.first_seen_at, row.last_seen_at) for row in rows]


def edges_of(store: Store, unique_id: str) -> list[tuple[str, str, datetime | None]]:
    with store.session() as session:
        rows = session.scalars(
            sa.select(m.NodeEdge)
            .where(sa.or_(m.NodeEdge.parent_id == unique_id, m.NodeEdge.child_id == unique_id))
            .order_by(m.NodeEdge.parent_id, m.NodeEdge.child_id)
        ).all()
        return [(row.parent_id, row.child_id, row.removed_at) for row in rows]


def count(store: Store, model: type) -> int:
    with store.session() as session:
        return session.scalar(sa.select(sa.func.count()).select_from(model))


def table_snapshot(store: Store) -> dict[str, list[tuple]]:
    snapshot = {}
    with store.session() as session:
        for model in (m.Node, m.NodeVersion, m.NodeEdge):
            rows = session.execute(sa.select(model.__table__)).all()
            snapshot[model.__tablename__] = sorted(tuple(row) for row in rows)
    return snapshot


def set_checksum(manifest: Manifest, unique_id: str, checksum: str) -> None:
    manifest.nodes[unique_id].checksum = FileHash(name="sha256", checksum=checksum)


# --------------------------------------------------------------------------------------
# Upserts
# --------------------------------------------------------------------------------------


def test_the_first_sync_stores_every_node_version_and_edge(store: Store, manifest: Manifest) -> None:
    add_project(store)
    rows = rows_at(manifest, T1)
    counts = sync(store, rows)
    assert counts == SyncCounts(
        nodes=49, versions=42, edges=len(rows.edges), removed_nodes=0, removed_edges=0
    )
    assert count(store, m.Node) == 49
    assert count(store, m.NodeVersion) == 42
    assert count(store, m.NodeEdge) == len(rows.edges)
    customers = node(store, CUSTOMERS)
    assert (customers.first_seen_at, customers.last_seen_at, customers.removed_at) == (T1, T1, None)
    assert customers.resource_type == "model"
    assert customers.checksum == manifest.nodes[CUSTOMERS].checksum.checksum


def test_a_later_sync_moves_last_seen_only(store: Store, manifest: Manifest) -> None:
    add_project(store)
    sync(store, rows_at(manifest, T1))
    sync(store, rows_at(manifest, T2))
    customers = node(store, CUSTOMERS)
    assert (customers.first_seen_at, customers.last_seen_at) == (T1, T2)
    assert count(store, m.NodeVersion) == 42
    checksum = manifest.nodes[CUSTOMERS].checksum.checksum
    assert versions(store, CUSTOMERS) == [(checksum, T1, T2)]


def test_the_same_manifest_again_changes_nothing(store: Store, manifest: Manifest) -> None:
    add_project(store)
    sync(store, rows_at(manifest, T1))
    before = table_snapshot(store)
    sync(store, rows_at(manifest, T1))
    assert table_snapshot(store) == before


def test_a_changed_checksum_adds_a_version(store: Store, manifest: Manifest) -> None:
    add_project(store)
    set_checksum(manifest, CUSTOMERS, "a" * 64)
    sync(store, rows_at(manifest, T1))
    set_checksum(manifest, CUSTOMERS, "b" * 64)
    sync(store, rows_at(manifest, T2))
    assert node(store, CUSTOMERS).checksum == "b" * 64
    assert versions(store, CUSTOMERS) == [("a" * 64, T1, T1), ("b" * 64, T2, T2)]


def test_a_reverted_checksum_bumps_the_old_version(store: Store, manifest: Manifest) -> None:
    add_project(store)
    set_checksum(manifest, CUSTOMERS, "a" * 64)
    sync(store, rows_at(manifest, T1))
    set_checksum(manifest, CUSTOMERS, "b" * 64)
    sync(store, rows_at(manifest, T2))
    set_checksum(manifest, CUSTOMERS, "a" * 64)
    sync(store, rows_at(manifest, T3))
    assert node(store, CUSTOMERS).checksum == "a" * 64
    assert versions(store, CUSTOMERS) == [("a" * 64, T1, T3), ("b" * 64, T2, T2)]


# --------------------------------------------------------------------------------------
# Removals
# --------------------------------------------------------------------------------------


def test_a_node_missing_from_the_next_manifest_is_marked_removed(store: Store, manifest: Manifest) -> None:
    add_project(store)
    sync(store, rows_at(manifest, T1))
    customer_edges = len(edges_of(store, CUSTOMERS))
    manifest.nodes.pop(CUSTOMERS)
    counts = sync(store, rows_at(manifest, T2))
    assert node(store, CUSTOMERS).removed_at == T2
    assert node(store, ORDERS).removed_at is None
    assert {removed for _, _, removed in edges_of(store, CUSTOMERS)} == {T2}
    assert counts.removed_nodes == 1
    assert counts.removed_edges == customer_edges


def test_a_re_added_node_is_no_longer_removed(store: Store, manifest: Manifest) -> None:
    add_project(store)
    sync(store, rows_at(manifest, T1))
    customers = manifest.nodes.pop(CUSTOMERS)
    sync(store, rows_at(manifest, T2))
    manifest.nodes[CUSTOMERS] = customers
    sync(store, rows_at(manifest, T3))
    row = node(store, CUSTOMERS)
    assert (row.first_seen_at, row.last_seen_at, row.removed_at) == (T1, T3, None)
    assert {removed for _, _, removed in edges_of(store, CUSTOMERS)} == {None}


def test_removals_never_touch_another_project(store: Store, manifest: Manifest) -> None:
    add_project(store, "jaffle_shop")
    add_project(store, "other_shop")
    sync(store, rows_at(manifest, T1))
    manifest.metadata.project_name = "other_shop"
    sync(store, rows_at(manifest, T1))
    manifest.nodes.pop(CUSTOMERS)
    sync(store, rows_at(manifest, T2))
    assert node(store, CUSTOMERS, "other_shop").removed_at == T2
    assert node(store, CUSTOMERS, "jaffle_shop").removed_at is None


# --------------------------------------------------------------------------------------
# Out-of-order loads (post-hoc)
# --------------------------------------------------------------------------------------


def test_an_older_manifest_loaded_later_keeps_the_newest_attributes(store: Store, manifest: Manifest) -> None:
    add_project(store)
    set_checksum(manifest, CUSTOMERS, "b" * 64)
    sync(store, rows_at(manifest, T2))
    set_checksum(manifest, CUSTOMERS, "a" * 64)
    sync(store, rows_at(manifest, T1))
    row = node(store, CUSTOMERS)
    assert row.checksum == "b" * 64
    assert (row.first_seen_at, row.last_seen_at) == (T1, T2)
    assert versions(store, CUSTOMERS) == [("a" * 64, T1, T1), ("b" * 64, T2, T2)]


def test_an_older_manifest_loaded_later_does_not_undo_a_removal(store: Store, manifest: Manifest) -> None:
    add_project(store)
    sync(store, rows_at(manifest, T1))
    customers = manifest.nodes.pop(CUSTOMERS)
    sync(store, rows_at(manifest, T3))
    manifest.nodes[CUSTOMERS] = customers
    sync(store, rows_at(manifest, T2))
    row = node(store, CUSTOMERS)
    assert row.removed_at == T3
    assert row.last_seen_at == T2


def test_a_node_only_in_an_older_manifest_is_marked_removed(store: Store, manifest: Manifest) -> None:
    add_project(store)
    customers = manifest.nodes.pop(CUSTOMERS)
    sync(store, rows_at(manifest, T2))
    manifest.nodes[CUSTOMERS] = customers
    sync(store, rows_at(manifest, T1))
    row = node(store, CUSTOMERS)
    assert (row.first_seen_at, row.last_seen_at, row.removed_at) == (T1, T1, T2)


# --------------------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------------------


def test_more_rows_than_one_batch_are_all_stored(store: Store) -> None:
    add_project(store)
    nodes = []
    versions_rows = []
    edges = []
    for number in range(1200):
        unique_id = f"model.jaffle_shop.m{number:04d}"
        nodes.append(
            {
                "project": "jaffle_shop",
                "unique_id": unique_id,
                "resource_type": "model",
                "package_name": "jaffle_shop",
                "name": f"m{number:04d}",
                "checksum": f"{number:064d}",
            }
        )
        versions_rows.append({"project": "jaffle_shop", "unique_id": unique_id, "checksum": f"{number:064d}"})
        if number > 0:
            edges.append({"project": "jaffle_shop", "parent_id": nodes[number - 1]["unique_id"], "child_id": unique_id})
    rows = ManifestRows(
        project="jaffle_shop", adapter_type="duckdb", seen_at=T1, nodes=nodes, versions=versions_rows, edges=edges
    )
    assert sync(store, rows) == SyncCounts(nodes=1200, versions=1200, edges=1199, removed_nodes=0, removed_edges=0)
    assert count(store, m.Node) == 1200
    assert count(store, m.NodeVersion) == 1200
    assert count(store, m.NodeEdge) == 1199
